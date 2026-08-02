"""SQLite-backed item store.

Holds three things worth persisting: which items we have already seen (so a
daily run only pays the LLM for new ones), read/saved state, and the enrichment
output. Everything else is recomputed each run.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "items.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            TEXT PRIMARY KEY,   -- hash of normalized url
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    source_key    TEXT NOT NULL,
    source_name   TEXT NOT NULL,
    category      TEXT NOT NULL,
    author        TEXT,
    published     REAL,               -- unix ts, best effort
    first_seen    REAL NOT NULL,
    signal        REAL DEFAULT 0,     -- raw popularity number (points/stars/ups)
    signal_label  TEXT,               -- how to render it, e.g. "842 pts"
    extra         TEXT,               -- source-specific blob (comments url, cve id)
    base_score    REAL DEFAULT 0,
    -- enrichment (null until the LLM pass runs)
    summary       TEXT,
    llm_score     INTEGER,
    llm_category  TEXT,
    is_top        INTEGER DEFAULT 0,
    top_reason    TEXT,
    enriched_at   REAL,
    -- user state
    read_at       REAL,
    saved         INTEGER DEFAULT 0,
    -- dedupe
    cluster_id    TEXT
);
CREATE INDEX IF NOT EXISTS idx_first_seen ON items(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_cluster    ON items(cluster_id);
CREATE INDEX IF NOT EXISTS idx_unread     ON items(read_at) WHERE read_at IS NULL;

CREATE TABLE IF NOT EXISTS runs (
    started_at  REAL PRIMARY KEY,
    fetched     INTEGER,
    new_items   INTEGER,
    enriched    INTEGER,
    errors      TEXT
);
"""

# Added after the first release, so they arrive via ALTER TABLE rather than in
# SCHEMA above (CREATE TABLE IF NOT EXISTS never revisits an existing table).
RUN_USAGE_COLUMNS = [
    ("model", "TEXT"),
    ("llm_calls", "INTEGER DEFAULT 0"),
    ("input_tokens", "INTEGER DEFAULT 0"),
    ("cache_creation_tokens", "INTEGER DEFAULT 0"),
    ("cache_read_tokens", "INTEGER DEFAULT 0"),
    ("output_tokens", "INTEGER DEFAULT 0"),
    ("cost_usd", "REAL DEFAULT 0"),
    ("duration_s", "REAL DEFAULT 0"),
]


def _migrate(con: sqlite3.Connection) -> None:
    existing = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    for name, decl in RUN_USAGE_COLUMNS:
        if name not in existing:
            con.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")
    con.commit()

# Tracking params that change per-referrer but not per-article. Stripping these
# is what makes the same story from HN, Lobsters and a newsletter collapse into
# one cluster instead of three near-identical rows.
_TRACKING = re.compile(
    r"^(utm_[a-z_]+|ref|ref_src|source|fbclid|gclid|mc_cid|mc_eid|s|t|__s|"
    r"at_medium|at_campaign|share|sh)$",
    re.I,
)


def normalize_url(url: str) -> str:
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()

    host = (p.hostname or "").lower().removeprefix("www.")
    # Keep non-default ports; they are part of identity.
    if p.port and not ((p.scheme == "https" and p.port == 443) or (p.scheme == "http" and p.port == 80)):
        host = f"{host}:{p.port}"

    path = re.sub(r"/+", "/", p.path).rstrip("/") or "/"
    # AMP and print variants are the same article.
    path = re.sub(r"/amp$|\.amp$|/print$", "", path)

    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not _TRACKING.match(k)]
    # arxiv abs/pdf are the same paper; canonicalize to abs.
    if host == "arxiv.org":
        path = path.replace("/pdf/", "/abs/")
        path = re.sub(r"v\d+$", "", path)

    return urlunsplit(("https", host, path, urlencode(sorted(q)), ""))


def item_id(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:16]


_STOP = {
    "the", "a", "an", "of", "to", "in", "for", "and", "or", "on", "with", "is",
    "are", "how", "why", "your", "you", "we", "i", "at", "by", "from", "it",
    "its", "this", "that", "new", "show", "hn", "using", "use", "into", "be",
}


def title_key(title: str) -> frozenset[str]:
    """Bag of meaningful words, for cross-source near-duplicate detection."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return frozenset(w for w in words if w not in _STOP and len(w) > 2)


@dataclass
class Item:
    url: str
    title: str
    source_key: str
    source_name: str
    category: str
    author: str | None = None
    published: float | None = None
    signal: float = 0.0
    signal_label: str = ""
    extra: str = ""
    base_score: float = 0.0
    # populated by enrich
    summary: str | None = None
    llm_score: int | None = None
    llm_category: str | None = None
    is_top: bool = False
    top_reason: str | None = None
    # populated by store
    id: str = ""
    first_seen: float = field(default_factory=time.time)
    read_at: float | None = None
    saved: bool = False
    cluster_id: str | None = None
    dupes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = item_id(self.url)
        self.title = " ".join(self.title.split())[:400]


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # WAL, because `news web` is multi-process: textual-serve spawns one NewsApp
    # per visitor, all against this file. Under the default rollback journal a
    # reader blocks a writer, so marking an item read during a refresh — which
    # holds write locks for minutes — dies on "database is locked". WAL lets the
    # two proceed concurrently; the timeout covers the write-write case.
    con = sqlite3.connect(path, timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def upsert(con: sqlite3.Connection, items: list[Item]) -> list[Item]:
    """Insert items, returning only the ones not already known.

    Popularity signals are refreshed for existing rows (an HN post gains points
    after we first see it) but enrichment and read state are left alone.
    """
    known = {r["id"] for r in con.execute("SELECT id FROM items")}
    fresh = []
    for it in items:
        if it.id in known:
            # Refresh the title too: feeds edit headlines, and a change to our
            # own title formatting should reach rows already stored.
            con.execute(
                "UPDATE items SET signal=MAX(signal,?), signal_label=?, "
                "base_score=?, title=? WHERE id=?",
                (it.signal, it.signal_label, it.base_score, it.title, it.id),
            )
            continue
        con.execute(
            """INSERT OR IGNORE INTO items
               (id,url,title,source_key,source_name,category,author,published,
                first_seen,signal,signal_label,extra,base_score,cluster_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (it.id, it.url, it.title, it.source_key, it.source_name, it.category,
             it.author, it.published, it.first_seen, it.signal, it.signal_label,
             it.extra, it.base_score, it.cluster_id),
        )
        known.add(it.id)
        fresh.append(it)
    con.commit()
    return fresh


def save_enrichment(con: sqlite3.Connection, rows: list[dict],
                    clear_top: bool = True) -> int:
    n = 0
    now = time.time()
    if clear_top:
        # is_top means "picked for the current briefing". Without clearing, every
        # run's picks pile up and the star stops meaning anything - two runs made
        # nine top picks.
        con.execute("UPDATE items SET is_top=0, top_reason=NULL WHERE is_top=1")
    for r in rows:
        cur = con.execute(
            """UPDATE items SET summary=?, llm_score=?, llm_category=?,
                                is_top=?, top_reason=?, enriched_at=?
               WHERE id=?""",
            (r.get("summary"), r.get("score"), r.get("category"),
             1 if r.get("is_top") else 0, r.get("why"), now, r["id"]),
        )
        n += cur.rowcount
    con.commit()
    return n


def needs_enrichment(con: sqlite3.Connection, limit: int = 160) -> list[sqlite3.Row]:
    return list(con.execute(
        """SELECT * FROM items WHERE enriched_at IS NULL
           ORDER BY base_score DESC LIMIT ?""", (limit,)))


def feed(con: sqlite3.Connection, category: str | None = None,
         unread_only: bool = False, days: int = 7,
         saved_only: bool = False) -> list[sqlite3.Row]:
    """Ranked feed. Enriched items sort by LLM score, un-enriched fall back to
    base_score so the TUI still works if the LLM pass was skipped or failed."""
    # cluster_id = id marks the representative of a duplicate group; other
    # members stay in the table (so they are not re-fetched) but never render.
    where = ["first_seen > ?", "(cluster_id IS NULL OR cluster_id = id)"]
    params: list = [time.time() - days * 86400]
    if category == "repos":
        # "repos" is a kind of item, not a topic: the LLM assigns repos a real
        # subject (an exploit PoC is security, an inference engine is ai), so
        # this view selects on where the item came from instead.
        placeholders = ",".join("?" * len(_IDENTIFIER_SOURCES))
        where.append(f"source_key IN ({placeholders})")
        params.extend(sorted(_IDENTIFIER_SOURCES))
    elif category:
        where.append("COALESCE(llm_category, category) = ?")
        params.append(category)
    if unread_only:
        where.append("read_at IS NULL")
    if saved_only:
        where.append("saved = 1")
    rows = list(con.execute(
        f"""SELECT * FROM items WHERE {' AND '.join(where)}
            ORDER BY is_top DESC,
                     COALESCE(llm_score, base_score) DESC,
                     first_seen DESC""", params))
    return diversify(rows)


def diversify(rows: list[sqlite3.Row], max_run: int = 3) -> list[sqlite3.Row]:
    """Break up long runs of one source while preserving overall ranking.

    Pure score order buries everything else under whichever source scores
    highest today - a KEV day puts fifteen consecutive CVEs at the top, which is
    accurate but unreadable. An item that would extend a run past `max_run` is
    deferred until a different source appears, so a scan down the list stays
    varied without reordering by anything other than score.

    Editor-chosen top picks are exempt: their order is deliberate.
    """
    tops = [r for r in rows if r["is_top"]]
    rest = [r for r in rows if not r["is_top"]]

    out: list[sqlite3.Row] = []
    deferred: list[sqlite3.Row] = []
    run_key: str | None = None
    run_len = 0

    def emit(row: sqlite3.Row) -> None:
        nonlocal run_key, run_len
        out.append(row)
        if row["source_key"] == run_key:
            run_len += 1
        else:
            run_key, run_len = row["source_key"], 1

    for row in rest:
        # A deferred item can go out as soon as it no longer extends a run.
        for i, held in enumerate(deferred):
            if held["source_key"] != run_key or run_len < max_run:
                emit(deferred.pop(i))
                break
        if row["source_key"] == run_key and run_len >= max_run:
            deferred.append(row)
        else:
            emit(row)

    out.extend(deferred)  # whatever never found a gap keeps its relative order
    return tops + out


def mark_read(con: sqlite3.Connection, item_id_: str, read: bool = True) -> None:
    con.execute("UPDATE items SET read_at=? WHERE id=?",
                (time.time() if read else None, item_id_))
    con.commit()


def toggle_saved(con: sqlite3.Connection, item_id_: str) -> bool:
    cur = con.execute("SELECT saved FROM items WHERE id=?", (item_id_,)).fetchone()
    new = 0 if (cur and cur["saved"]) else 1
    con.execute("UPDATE items SET saved=? WHERE id=?", (new, item_id_))
    con.commit()
    return bool(new)


def recluster(con: sqlite3.Connection, days: int = 30) -> tuple[int, int]:
    """Recompute cluster assignments over stored items.

    Needed after any change to the clustering rules: a past run's decisions are
    baked into cluster_id, so items wrongly merged by an old threshold stay
    hidden until they are reassigned. Returns (rows, merged).
    """
    rows = list(con.execute(
        "SELECT * FROM items WHERE first_seen > ? ORDER BY first_seen",
        (time.time() - days * 86400,)))
    items = [Item(url=r["url"], title=r["title"], source_key=r["source_key"],
                  source_name=r["source_name"], category=r["category"],
                  signal=r["signal"] or 0.0, base_score=r["base_score"] or 0.0)
             for r in rows]
    clustered = cluster(items)
    merged = 0
    for it in clustered:
        # A representative points at itself; a duplicate points at its head.
        con.execute("UPDATE items SET cluster_id=? WHERE id=?", (it.cluster_id, it.id))
        if it.cluster_id and it.cluster_id != it.id:
            merged += 1
    con.commit()
    return len(rows), merged


def record_run(con: sqlite3.Connection, started: float, fetched: int,
               new: int, enriched: int, errors: str,
               usage: dict | None = None) -> None:
    """Record a run plus its LLM token usage.

    Named columns rather than positional VALUES: the runs table gains columns
    over time via _migrate(), and a bare `VALUES (?,?,?,?,?)` breaks the moment
    one is added.
    """
    u = usage or {}
    con.execute(
        """INSERT OR REPLACE INTO runs
           (started_at, fetched, new_items, enriched, errors, model, llm_calls,
            input_tokens, cache_creation_tokens, cache_read_tokens,
            output_tokens, cost_usd, duration_s)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (started, fetched, new, enriched, errors,
         u.get("model"), u.get("calls", 0), u.get("input_tokens", 0),
         u.get("cache_creation_tokens", 0), u.get("cache_read_tokens", 0),
         u.get("output_tokens", 0), u.get("cost_usd", 0.0),
         u.get("duration_s", 0.0)),
    )
    con.commit()


def usage_history(con: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return list(con.execute(
        "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)))


def usage_totals(con: sqlite3.Connection, days: float | None = None) -> sqlite3.Row:
    # Only runs that actually called the LLM. Runs recorded before the usage
    # columns existed (and --no-llm runs) have zero tokens, and counting them
    # divides the average by rows that never spent anything.
    where, params = "WHERE llm_calls > 0", []
    if days is not None:
        where += " AND started_at > ?"
        params = [time.time() - days * 86400]
    return con.execute(
        f"""SELECT COUNT(*) runs,
                   COALESCE(SUM(llm_calls),0)             calls,
                   COALESCE(SUM(input_tokens),0)          input_tokens,
                   COALESCE(SUM(cache_creation_tokens),0) cache_creation_tokens,
                   COALESCE(SUM(cache_read_tokens),0)     cache_read_tokens,
                   COALESCE(SUM(output_tokens),0)         output_tokens,
                   COALESCE(SUM(cost_usd),0)              cost_usd,
                   COALESCE(SUM(enriched),0)              enriched
            FROM runs {where}""", params).fetchone()


# Sources whose "title" is really a unique identifier (owner/repo, org/model).
# Two of these are never the same thing however much their names overlap, so
# they are matched on URL alone.
_IDENTIFIER_SOURCES = {"gh_new", "gh_trending", "hf_models"}

# Jaccard over the union is what actually defends against short titles: "Flux 3"
# reduces to {"flux"}, which against a five-word title scores 1/5 = 0.2 and is
# rejected on its own. So this floor only needs to exclude titles that reduce to
# nothing. Measured on 688 real items, threshold 0.62 yields exactly one merge -
# "Claude Opus 5" / "Introducing Claude Opus 5" - and no false positives.
_MIN_WORDS = 2


def cluster(items: list[Item], threshold: float = 0.62) -> list[Item]:
    """Group near-duplicate titles across sources.

    O(n^2) on title word-sets, which is fine at our scale (a few hundred items).
    The highest-signal member of each cluster becomes the representative and
    absorbs the others' source names, so the TUI can show "hn · lobsters".

    Two guards stop over-merging, both of which produced real false positives:
    Jaccard over the union rather than the smaller set (min() makes any short
    title contained in a longer one score 1.0), and never merging two items from
    the same source - a feed publishing twice means two different things, e.g.
    consecutive Latent Space issues that share a boilerplate title.
    """
    keys = [title_key(it.title) for it in items]
    # Each group carries the index of its head so we can compare word-sets
    # without searching `items` (which would also mis-match equal-valued Items).
    groups: list[tuple[int, list[Item]]] = []
    for i, it in enumerate(items):
        a = keys[i]
        eligible = (it.source_key not in _IDENTIFIER_SOURCES and len(a) >= _MIN_WORDS)
        if eligible:
            for head_idx, group in groups:
                head = items[head_idx]
                b = keys[head_idx]
                if len(b) < _MIN_WORDS or head.source_key in _IDENTIFIER_SOURCES:
                    continue
                # Same feed publishing twice = two distinct items, not a dupe.
                if any(g.source_key == it.source_key for g in group):
                    continue
                if len(a & b) / len(a | b) >= threshold:
                    group.append(it)
                    break
            else:
                groups.append((i, [it]))
        else:
            groups.append((i, [it]))

    out = []
    for _, group in groups:
        group.sort(key=lambda x: (x.signal, x.base_score), reverse=True)
        head = group[0]
        head.cluster_id = head.id
        head.dupes = [g.source_name for g in group[1:]]
        for g in group[1:]:
            g.cluster_id = head.id
        out.append(head)
        # Non-representatives are still stored (so they are not re-fetched
        # forever) but are excluded from the ranked feed by cluster_id.
        out.extend(group[1:])
    return out
