# news-aggregator

One terminal feed for tech, AI and cybersecurity — including the parts that
usually only surface on Twitter: new GitHub repos with real traction, HN/Lobsters
front-page discussion, r/LocalLLaMA, Bluesky, and CISA's actively-exploited-vuln
catalogue. Ranked and summarized by `claude -p` against your Claude subscription.

## Use

```bash
./news refresh     # fetch all sources + LLM scoring  (~5 min, run once a day)
./news             # open the reader
./news web         # the same reader, in a browser at localhost:8000
./news stats       # counts, per-source average scores, recent runs
./news sources     # list configured sources
```

Reader keys: `j`/`k` move · `o` open in browser · `m` read · `s` save ·
`c` cycle category (or `1`-`5`) · `u` unread only · `b` saved · `/` search ·
`r` fetch · `q` quit.

## The web reader

`./news web` is not a second frontend. `textual-serve` runs the same `NewsApp`
as a subprocess and streams its output to a terminal emulator in the browser, so
the page is the reader — same layout, same keys, same colours, one codebase. It
takes the same flags as `read` (`-c`, `-u`, `--days`), plus `--host`/`--port`.

`o` opens links in *your* browser rather than the server's, because the reader
calls `App.open_url` and the driver routes it to whoever is looking.

There is no authentication, and each visitor gets their own process against the
same SQLite file — so `--host 0.0.0.0` means anyone on the network can read your
feed, mark items read, and trigger fetches. Keep it on localhost, or put it
behind a tunnel or a reverse proxy that handles auth.

`refresh` is split from `read` deliberately: fetching 29 sources and running the
LLM pass takes minutes, and you don't want that in front of you at 7am. Schedule
it and just read.

```bash
./schedule install     # daily 07:00 LaunchAgent
./schedule status
```

## How ranking works

Two stages. A deterministic `base_score` from popularity signal × source weight ×
recency decides *what is worth spending LLM tokens on*. Then one batched
`claude -p` call scores and summarizes against a rubric written for one specific
reader — AI infrastructure plus security — and a second call picks the day's top
five with a reason each.

The rubric judges substance over popularity, so a 900-point HN post about a
product launch can land below a 60-point post about a novel attack. It works:
"Em dashes are amazing" scored 0, and every CISA KEV entry landed 90+.

`news stats` shows average score per source, which is how you decide what to cut.

## The LLM layer

Uses `claude -p` with the OAuth token already in your login keychain — no API
key, and usage draws against your Claude subscription rather than per-token
billing. The `total_cost_usd` printed on each run is notional API-equivalent, not
a charge.

**Every invocation carries a fixed floor of ~8k tokens** of Claude Code
scaffolding. Measured, and not reducible via `--setting-sources ""` or
`--disable-slash-commands`. That single fact drives the whole design:

| approach | overhead/day |
| --- | --- |
| one call per item (~700 items) | ~5.6M tokens |
| batched, 80 items per call | ~25k tokens |

So the unit of work is a batch, never an item. If you extend this, keep it that
way.

Flags that matter, and why:

- `--tools ""` — pure inference; no agentic loop, no file access.
- `--json-schema` — validated structured output, returned in the
  `structured_output` field (`result` comes back empty).
- `--system-prompt` — replaces the default Claude Code prompt.
- `--max-budget-usd` — a runaway loop can't quietly burn your limits.
- **Never `--bare`.** Its own help text: *"Anthropic auth is strictly
  ANTHROPIC_API_KEY or apiKeyHelper ... OAuth and keychain are never read."* It
  silently breaks the one thing this design depends on.
- `env -u CLAUDECODE` (handled in `enrich.py`) — Claude Code refuses to launch
  nested inside another Claude Code session.

Change the model with `./news refresh --model sonnet`. Haiku is the default and
is good enough for scoring; the reasoning shows up mostly in the top-five picks.

## Token accounting

Every `refresh` prints a breakdown, and `./news usage` shows history plus
rolling totals:

```
  tokens (2 claude -p calls, model haiku)
                tokens   $/MTok      cost  of cost
    input           46     1.00    0.0000       0%
    cache write  26.1k     1.25    0.0327      27%
    cache read   26.7k     0.10    0.0027       2%
    output       16.8k     5.00    0.0842      70%
    total        69.7k             0.1196
```

The table shows share of **cost**, not share of tokens, because those tell
opposite stories: output is a quarter of the tokens and 70% of the cost, since
it bills at 5× the input rate. If you want a run cheaper, shortening what the
model *writes* beats shrinking what it reads.

Rates used (Haiku 4.5, per million tokens): **$1.00 input, $5.00 output**, cache
writes at **1.25×** input, cache reads at **0.10×** input. That arithmetic
reproduces the `total_cost_usd` the CLI reports itself, exactly — `tests/test_logic.py`
pins it against a real envelope. Note the reported figure prices cache writes at
1.25× even when the usage block says the entry has a 1-hour TTL (which lists at 2×).

**Nothing here is a charge.** The run authenticates with your OAuth session, so
it consumes Claude subscription usage rather than being billed per token. The
dollar figures are what the same tokens *would* have cost on the API — useful for
comparing runs and models, not a bill.

**Subscription usage is not readable from a script.** Claude Code's limits are
measured in usage windows rather than tokens or dollars, and no quota data is
exposed to `claude -p` or written anywhere local (checked: no rate-limit or
quota fields in the config, `stats-cache.json`, or session transcripts). For
remaining quota, run `/usage` inside Claude Code. This tool reports what it can
actually measure and doesn't guess at a percentage.

Batch size is the main lever on total spend, because the ~8k-token floor
amortizes: a 160-item run cost ~$0.22 (≈$0.0014/item) where a 30-item run cost
~$0.12 (≈$0.004/item). Fewer, bigger runs are cheaper per item.

## Sources

29 sources, all verified reachable. Notes on the ones that fight back:

| source | quirk |
| --- | --- |
| Reddit | 403s generic user-agents; rate-limits to ~1 request per run per IP. All three subreddits go through **one** multireddit request (`/r/a+b+c/.rss`); `<category label>` preserves per-sub routing. |
| tl;dr sec | `/feed` and `/rss` return the SPA's HTML shell with status **200**. Only `/feed.xml` is real. `fetch.py` content-checks the body for exactly this. |
| Krebs | serves valid RSS under `content-type: text/html`. Judge the body, not the header. |
| BleepingComputer | 403s unless the user-agent looks like a browser. |
| GitHub trending | no API; HTML scraped. `href` is not the first attribute on the anchor. Raises loudly if it parses 0 repos, rather than silently returning nothing. |
| arXiv | genuinely empty on weekends (it ships a `skipDays` element). Zero items is not an error. |
| Bluesky | `public.api.bsky.app` needs no auth at all. Verify a handle before adding: `./news check-handle <handle>`. |
| CISA KEV | records repeat themselves ("Langflow Langflow — Langflow ... Vulnerability"); `_kev_title` collapses that. |

Twitter/X itself is deliberately absent: Nitter is dead (every public instance
returns an empty body) and the official API is $200/mo. Bluesky, HN, Lobsters,
r/LocalLLaMA and the curated newsletters cover the same ground — anything
genuinely interesting on AI Twitter reaches HN within hours.

Add a source in `agg/sources.py`. If it's a plain feed, `kind="rss"` and a
`weight` is the whole job. **Check the response body, not the status code** — see
the tl;dr sec row above.

## Dedupe

Near-duplicate titles cluster across sources via Jaccard overlap on meaningful
words (threshold 0.62); one representative renders and absorbs the others' source
names. Two rules, both added after they produced real false positives:

- **Jaccard over the union**, not over the smaller set. `min()` makes any short
  title contained in a longer one score 1.0.
- **Never merge two items from the same source.** A feed publishing twice means
  two different things — consecutive Latent Space issues share a boilerplate
  title, and successive HuggingFace models differ by one character.

Measured on 688 real items this yields exactly **one** merge. Cross-source
duplicates are genuinely rare here; an aggressive threshold destroys real items
rather than tidying up. After changing any of this, run `./news recluster` —
past decisions are baked into `cluster_id` and wrongly-merged items stay hidden
until reassigned.

`store.diversify()` then breaks runs of more than 3 items from one source, so a
heavy KEV day doesn't bury everything under fifteen consecutive CVEs.

## Layout

```
agg/sources.py   declarative source registry (start here)
agg/fetch.py     one fetcher per source kind; never raises, collects errors
agg/store.py     sqlite, url normalization, clustering, ranking
agg/enrich.py    batched claude -p calls
agg/tui.py       Textual reader
agg/cli.py       argparse entry point
data/items.db    items, read state, run history
```

`repos` is a **view over `source_key`, not a topic.** The LLM assigns every item a
real subject, so an exploit PoC repo files under security and an inference engine
under ai; the repos view selects on where an item came from. Don't collapse those
two axes back together.
