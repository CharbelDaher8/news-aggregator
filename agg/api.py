"""A JSON view of the feed, for another application to read.

Why this exists rather than the other application reading `items.db` directly:
the interesting part of a feed is not the rows, it is `store.feed()` and
`store.diversify()` — what ranks above what, and the rule that stops fifteen
consecutive CVEs from burying everything else on a KEV day. A client with its
own SQL would have to reimplement both, in another language, and the two copies
would drift the first time either is tuned. Marking an item read is a *write*,
too, so direct access would also mean two applications writing one database.

So the boundary is HTTP and the ranking stays here, in the one place that
already knows it.

## No authentication, deliberately

There is none, and the deployment is what has to be true. This binds loopback by
default, and in the compose stack it runs on a private network with no published
port — the only thing that can reach it is the notes server in a sibling
container. Anything that can reach this endpoint can mark your feed read and
read everything in it.

## Threading

One connection per request rather than one shared: `sqlite3` connections are not
safe to move between threads, and `ThreadingHTTPServer` hands each request to
whichever thread it likes. Connections are cheap and WAL means readers do not
block the refresh that may be running for minutes alongside them.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import store

# Bigger than a screen, small enough that a client cannot ask for the whole
# database in one request by accident.
MAX_LIMIT = 500
DEFAULT_LIMIT = 100


def item_json(row: sqlite3.Row) -> dict:
    """The wire shape.

    Listed field by field rather than `dict(row)` so that adding a column to
    the table is not the same thing as publishing it. `extra` is deliberately
    absent: it is a per-source blob whose shape is a private arrangement
    between fetch.py and the reader.
    """
    return {
        "id": row["id"],
        "url": row["url"],
        "title": row["title"],
        "source": row["source_name"],
        "sourceKey": row["source_key"],
        "category": row["llm_category"] or row["category"],
        "author": row["author"],
        "published": row["published"],
        "firstSeen": row["first_seen"],
        "signal": row["signal"],
        "signalLabel": row["signal_label"],
        # Null until the LLM pass has seen it, which is a real state a client
        # has to render: "not scored yet" is not "scored zero".
        "summary": row["summary"],
        "score": row["llm_score"],
        "isTop": bool(row["is_top"]),
        "topReason": row["top_reason"],
        "read": row["read_at"] is not None,
        "saved": bool(row["saved"]),
    }


def _int(params: dict, name: str, default: int, low: int, high: int) -> int:
    raw = params.get(name, [None])[0]
    if raw is None:
        return default
    try:
        return max(low, min(high, int(raw)))
    except ValueError:
        return default


def _flag(params: dict, name: str) -> bool:
    raw = params.get(name, [None])[0]
    return raw is not None and raw.lower() in ("1", "true", "yes")


def _exists(con: sqlite3.Connection, item_id: str) -> bool:
    """Checked before every write, so an unknown id is a 404 and not a shrug.

    `store.mark_read` runs an UPDATE that matches nothing and reports nothing,
    which is fine for the TUI -- it only ever passes ids it just read out of the
    table. Over HTTP the id comes from a client that may be out of date or
    simply wrong, and "200, sure, marked it read" is the kind of answer that
    turns into an afternoon of wondering why the state does not stick.
    """
    return con.execute(
        "SELECT 1 FROM items WHERE id=? LIMIT 1", (item_id,)).fetchone() is not None


class Handler(BaseHTTPRequestHandler):
    server_version = "news-api"

    # --- plumbing ---------------------------------------------------------

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # No cache, ever. Read state changes under the client's feet whenever
        # the reader in the terminal touches the same item.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # The default logs every request to stderr, which in a container is a
        # line per poll forever. Errors still surface: they raise.
        pass

    # --- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - the base class names it
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/health":
            with closing(store.connect()) as con:
                total = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
                enriched = con.execute(
                    "SELECT COUNT(*) FROM items WHERE enriched_at IS NOT NULL"
                ).fetchone()[0]
                last = con.execute(
                    "SELECT MAX(started_at) FROM runs").fetchone()[0]
            self._send(200, {"ok": True, "items": total,
                             "enriched": enriched, "lastRun": last})
            return

        if parsed.path == "/feed":
            category = (params.get("category", [None])[0]) or None
            days = _int(params, "days", 7, 1, 365)
            limit = _int(params, "limit", DEFAULT_LIMIT, 1, MAX_LIMIT)

            with closing(store.connect()) as con:
                rows = store.feed(
                    con,
                    category=category,
                    unread_only=_flag(params, "unread"),
                    days=days,
                    saved_only=_flag(params, "saved"),
                )
            self._send(200, {"items": [item_json(r) for r in rows[:limit]],
                             "total": len(rows)})
            return

        self._send(404, {"error": "no such endpoint"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]

        # /items/<id>/read and /items/<id>/saved
        if len(parts) == 3 and parts[0] == "items":
            item_id, action = parts[1], parts[2]

            if action == "read":
                # The body says which way, so a client can mark unread too.
                # Absent body means read, which is the common case.
                read = self._body().get("read", True)
                with closing(store.connect()) as con:
                    if not _exists(con, item_id):
                        self._send(404, {"error": "no such item"})
                        return
                    store.mark_read(con, item_id, bool(read))
                self._send(200, {"ok": True, "read": bool(read)})
                return

            if action == "saved":
                with closing(store.connect()) as con:
                    if not _exists(con, item_id):
                        self._send(404, {"error": "no such item"})
                        return
                    saved = store.toggle_saved(con, item_id)
                self._send(200, {"ok": True, "saved": saved})
                return

        self._send(404, {"error": "no such endpoint"})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return {}


def serve(host: str = "127.0.0.1", port: int = 8787) -> None:
    ThreadingHTTPServer((host, port), Handler).serve_forever()
