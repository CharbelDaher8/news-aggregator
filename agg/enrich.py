"""LLM enrichment via `claude -p`, using the local OAuth session.

Why the CLI instead of the API: it authenticates with the OAuth token already in
the login keychain, so there is no API key to manage and usage draws against the
Claude subscription rather than per-token billing.

The cost model is the thing that shapes this module. Every `claude -p`
invocation carries a fixed floor of roughly 8k tokens of Claude Code scaffolding
- measured, and not reducible via --setting-sources or --disable-slash-commands.
So the unit of work is a BATCH, never an item: one call per ~80 items instead of
one call per item turns ~1M tokens of daily overhead into ~24k.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sqlite3

# The `claude` on PATH is often a shell alias or a wrapper shim, neither of which
# exists under launchd/cron. Resolve to a real binary.
CLAUDE_BIN = os.environ.get("NEWS_CLAUDE_BIN") or "/opt/homebrew/bin/claude"

BATCH_SIZE = 80          # items per call; keeps output tokens and attention sane
TOP_CANDIDATES = 24      # finalists considered for the day's top picks
TOP_N = 5
TOP_FLOOR = 70           # minimum score to be eligible as a top pick
TOP_PER_SOURCE = 2       # max candidates one source may contribute to the picks

SYSTEM = (
    "You are the editor of a private daily briefing for one reader: a technical "
    "person who works on AI infrastructure and cares deeply about cybersecurity. "
    "You are terse, concrete and allergic to hype. You never pad. Output JSON only."
)

RUBRIC = """Score each item 0-100 for THIS reader:

  90-100  changes what they should do this week (actively exploited vuln in
          software they plausibly run; a genuinely new capability or technique)
  70-89   substantive and worth reading today (real research, notable release,
          good technical writeup)
  40-69   useful context, skimmable
  10-39   routine news, incremental, or churn
  0-9     marketing, listicles, funding-round noise, engagement bait

Rules:
- Judge the SUBSTANCE, not the popularity number. A 900-point HN post about a
  product launch can score below a 60-point post about a novel attack.
- A CVE already exploited in the wild (KEV) outranks a higher-severity CVE that
  is only theoretical.
- Prefer primary sources over commentary about them.
- summary: ONE clause, <=20 words, stating what is actually new. No "This
  article discusses". If the title already says it, add what the title omits.
- category is the TOPIC: ai | security | tech. Judge by subject matter, not by
  where it came from - a repo containing an exploit is security, an inference
  engine is ai. ("repos" is deliberately not an option; whether something is a
  repo is tracked separately.)
"""

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer", "description": "the item's number"},
                    "summary": {"type": "string"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "category": {"type": "string",
                                 "enum": ["ai", "security", "tech"]},
                },
                "required": ["n", "summary", "score", "category"],
            },
        }
    },
    "required": ["items"],
}

TOP_SCHEMA = {
    "type": "object",
    "properties": {
        "top": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "why": {"type": "string",
                            "description": "why this matters to the reader, one sentence"},
                },
                "required": ["n", "why"],
            },
        }
    },
    "required": ["top"],
}


class EnrichError(RuntimeError):
    pass


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # Claude Code refuses to launch nested inside another Claude Code session.
    # Harmless to strip unconditionally; required when a run is triggered from
    # inside one.
    for var in ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(var, None)
    return env


def call_claude(prompt: str, schema: dict, model: str = "haiku",
                timeout: int = 300, max_budget: float = 0.75) -> dict:
    """One `claude -p` call returning schema-validated JSON."""
    if not (shutil.which(CLAUDE_BIN) or os.path.isfile(CLAUDE_BIN)):
        raise EnrichError(
            f"claude binary not found at {CLAUDE_BIN}. Set NEWS_CLAUDE_BIN.")

    cmd = [
        CLAUDE_BIN, "-p", prompt,
        # No tools: this is pure inference. Prevents the model from trying to
        # read files or search, and drops the tool definitions from the prompt.
        "--tools", "",
        "--system-prompt", SYSTEM,
        "--json-schema", json.dumps(schema),
        "--output-format", "json",
        "--model", model,
        "--no-session-persistence",
        # Belt and braces: a runaway loop can't quietly burn the subscription.
        "--max-budget-usd", str(max_budget),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=_env())
    except subprocess.TimeoutExpired as exc:
        raise EnrichError(f"claude timed out after {timeout}s") from exc

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise EnrichError(f"claude exited {proc.returncode}: {tail}")

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise EnrichError(f"claude gave non-JSON: {proc.stdout[:300]}") from exc

    if envelope.get("is_error"):
        raise EnrichError(f"claude reported error: {envelope.get('result')!r}")

    # With --json-schema the payload lands in structured_output and `result` is
    # empty. Fall back to parsing `result` in case that ever changes.
    out = envelope.get("structured_output")
    if out is None:
        try:
            out = json.loads(envelope.get("result") or "")
        except json.JSONDecodeError as exc:
            raise EnrichError("claude returned no structured_output") from exc

    out["_usage"] = envelope.get("usage", {})
    out["_cost"] = envelope.get("total_cost_usd", 0.0)
    out["_duration_ms"] = envelope.get("duration_ms", 0)
    # `--model haiku` is an alias; the envelope's modelUsage keys carry the dated
    # snapshot that actually ran (e.g. claude-haiku-4-5-20251001). Worth recording
    # so a usage history stays meaningful after an alias starts pointing
    # somewhere new.
    resolved = list((envelope.get("modelUsage") or {}).keys())
    out["_model"] = resolved[0] if len(resolved) == 1 else ("+".join(sorted(resolved)) or None)
    return out


# Per-MTok list prices. Cache writes bill at 1.25x the input rate and cache
# reads at 0.10x — the same arithmetic `claude -p` uses for its own
# total_cost_usd, verified to reproduce it exactly. Used only when an envelope
# omits total_cost_usd; otherwise the reported figure wins.
PRICES = {
    "haiku":   {"in": 1.00, "out": 5.00},   # claude-haiku-4-5
    "sonnet":  {"in": 3.00, "out": 15.00},  # claude-sonnet-5
    "opus":    {"in": 5.00, "out": 25.00},  # claude-opus-5
}
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


def price_for(model: str | None) -> dict:
    """Look up rates from either an alias ("haiku") or a resolved snapshot
    ("claude-haiku-4-5-20251001"), so recording the full model ID doesn't
    silently fall back to the wrong tier."""
    name = (model or "haiku").lower()
    for tier, rates in PRICES.items():
        if tier in name:
            return rates
    return PRICES["haiku"]


def estimate_cost(usage: dict, model: str = "haiku") -> float:
    p = price_for(model)
    return (
        usage.get("input_tokens", 0) * p["in"]
        + usage.get("cache_creation_tokens", 0) * p["in"] * CACHE_WRITE_MULT
        + usage.get("cache_read_tokens", 0) * p["in"] * CACHE_READ_MULT
        + usage.get("output_tokens", 0) * p["out"]
    ) / 1_000_000


class Usage:
    """Running total of token spend across the calls in one run."""

    FIELDS = ("input_tokens", "cache_creation_tokens", "cache_read_tokens",
              "output_tokens")

    def __init__(self, model: str) -> None:
        self.model = model          # the alias requested, e.g. "haiku"
        self.resolved_model: str | None = None   # what actually ran
        self.calls = 0
        self.cost_usd = 0.0
        self.duration_s = 0.0
        self.input_tokens = self.cache_creation_tokens = 0
        self.cache_read_tokens = self.output_tokens = 0

    def add(self, out: dict) -> None:
        u = out.get("_usage") or {}
        self.calls += 1
        self.input_tokens += u.get("input_tokens", 0) or 0
        # The envelope spells these with an `_input_` infix; ours drop it.
        self.cache_creation_tokens += u.get("cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += u.get("cache_read_input_tokens", 0) or 0
        self.output_tokens += u.get("output_tokens", 0) or 0
        self.cost_usd += out.get("_cost", 0.0) or 0.0
        self.duration_s += (out.get("_duration_ms", 0) or 0) / 1000.0
        if m := out.get("_model"):
            self.resolved_model = m

    @property
    def total_tokens(self) -> int:
        return sum(getattr(self, f) for f in self.FIELDS)

    def as_dict(self) -> dict:
        d = {f: getattr(self, f) for f in self.FIELDS}
        # Store the resolved snapshot when we know it, falling back to the alias.
        d.update(model=self.resolved_model or self.model, calls=self.calls,
                 cost_usd=self.cost_usd, duration_s=self.duration_s)
        return d


def _render(rows: list[sqlite3.Row]) -> str:
    """Compact numbered listing. Kept dense on purpose - this is the only part of
    the prompt that scales with item count."""
    lines = []
    for i, r in enumerate(rows, 1):
        bits = [f"{i}. [{r['source_name']}]"]
        if r["signal_label"]:
            bits.append(f"({r['signal_label']})")
        bits.append(r["title"][:220])
        extra = _extra_hint(r)
        if extra:
            bits.append(extra)
        lines.append(" ".join(bits))
    return "\n".join(lines)


def _extra_hint(r: sqlite3.Row) -> str:
    """Surface the few extra fields that genuinely change a judgement."""
    try:
        ex = json.loads(r["extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    hints = []
    if ex.get("ransomware"):
        hints.append("used in ransomware campaigns")
    if ex.get("due"):
        hints.append(f"federal patch deadline {ex['due']}")
    if ex.get("lang"):
        hints.append(ex["lang"])
    if ex.get("pipeline"):
        hints.append(ex["pipeline"])
    return f"<{'; '.join(hints)}>" if hints else ""


def select_finalists(results: list[dict], rows: list) -> list[dict]:
    """Choose the candidate pool for the top-picks pass.

    Two filters, both deterministic on purpose:

    A score floor, so a thin run cannot promote whatever is least bad - a 12-item
    catch-up once put a star-farmed Telegram bot repo in the picks.

    A per-source cap applied BEFORE the model sees the list. Instructing it to
    "favour variety" was not enough: on a heavy security news day four of five
    picks came from the same outlet. Capping the input guarantees the picks span
    sources while the model still chooses freely among them.
    """
    src_of = {r["id"]: r["source_key"] for r in rows}
    ranked = sorted((r for r in results if r["score"] >= TOP_FLOOR),
                    key=lambda x: x["score"], reverse=True)
    per_source: dict[str, int] = {}
    out: list[dict] = []
    for r in ranked:
        src = src_of.get(r["id"], "?")
        if per_source.get(src, 0) >= TOP_PER_SOURCE:
            continue
        per_source[src] = per_source.get(src, 0) + 1
        out.append(r)
        if len(out) >= TOP_CANDIDATES:
            break
    return out


def enrich(con: sqlite3.Connection, rows: list[sqlite3.Row], model: str = "haiku",
           on_progress=None) -> tuple[list[dict], list[str], Usage]:
    """Score and summarize `rows` in batches. Returns (results, errors, usage)."""
    results: list[dict] = []
    errors: list[str] = []
    usage = Usage(model)

    batches = [rows[i:i + BATCH_SIZE] for i in range(0, len(rows), BATCH_SIZE)]
    for bi, batch in enumerate(batches, 1):
        prompt = (
            f"{RUBRIC}\n"
            f"Return one object per item, all {len(batch)} of them, keeping the "
            f"same numbers.\n\nITEMS:\n{_render(batch)}"
        )
        if on_progress:
            on_progress(f"scoring batch {bi}/{len(batches)} ({len(batch)} items)")
        try:
            out = call_claude(prompt, ITEM_SCHEMA, model=model)
        except EnrichError as exc:
            errors.append(f"batch {bi}: {exc}")
            continue
        usage.add(out)

        by_n = {int(o["n"]): o for o in out.get("items", []) if "n" in o}
        missing = 0
        for i, r in enumerate(batch, 1):
            o = by_n.get(i)
            if not o:
                missing += 1
                continue
            results.append({
                "id": r["id"],
                "summary": (o.get("summary") or "").strip()[:300],
                "score": max(0, min(100, int(o.get("score", 0)))),
                "category": o.get("category") or r["category"],
            })
        if missing:
            errors.append(f"batch {bi}: {missing}/{len(batch)} items unscored")

    # Second pass: pick the day's top items from the highest scorers across all
    # batches. Needs global context, so it cannot be folded into the batches.
    # Only genuinely good items are eligible. Without a floor, a thin run (say a
    # 12-item catch-up) promotes whatever happens to be least bad, which is how a
    # star-farmed Telegram bot repo once made the picks.
    finalists = select_finalists(results, rows)
    if finalists:
        by_id = {r["id"]: r for r in rows}
        listing = "\n".join(
            f"{i}. [{by_id[f['id']]['source_name']}] {by_id[f['id']]['title'][:200]}"
            f" — {f['summary']}"
            for i, f in enumerate(finalists, 1) if f["id"] in by_id
        )
        if on_progress:
            on_progress(f"picking top {TOP_N}")
        try:
            out = call_claude(
                f"These are today's highest-scoring items. Choose the {TOP_N} that "
                f"most deserve the reader's attention, most important first. Favour "
                f"variety - do not pick five of the same kind. For each, give one "
                f"sentence on why it matters to them specifically.\n\n{listing}",
                TOP_SCHEMA, model=model)
            usage.add(out)
            for rank, t in enumerate(out.get("top", [])[:TOP_N]):
                idx = int(t.get("n", 0)) - 1
                if 0 <= idx < len(finalists):
                    finalists[idx]["is_top"] = True
                    finalists[idx]["why"] = (t.get("why") or "").strip()[:400]
                    # Keep editor-chosen order stable above everything else.
                    finalists[idx]["score"] = max(finalists[idx]["score"], 100 - rank)
        except EnrichError as exc:
            errors.append(f"top picks: {exc}")

    return results, errors, usage
