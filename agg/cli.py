"""Command line entry point: news [refresh|read|stats|sources|check]."""

from __future__ import annotations

import argparse
import shlex
import sys
import time

from . import store
from .enrich import (BATCH_SIZE, CACHE_READ_MULT, CACHE_WRITE_MULT,
                     EnrichError, enrich, price_for)
from .fetch import fetch_all
from .sources import enabled

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"
GREEN, RED, YELLOW, CYAN = "\033[32m", "\033[31m", "\033[33m", "\033[36m"


def cmd_refresh(args: argparse.Namespace) -> int:
    started = time.time()
    con = store.connect()

    print(f"{BOLD}fetching{RESET} {len(enabled())} sources…")
    ok = fail = 0

    def prog(src, n):
        nonlocal ok, fail
        if n is None:
            fail += 1
            print(f"  {RED}✗{RESET} {src.key}")
        else:
            ok += 1
            colour = DIM if n == 0 else ""
            print(f"  {GREEN}✓{RESET} {colour}{src.key:<16} {n}{RESET}")

    items, errors = fetch_all(on_progress=prog)
    print(f"\n{ok} sources ok, {fail} failed — {len(items)} items")

    clustered = store.cluster(items)
    fresh = store.upsert(con, clustered)
    reps = [i for i in fresh if i.cluster_id is None or i.cluster_id == i.id]
    print(f"{len(fresh)} new ({len(reps)} after dedupe)")

    enriched = 0
    usage = None
    if args.no_llm:
        print(f"{DIM}skipping LLM pass (--no-llm){RESET}")
    else:
        todo = store.needs_enrichment(con, limit=args.limit)
        if not todo:
            print("nothing to enrich")
        else:
            n_batches = (len(todo) + BATCH_SIZE - 1) // BATCH_SIZE
            print(f"\n{BOLD}enriching{RESET} {len(todo)} items "
                  f"({n_batches} batch{'es' if n_batches > 1 else ''} + top picks)")
            try:
                results, errs, usage = enrich(
                    con, todo, model=args.model,
                    on_progress=lambda m: print(f"  {DIM}{m}{RESET}"))
                enriched = store.save_enrichment(con, results)
                errors.extend(errs)
                print(f"  {GREEN}✓{RESET} {enriched} enriched")
                print_usage(usage, enriched)
            except EnrichError as exc:
                errors.append(str(exc))
                print(f"  {RED}✗ {exc}{RESET}")

    store.record_run(con, started, len(items), len(fresh), enriched,
                     "; ".join(errors),
                     usage.as_dict() if usage else None)

    if errors:
        print(f"\n{YELLOW}{len(errors)} issue(s):{RESET}")
        for e in errors:
            print(f"  - {e}")

    print(f"\ndone in {time.time() - started:.1f}s — run {BOLD}news read{RESET}")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    from .tui import run_tui
    run_tui(category=args.category, unread_only=args.unread, days=args.days)
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    """Serve the reader over HTTP.

    Not a reimplementation: textual-serve runs the same NewsApp as a subprocess
    and streams its output to a terminal emulator in the browser, so the web
    reader is the terminal reader — same layout, keys and colours.
    """
    try:
        from textual_serve.server import Server
    except ModuleNotFoundError:
        print(f"{RED}✗{RESET} textual-serve is not installed — "
              f"{BOLD}.venv/bin/pip install textual-serve{RESET}", file=sys.stderr)
        return 1

    # sys.executable, not "news": the served command is run by the server with a
    # bare environment, and this pins it to the same venv without a PATH lookup.
    cmd = [shlex.quote(sys.executable), "-m", "agg.cli", "read",
           "--days", str(args.days)]
    if args.category:
        cmd += ["-c", args.category]
    if args.unread:
        cmd.append("-u")

    if args.host not in ("localhost", "127.0.0.1"):
        # Worth stating plainly: there is no auth layer, and every visitor drives
        # a real process on this machine (r refetches, o marks items read).
        print(f"{YELLOW}!{RESET} serving on {args.host} — anyone who can reach "
              f"this port gets the reader, with no password")
    print(f"{BOLD}news{RESET} on {CYAN}http://{args.host}:{args.port}{RESET} "
          f"{DIM}— ctrl-c to stop{RESET}")

    Server(" ".join(cmd), host=args.host, port=args.port,
           title="news").serve()
    return 0


def _k(n: float) -> str:
    """Compact token count: 1234 -> 1.2k, 1234567 -> 1.23M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def print_usage(usage, enriched: int) -> None:
    """Per-run token and cost breakdown."""
    total = usage.total_tokens
    p = price_for(usage.resolved_model or usage.model)
    shown = usage.resolved_model or usage.model
    print(f"\n  {BOLD}tokens{RESET} {DIM}({usage.calls} claude -p calls, "
          f"{shown}){RESET}")
    # Rate per MTok per bucket. Showing the share of COST rather than the share
    # of tokens is what makes this table actionable: output is a small slice of
    # the token count but bills at 5x the input rate, so it usually dominates.
    rows = [
        ("input", usage.input_tokens, p["in"]),
        ("cache write", usage.cache_creation_tokens, p["in"] * CACHE_WRITE_MULT),
        ("cache read", usage.cache_read_tokens, p["in"] * CACHE_READ_MULT),
        ("output", usage.output_tokens, p["out"]),
    ]
    costs = [(label, n, rate, n * rate / 1_000_000) for label, n, rate in rows]
    subtotal = sum(c for *_, c in costs) or 1e-12

    print(f"    {DIM}{'':<12}{'tokens':>8}{'$/MTok':>9}{'cost':>10}"
          f"{'of cost':>9}{RESET}")
    for label, n, rate, cost in costs:
        print(f"    {label:<12}{_k(n):>8}{rate:>9.2f}{cost:>10.4f}"
              f"{100 * cost / subtotal:>8.0f}%")
    print(f"    {BOLD}{'total':<12}{_k(total):>8}{'':>9}{subtotal:>10.4f}{RESET}")

    per_item = total / enriched if enriched else 0
    print(f"\n  {BOLD}cost{RESET}  {CYAN}${usage.cost_usd:.4f}{RESET} "
          f"{DIM}API-equivalent{RESET}")
    if enriched:
        print(f"    {DIM}{_k(per_item)} tokens/item · "
              f"${usage.cost_usd / enriched * 1000:.2f} per 1000 items{RESET}")
    # The critical caveat: this figure is what the same tokens would have cost
    # on the API. Nothing is charged - the run draws against the subscription's
    # usage limits instead, and those are not denominated in tokens or dollars.
    print(f"    {DIM}Not billed. Draws against your Claude subscription's usage "
          f"limits{RESET}")
    print(f"    {DIM}Check remaining quota with {RESET}{BOLD}/usage{RESET}"
          f"{DIM} inside Claude Code{RESET}")


def cmd_usage(args: argparse.Namespace) -> int:
    con = store.connect()
    hist = store.usage_history(con, limit=args.limit)
    if not hist or not any(r["llm_calls"] for r in hist):
        print("no LLM usage recorded yet — run `news refresh`")
        return 0

    print(f"{BOLD}{'when':<14}{'items':>6}{'calls':>6}{'tokens':>9}"
          f"{'cost':>10}  {'model'}{RESET}")
    for r in hist:
        if not r["llm_calls"]:
            continue
        total = (r["input_tokens"] + r["cache_creation_tokens"]
                 + r["cache_read_tokens"] + r["output_tokens"])
        when = time.strftime("%m-%d %H:%M", time.localtime(r["started_at"]))
        print(f"{when:<14}{r['enriched']:>6}{r['llm_calls']:>6}"
              f"{_k(total):>9}{'$' + format(r['cost_usd'], '.4f'):>10}"
              f"  {DIM}{r['model'] or '-'}{RESET}")

    for label, days in (("last 24h", 1), ("last 7d", 7), ("all time", None)):
        t = store.usage_totals(con, days)
        if not t["calls"]:
            continue
        total = (t["input_tokens"] + t["cache_creation_tokens"]
                 + t["cache_read_tokens"] + t["output_tokens"])
        plural = "run" if t["runs"] == 1 else "runs"
        print(f"\n{BOLD}{label}{RESET}  {t['runs']} {plural} · {t['calls']} calls · "
              f"{_k(total)} tokens · {CYAN}${t['cost_usd']:.4f}{RESET} "
              f"{DIM}API-equivalent{RESET}")
        if label == "last 7d" and t["runs"]:
            # Per RUN, not per day - dividing a week's spend by 7 would be wrong
            # after several runs in one day. The monthly figure then assumes the
            # intended cadence of one scheduled run per day.
            per_run = t["cost_usd"] / t["runs"]
            print(f"  {DIM}~${per_run:.4f}/run → ~${per_run * 30:.2f}/month at "
                  f"one run a day, if this were billed{RESET}")

    print(f"\n{DIM}These are API-equivalent prices for the tokens used, not "
          f"charges.{RESET}")
    print(f"{DIM}Runs authenticate with your OAuth session, so they consume "
          f"subscription{RESET}")
    print(f"{DIM}usage limits instead. Those limits are measured in usage "
          f"windows, not tokens,{RESET}")
    print(f"{DIM}and are not exposed to scripts — run {RESET}{BOLD}/usage"
          f"{RESET}{DIM} in Claude Code to see what is left.{RESET}")
    return 0


def cmd_recluster(args: argparse.Namespace) -> int:
    con = store.connect()
    rows, merged = store.recluster(con)
    print(f"reclustered {rows} items — {merged} now marked as duplicates")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    con = store.connect()
    total = con.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    unread = con.execute(
        "SELECT COUNT(*) c FROM items WHERE read_at IS NULL "
        "AND (cluster_id IS NULL OR cluster_id = id)").fetchone()["c"]
    enriched = con.execute(
        "SELECT COUNT(*) c FROM items WHERE enriched_at IS NOT NULL").fetchone()["c"]
    print(f"{BOLD}{total}{RESET} items  {BOLD}{unread}{RESET} unread  "
          f"{BOLD}{enriched}{RESET} enriched")

    print(f"\n{BOLD}by category{RESET}")
    for r in con.execute(
        """SELECT COALESCE(llm_category, category) cat, COUNT(*) c FROM items
           WHERE cluster_id IS NULL OR cluster_id = id
           GROUP BY cat ORDER BY c DESC"""):
        print(f"  {r['cat']:<10} {r['c']}")

    print(f"\n{BOLD}top sources (7d){RESET}")
    for r in con.execute(
        """SELECT source_name, COUNT(*) c, ROUND(AVG(COALESCE(llm_score,0)),1) avg
           FROM items WHERE first_seen > ? GROUP BY source_name
           ORDER BY avg DESC, c DESC LIMIT 12""", (time.time() - 7 * 86400,)):
        print(f"  {r['source_name']:<22} {r['c']:>4} items  avg score {r['avg']}")

    print(f"\n{BOLD}recent runs{RESET}")
    for r in con.execute("SELECT * FROM runs ORDER BY started_at DESC LIMIT 5"):
        when = time.strftime("%m-%d %H:%M", time.localtime(r["started_at"]))
        errs = f"  {YELLOW}{r['errors'][:60]}{RESET}" if r["errors"] else ""
        print(f"  {when}  {r['fetched']:>4} fetched  {r['new_items']:>3} new  "
              f"{r['enriched']:>3} enriched{errs}")
    return 0


def cmd_sources(args: argparse.Namespace) -> int:
    con = store.connect()
    counts = {r["source_key"]: r["c"] for r in con.execute(
        "SELECT source_key, COUNT(*) c FROM items GROUP BY source_key")}
    print(f"{BOLD}{'key':<16}{'kind':<12}{'cat':<10}{'wt':<6}stored{RESET}")
    for s in enabled():
        print(f"{s.key:<16}{s.kind:<12}{s.category:<10}{s.weight:<6}"
              f"{counts.get(s.key, 0)}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Verify one Bluesky handle resolves and has posts, before adding it."""
    import httpx
    r = httpx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
                  params={"actor": args.handle, "limit": 3}, timeout=15)
    if r.status_code != 200:
        print(f"{RED}✗{RESET} {args.handle}: HTTP {r.status_code} "
              f"{r.json().get('error', '') if r.text.startswith('{') else ''}")
        return 1
    feed = r.json().get("feed", [])
    if not feed:
        print(f"{YELLOW}~{RESET} {args.handle}: resolves but the feed is empty")
        return 1
    print(f"{GREEN}✓{RESET} {args.handle}: {len(feed)} recent posts")
    for e in feed:
        text = " ".join((e.get("post", {}).get("record", {}).get("text") or "").split())
        print(f"    {DIM}{text[:90]}{RESET}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="news", description="Tech/AI/security aggregator")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="fetch all sources and enrich")
    r.add_argument("--no-llm", action="store_true",
                   help="fetch only; skip the claude -p pass")
    r.add_argument("--model", default="haiku", help="claude model alias (default: haiku)")
    r.add_argument("--limit", type=int, default=160,
                   help="max items to enrich this run (default: 160)")
    r.set_defaults(func=cmd_refresh)

    d = sub.add_parser("read", help="open the reader")
    d.add_argument("-c", "--category", choices=["ai", "security", "tech", "repos"])
    d.add_argument("-u", "--unread", action="store_true")
    d.add_argument("--days", type=int, default=7)
    d.set_defaults(func=cmd_read)

    w = sub.add_parser("web", help="serve the reader in a browser")
    w.add_argument("-c", "--category", choices=["ai", "security", "tech", "repos"])
    w.add_argument("-u", "--unread", action="store_true")
    w.add_argument("--days", type=int, default=7)
    w.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 to expose on the LAN (unauthenticated)")
    w.add_argument("--port", type=int, default=8000)
    w.set_defaults(func=cmd_web)

    us = sub.add_parser("usage", help="token spend per run and cumulative")
    us.add_argument("-n", "--limit", type=int, default=20,
                    help="how many recent runs to list (default: 20)")
    us.set_defaults(func=cmd_usage)

    sub.add_parser("recluster", help="recompute dedupe over stored items"
                   ).set_defaults(func=cmd_recluster)
    sub.add_parser("stats", help="show store stats").set_defaults(func=cmd_stats)
    sub.add_parser("sources", help="list configured sources").set_defaults(func=cmd_sources)

    c = sub.add_parser("check-handle", help="verify a bluesky handle")
    c.add_argument("handle")
    c.set_defaults(func=cmd_check)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
