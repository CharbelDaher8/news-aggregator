"""Tests for the pure logic — run with: ./.venv/bin/python tests/test_logic.py

Every case here corresponds to a bug that actually occurred during development,
not a hypothetical. No network, no LLM, no database writes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agg.enrich import (TOP_PER_SOURCE, Usage, estimate_cost,  # noqa: E402
                        price_for, select_finalists)
from agg.fetch import _kev_title  # noqa: E402
from agg.store import Item, cluster, diversify, normalize_url, title_key  # noqa: E402

PASS = FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got:  {got!r}\n         want: {want!r}")


def item(title: str, source: str, url: str | None = None, signal: float = 0.0) -> Item:
    return Item(url=url or f"https://example.com/{abs(hash((title, source)))}",
                title=title, source_key=source, source_name=source,
                category="tech", signal=signal)


# ---------------------------------------------------------------- url identity
print("normalize_url")
check("strips tracking params",
      normalize_url("https://x.com/a?utm_source=hn&id=3"),
      normalize_url("https://x.com/a?id=3"))
check("www and scheme are not identity",
      normalize_url("http://www.x.com/a/"), normalize_url("https://x.com/a"))
check("arxiv pdf == abs, version-insensitive",
      normalize_url("https://arxiv.org/pdf/2401.001v2"),
      normalize_url("https://arxiv.org/abs/2401.001"))
check("keeps meaningful query params",
      normalize_url("https://x.com/a?id=3") != normalize_url("https://x.com/a?id=4"), True)
check("non-default port is identity",
      normalize_url("https://x.com:8443/a") != normalize_url("https://x.com/a"), True)

# ------------------------------------------------------------------- clustering
print("\ncluster")
# The real false positives that motivated Jaccard-over-union.
c = cluster([item("Flux 3", "hn"),
             item("Flux 3 is an open weights image model from BFL", "latent")])
check("short title does not absorb a longer one (min() bug)",
      sum(1 for i in c if i.cluster_id == i.id), 2)

c = cluster([item("[AINews] not much happened today", "latent"),
             item("[AINews] not much happened today", "latent")])
check("same source never merges (consecutive newsletter issues)",
      sum(1 for i in c if i.cluster_id == i.id), 2)

c = cluster([item("poolside/Laguna-S-2.1", "hf_models"),
             item("poolside/Laguna-M-2.1", "hf_models")])
check("identifier sources never merge on name overlap",
      sum(1 for i in c if i.cluster_id == i.id), 2)

c = cluster([item("Claude Opus 5", "hn", signal=500),
             item("Introducing Claude Opus 5", "simonw")])
reps = [i for i in c if i.cluster_id == i.id]
check("genuine cross-source duplicate does merge", len(reps), 1)
check("higher-signal member represents the cluster", reps[0].source_key, "hn")
check("representative records the absorbed source", reps[0].dupes, ["simonw"])

# ------------------------------------------------------------------- diversify
print("\ndiversify")


class Row(dict):
    """Stands in for sqlite3.Row, which supports __getitem__ only."""


def row(src: str, top: int = 0) -> Row:
    return Row(source_key=src, is_top=top)


seq = [row("kev") for _ in range(6)] + [row("hn") for _ in range(2)]
out = [r["source_key"] for r in diversify(seq, max_run=3)]
longest = best = 0
prev = None
for s in out:
    longest = longest + 1 if s == prev else 1
    best = max(best, longest)
    prev = s
check("breaks runs longer than max_run", best <= 3, True)
check("loses no items", len(out), len(seq))
check("top picks stay first",
      [r["source_key"] for r in diversify([row("a"), row("b", top=1)], max_run=3)],
      ["b", "a"])

# ------------------------------------------------------------------- kev titles
print("\n_kev_title")
check("collapses duplicated vendor/product and drops boilerplate",
      _kev_title("CVE-1", "Langflow", "Langflow",
                 "Langflow Inclusion of Functionality Vulnerability"),
      "CVE-1: Langflow — Inclusion of Functionality")
check("keeps distinct vendor and product",
      _kev_title("CVE-2", "Microsoft", "SharePoint",
                 "Microsoft SharePoint Deserialization of Untrusted Data Vulnerability"),
      "CVE-2: Microsoft SharePoint — Deserialization of Untrusted Data")
check("survives an empty vulnerability name",
      _kev_title("CVE-3", "Acme", "Acme", ""), "CVE-3: Acme")

# -------------------------------------------------------------- top-pick pool
print("\nselect_finalists")
rows = ([Row(id=f"t{i}", source_key="thn") for i in range(8)]
        + [Row(id=f"k{i}", source_key="kev") for i in range(8)]
        + [Row(id="s1", source_key="simonw")])
results = ([{"id": f"t{i}", "score": 99} for i in range(8)]
           + [{"id": f"k{i}", "score": 98} for i in range(8)]
           + [{"id": "s1", "score": 75}])
fin = select_finalists(results, rows)
counts: dict[str, int] = {}
src_of = {r["id"]: r["source_key"] for r in rows}
for f in fin:
    counts[src_of[f["id"]]] = counts.get(src_of[f["id"]], 0) + 1
check("caps candidates per source", max(counts.values()) <= TOP_PER_SOURCE, True)
check("still spans sources", len(counts), 3)
check("excludes anything below the score floor",
      select_finalists([{"id": "s1", "score": 40}], rows), [])

# ------------------------------------------------------------------- title_key
print("\ntitle_key")
check("keeps content words, drops stopwords",
      title_key("How to deploy the New Langflow Server"),
      frozenset({"deploy", "langflow", "server"}))
check("a title of only stopwords and short tokens reduces to nothing",
      title_key("How to use the New AI"), frozenset())

# ------------------------------------------------------------------ token cost
print("\nusage accounting")
# Pinned against a real `claude -p` envelope: Haiku 4.5 at $1/MTok in and
# $5/MTok out, cache writes 1.25x input and cache reads 0.10x input, reproduced
# the CLI's own total_cost_usd of 0.01352325 exactly.
observed = {"input_tokens": 3887, "cache_creation_tokens": 4593,
            "cache_read_tokens": 0, "output_tokens": 779}
check("reproduces claude -p's reported cost to the cent",
      round(estimate_cost(observed, "haiku"), 8), 0.01352325)
check("a bare in/out call also matches exactly",
      round(estimate_cost({"input_tokens": 3622, "output_tokens": 39}, "haiku"), 8),
      0.00381700)
check("output is priced 5x input",
      estimate_cost({"output_tokens": 1000}, "haiku"),
      estimate_cost({"input_tokens": 1000}, "haiku") * 5)
check("cache reads are a tenth of input",
      round(estimate_cost({"cache_read_tokens": 1_000_000}, "haiku"), 6), 0.10)

# price_for must handle resolved snapshots, not just aliases: splitting
# "claude-haiku-4-5-20251001" on "-" yields "claude", which matches no tier and
# would silently price every model at the Haiku fallback rate.
check("resolves rates from a dated snapshot ID",
      price_for("claude-haiku-4-5-20251001"), price_for("haiku"))
check("does not collapse sonnet to the haiku fallback",
      price_for("claude-sonnet-5")["out"], 15.00)
check("does not collapse opus to the haiku fallback",
      price_for("claude-opus-5")["out"], 25.00)
check("falls back safely on an unknown model", price_for(None), price_for("haiku"))

u = Usage("haiku")
u.add({"_usage": {"input_tokens": 10, "cache_creation_input_tokens": 20,
                  "cache_read_input_tokens": 30, "output_tokens": 40},
       "_cost": 0.5, "_duration_ms": 1500})
u.add({"_usage": {"input_tokens": 1, "output_tokens": 2}, "_cost": 0.25})
check("accumulates across calls", (u.calls, u.total_tokens), (2, 103))
check("maps the envelope's cache_*_input_tokens names",
      (u.cache_creation_tokens, u.cache_read_tokens), (20, 30))
check("sums reported cost rather than re-deriving it", u.cost_usd, 0.75)
check("converts duration to seconds", u.duration_s, 1.5)
check("survives a usage block with missing keys",
      Usage("haiku").add({"_usage": {}}) or True, True)

# --- the JSON API ------------------------------------------------------------
#
# The wire shape, not the HTTP. Whether a socket accepts a connection is not
# where this breaks; what breaks is a field quietly renamed or dropped, because
# the reader on the other side is in another language and another repository and
# finds out at runtime.

import sqlite3  # noqa: E402
from agg.api import _flag, _int, item_json  # noqa: E402

_row = sqlite3.connect(":memory:")
_row.row_factory = sqlite3.Row
_row.execute("CREATE TABLE t (id TEXT, url TEXT, title TEXT, source_name TEXT, "
             "source_key TEXT, category TEXT, llm_category TEXT, author TEXT, "
             "published REAL, first_seen REAL, signal REAL, signal_label TEXT, "
             "summary TEXT, llm_score INTEGER, is_top INTEGER, top_reason TEXT, "
             "read_at REAL, saved INTEGER)")
_row.execute("INSERT INTO t VALUES ('abc','http://x','T','HN','hn','tech','ai',"
             "'me',1.0,2.0,42.0,'42 pts','sum',88,1,'why',NULL,1)")
sample = _row.execute("SELECT * FROM t").fetchone()
wire = item_json(sample)

check("publishes the fields the client reads",
      set(wire) >= {"id", "url", "title", "source", "category", "summary",
                    "score", "read", "saved", "isTop"}, True)
check("prefers the LLM's category over the fetcher's", wire["category"], "ai")
check("reports read as a boolean, not a timestamp", wire["read"], False)
check("reports saved as a boolean", wire["saved"], True)
check("reports the LLM score it was given", wire["score"], 88)

# "not scored yet" and "scored zero" are different states and the client renders
# them differently, so null has to survive the trip as null.
_row.execute("INSERT INTO t VALUES ('def','http://y','U','Lobsters','lob','tech',"
             "NULL,NULL,NULL,3.0,0.0,'',NULL,NULL,0,NULL,NULL,0)")
unscored = item_json(_row.execute("SELECT * FROM t WHERE id='def'").fetchone())
check("keeps an unscored item's score null rather than zero", unscored["score"], None)
check("falls back to the fetcher's category when the LLM has not run",
      unscored["category"], "tech")
check("has no summary before the LLM pass", unscored["summary"], None)
# `extra` is a private arrangement between fetch.py and the reader; publishing
# it would make a source's internal blob part of an external contract.
check("does not publish the source-specific blob", "extra" in wire, False)

check("clamps a limit above the ceiling", _int({"limit": ["9999"]}, "limit", 100, 1, 500), 500)
check("falls back on a non-numeric limit", _int({"limit": ["all"]}, "limit", 100, 1, 500), 100)
check("reads a flag as set", _flag({"unread": ["1"]}, "unread"), True)
check("treats an absent flag as unset", _flag({}, "unread"), False)
check("treats an explicit false as unset", _flag({"unread": ["false"]}, "unread"), False)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
