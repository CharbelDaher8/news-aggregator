"""Terminal reader.

Layout mirrors a mail client: ranked list on top, detail for the highlighted row
below. Everything is keyboard driven.
"""

from __future__ import annotations

import json
import sqlite3
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Input, Static

from . import store

CATEGORIES = [None, "ai", "security", "tech", "repos"]
CAT_STYLE = {"ai": "cyan", "security": "red", "tech": "green", "repos": "magenta"}
# Explicit abbreviations: slicing to a fixed width gives "secu", which reads worse
# than a chosen short form.
CAT_ABBR = {"ai": "ai", "security": "sec", "tech": "tech", "repos": "repo"}


def _age(ts: float | None) -> str:
    if not ts:
        return "—"
    secs = max(0, time.time() - ts)
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    if secs < 86400 * 14:
        return f"{int(secs // 86400)}d"
    return f"{int(secs // (86400 * 7))}w"


class Detail(Static):
    """Bottom pane: everything known about the highlighted item."""

    def show(self, row: sqlite3.Row | None) -> None:
        if row is None:
            self.update(Text("no items — run `news refresh`", style="dim"))
            return

        t = Text()
        if row["is_top"]:
            t.append("★ TOP PICK  ", style="bold yellow")
        t.append(row["title"], style="bold")
        t.append("\n")

        meta = [row["source_name"]]
        if row["signal_label"]:
            meta.append(row["signal_label"])
        if row["author"]:
            meta.append(f"@{row['author']}")
        meta.append(_age(row["published"] or row["first_seen"]) + " ago")
        if row["llm_score"] is not None:
            meta.append(f"score {row['llm_score']}")
        t.append(" · ".join(meta) + "\n", style="dim")

        if row["summary"]:
            t.append("\n")
            t.append(row["summary"], style="white")
            t.append("\n")
        if row["top_reason"]:
            t.append("\nwhy: ", style="bold yellow")
            t.append(row["top_reason"], style="italic")
            t.append("\n")

        for label, value in _extras(row):
            t.append(f"\n{label}: ", style="dim")
            t.append(value)

        t.append("\n\n")
        t.append(row["url"], style="blue underline")
        self.update(t)


def _extras(row: sqlite3.Row) -> list[tuple[str, str]]:
    try:
        ex = json.loads(row["extra"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return []
    out = []
    if ex.get("comments"):
        out.append(("comments", ex["comments"]))
    if ex.get("cve"):
        detail = ex["cve"]
        if ex.get("ransomware"):
            detail += "  (used in ransomware campaigns)"
        if ex.get("due"):
            detail += f"  · federal patch deadline {ex['due']}"
        out.append(("cve", detail))
    if ex.get("action"):
        out.append(("required action", ex["action"][:200]))
    if ex.get("lang"):
        out.append(("language", ex["lang"]))
    if ex.get("downloads"):
        out.append(("downloads", f"{ex['downloads']:,}"))
    if ex.get("post"):
        out.append(("post", ex["post"]))
    return out


class NewsApp(App):
    CSS = """
    Screen { layout: vertical; }
    #status { height: 1; background: $panel; color: $text; padding: 0 1; }
    DataTable { height: 1fr; }
    /* Proportional, not fixed: a fixed 12 lines leaves only 9 list rows on an
       80x24 terminal. Scales with the window, with a floor for tiny ones. */
    #detail { height: 40%; min-height: 7; border-top: solid $primary;
              padding: 0 1; overflow-y: auto; }
    #search { display: none; }
    #search.visible { display: block; }
    """

    BINDINGS = [
        Binding("j,down", "cursor_down", "down", show=False),
        Binding("k,up", "cursor_up", "up", show=False),
        Binding("o,enter", "open", "open"),
        Binding("m", "toggle_read", "read"),
        Binding("s", "toggle_saved", "save"),
        # Not `tab`: Textual reserves it for focus traversal and the binding
        # never fires. `c` cycles, digits jump straight to a category.
        Binding("c", "next_category", "category"),
        Binding("1", "cat(0)", "all", show=False),
        Binding("2", "cat(1)", "ai", show=False),
        Binding("3", "cat(2)", "security", show=False),
        Binding("4", "cat(3)", "tech", show=False),
        Binding("5", "cat(4)", "repos", show=False),
        Binding("u", "toggle_unread", "unread only"),
        Binding("b", "toggle_saved_view", "saved"),
        Binding("slash", "search", "search"),
        Binding("r", "refresh", "refresh"),
        Binding("g", "top", "top", show=False),
        Binding("G", "bottom", "bottom", show=False),
        Binding("q,escape", "quit", "quit"),
    ]

    def __init__(self, category: str | None = None, unread_only: bool = False,
                 days: int = 7) -> None:
        super().__init__()
        self.con = store.connect()
        self.cat_idx = CATEGORIES.index(category) if category in CATEGORIES else 0
        self.unread_only = unread_only
        self.saved_only = False
        self.days = days
        self.rows: list[sqlite3.Row] = []
        self.query = ""

    def compose(self) -> ComposeResult:
        yield Static(id="status")
        with Vertical():
            yield DataTable(id="table", cursor_type="row", zebra_stripes=False)
            yield Input(placeholder="search titles…", id="search")
            yield Detail(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_column("", width=2, key="mark")
        table.add_column("score", width=5, key="score")
        table.add_column("title", key="title")
        table.add_column("source", width=18, key="source")
        table.add_column("", width=4, key="cat")
        table.add_column("age", width=4, key="age")
        table.focus()
        self.reload()

    # ---------------- data ----------------
    def reload(self, keep_cursor: bool = True) -> None:
        table = self.query_one("#table", DataTable)
        prev = table.cursor_row if keep_cursor else 0

        self.rows = store.feed(
            self.con, category=CATEGORIES[self.cat_idx],
            unread_only=self.unread_only, days=self.days,
            saved_only=self.saved_only)
        if self.query:
            q = self.query.lower()
            # Include source and signal label: searching "kev" should find the
            # CISA KEV entries even though their titles are bare CVE ids.
            self.rows = [
                r for r in self.rows
                if q in r["title"].lower()
                or q in (r["summary"] or "").lower()
                or q in r["source_name"].lower()
                or q in (r["signal_label"] or "").lower()
            ]

        # The title column has no natural width, so left unset it expands to the
        # longest title and shoves source/cat/age off the right edge. Size it to
        # whatever is left over instead. Guarded because a resize event can
        # arrive before on_mount has added the columns.
        if "title" in table.columns:
            fixed = 2 + 5 + 18 + 4 + 4      # mark, score, source, cat, age
            padding = 2 * 6                  # DataTable pads each cell
            self._title_w = max(28, self.size.width - fixed - padding)
            col = table.columns["title"]
            # auto_width must go off too: a column added without an explicit
            # width keeps sizing itself to its content and ignores `width`.
            col.auto_width = False
            col.width = self._title_w

        table.clear()
        for r in self.rows:
            table.add_row(*self._cells(r), key=r["id"])

        if self.rows:
            table.move_cursor(row=min(prev, len(self.rows) - 1))
        self.update_status()
        self.show_detail()

    def _cells(self, r: sqlite3.Row) -> list[Text]:
        unread = r["read_at"] is None
        mark = Text("★" if r["is_top"] else ("●" if unread else "○"),
                    style="yellow" if r["is_top"] else ("bold" if unread else "dim"))
        if r["saved"]:
            mark = Text("⚑", style="green")

        score = r["llm_score"]
        score_t = (Text(str(score), style=_score_style(score)) if score is not None
                   else Text(f"~{int(r['base_score'])}", style="dim"))

        # Text() rather than markup: titles contain [brackets] that Rich would
        # otherwise try to parse as style tags.
        title = Text(r["title"], style="" if unread else "dim", no_wrap=True)
        if r["summary"] and not r["is_top"]:
            title.append("  ")
            title.append(r["summary"], style="dim italic")
        # Truncate here as well as setting the column width, so a long title can
        # never push the right-hand columns off screen.
        title.truncate(getattr(self, "_title_w", 60), overflow="ellipsis")

        raw_cat = r["llm_category"] or r["category"]
        cat = CAT_ABBR.get(raw_cat, raw_cat[:4])
        return [
            mark, score_t, title,
            Text(r["source_name"][:18], style="dim"),
            Text(cat, style=CAT_STYLE.get(r["llm_category"] or r["category"], "")),
            Text(_age(r["published"] or r["first_seen"]), style="dim"),
        ]

    def current(self) -> sqlite3.Row | None:
        table = self.query_one("#table", DataTable)
        if not self.rows or table.cursor_row is None:
            return None
        if 0 <= table.cursor_row < len(self.rows):
            return self.rows[table.cursor_row]
        return None

    def update_status(self) -> None:
        cat = CATEGORIES[self.cat_idx] or "all"
        flags = []
        if self.unread_only:
            flags.append("unread")
        if self.saved_only:
            flags.append("saved")
        if self.query:
            flags.append(f"/{self.query}")
        unread = sum(1 for r in self.rows if r["read_at"] is None)
        suffix = f"  [{' '.join(flags)}]" if flags else ""
        self.query_one("#status", Static).update(
            Text.assemble(
                ("news ", "bold"), ("▸ ", "dim"), (cat, "bold cyan"),
                (f"  {len(self.rows)} items, {unread} unread{suffix}", "dim"),
            ))

    def show_detail(self) -> None:
        self.query_one("#detail", Detail).show(self.current())

    def on_data_table_row_highlighted(self, _: DataTable.RowHighlighted) -> None:
        self.show_detail()

    def on_resize(self, _) -> None:
        # Recompute the title column width against the new terminal size. Skipped
        # until the table has columns, since resize fires before on_mount.
        if self.is_mounted and "title" in self.query_one("#table", DataTable).columns:
            self.reload()

    # ---------------- actions ----------------
    def action_cursor_down(self) -> None:
        self.query_one("#table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#table", DataTable).action_cursor_up()

    def action_top(self) -> None:
        self.query_one("#table", DataTable).move_cursor(row=0)

    def action_bottom(self) -> None:
        if self.rows:
            self.query_one("#table", DataTable).move_cursor(row=len(self.rows) - 1)

    def action_open(self) -> None:
        row = self.current()
        if not row:
            return
        # App.open_url, not webbrowser.open: served over the web the latter would
        # open a browser on the *server*. The driver routes it to whoever is
        # looking — the local terminal, or the remote browser tab.
        self.open_url(row["url"])
        if row["read_at"] is None:
            store.mark_read(self.con, row["id"])
            self._refresh_row(row["id"])
        self.notify(f"opened {row['source_name']}", timeout=2)

    def action_toggle_read(self) -> None:
        row = self.current()
        if not row:
            return
        store.mark_read(self.con, row["id"], read=row["read_at"] is None)
        # In unread-only view the item should disappear, so reload wholesale.
        if self.unread_only:
            self.reload()
        else:
            self._refresh_row(row["id"])

    def action_toggle_saved(self) -> None:
        row = self.current()
        if not row:
            return
        now_saved = store.toggle_saved(self.con, row["id"])
        if self.saved_only:
            self.reload()
        else:
            self._refresh_row(row["id"])
        self.notify("saved" if now_saved else "unsaved", timeout=2)

    def _refresh_row(self, item_id: str) -> None:
        """Re-render a single row in place, so the cursor does not jump."""
        fresh = self.con.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if fresh is None:
            return
        for i, r in enumerate(self.rows):
            if r["id"] == item_id:
                self.rows[i] = fresh
                table = self.query_one("#table", DataTable)
                for col, cell in zip(table.columns, self._cells(fresh)):
                    table.update_cell(item_id, col, cell, update_width=False)
                break
        self.update_status()
        self.show_detail()

    def action_next_category(self) -> None:
        self.cat_idx = (self.cat_idx + 1) % len(CATEGORIES)
        self.reload(keep_cursor=False)

    def action_cat(self, idx: int) -> None:
        self.cat_idx = idx % len(CATEGORIES)
        self.reload(keep_cursor=False)

    def action_toggle_unread(self) -> None:
        self.unread_only = not self.unread_only
        self.reload(keep_cursor=False)

    def action_toggle_saved_view(self) -> None:
        self.saved_only = not self.saved_only
        self.reload(keep_cursor=False)

    def action_search(self) -> None:
        box = self.query_one("#search", Input)
        box.add_class("visible")
        box.value = self.query
        box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query = event.value.strip()
        box = self.query_one("#search", Input)
        box.remove_class("visible")
        self.query_one("#table", DataTable).focus()
        self.reload(keep_cursor=False)

    def action_refresh(self) -> None:
        self.notify("fetching sources…", timeout=3)
        self._do_refresh()

    @work(thread=True)
    def _do_refresh(self) -> None:
        """Fetch in a worker thread so the UI stays responsive.

        A separate connection is required: sqlite3 objects are bound to the
        thread that created them.
        """
        from .fetch import fetch_all

        con = store.connect()
        try:
            items, errors = fetch_all()
            clustered = store.cluster(items)
            fresh = store.upsert(con, clustered)
            msg = f"{len(fresh)} new items"
            if errors:
                msg += f" ({len(errors)} source errors)"
            msg += " — run `news refresh` for summaries"
        except Exception as exc:  # noqa: BLE001 - surface, never crash the reader
            msg = f"refresh failed: {exc}"
        finally:
            con.close()
        self.call_from_thread(self._after_refresh, msg)

    def _after_refresh(self, msg: str) -> None:
        self.reload()
        self.notify(msg, timeout=6)


def _score_style(score: int) -> str:
    if score >= 90:
        return "bold red"
    if score >= 70:
        return "bold yellow"
    if score >= 40:
        return "white"
    return "dim"


def run_tui(category: str | None = None, unread_only: bool = False,
            days: int = 7) -> None:
    NewsApp(category=category, unread_only=unread_only, days=days).run()
