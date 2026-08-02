"""Fetchers, one per source kind.

Sources are fetched concurrently, but each returns items or an error string -
never raises - so one dead endpoint can't take down a run. Reddit is handled
serially at the end because it 429s under concurrency.
"""

from __future__ import annotations

import html
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import httpx

from .sources import BROWSER_UA, Source, enabled
from .store import Item

DEFAULT_UA = "news-aggregator/1.0 (personal feed reader)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def _client(ua: str | None = None) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": ua or DEFAULT_UA, "Accept": "*/*"},
        timeout=TIMEOUT, follow_redirects=True,
    )


# Transient failures that are worth another go. A 403/429 is deliberate refusal,
# not a blip, so it is excluded - retrying only digs the hole deeper.
_RETRYABLE = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError,
              httpx.RemoteProtocolError, httpx.WriteTimeout)


def _get(c: httpx.Client, url: str, *, tries: int = 3, **kw) -> httpx.Response:
    """GET with backoff on transient network errors and 5xx."""
    last: Exception | None = None
    for attempt in range(tries):
        if attempt:
            time.sleep(1.5 * (2 ** (attempt - 1)))
        try:
            r = c.get(url, **kw)
        except _RETRYABLE as exc:
            last = exc
            continue
        if r.status_code >= 500:
            last = httpx.HTTPStatusError(
                f"server error {r.status_code}", request=r.request, response=r)
            continue
        return r
    raise last  # type: ignore[misc]


def _ts(text: str | None) -> float | None:
    """Parse the several date formats feeds use in the wild."""
    if not text:
        return None
    text = text.strip()
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
            return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).timestamp()
        except ValueError:
            continue
    # Fall back to a leading ISO date if the tail is junk.
    m = re.match(r"(\d{4}-\d{2}-\d{2})", text)
    if m:
        return datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    return None


def _clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())


def _recency(published: float | None, half_life_h: float = 36.0) -> float:
    """1.0 for brand new, decaying by half every `half_life_h`. Items with no
    date are treated as moderately fresh rather than penalized to zero."""
    if not published:
        return 0.5
    age_h = max(0.0, (time.time() - published) / 3600)
    return 0.5 ** (age_h / half_life_h)


def _score(signal: float, weight: float, published: float | None, curated: bool) -> float:
    """Base (pre-LLM) score, 0-100.

    Curated sources have no popularity number, so their score is weight-driven;
    otherwise log-compress the signal so a 3000-point post doesn't sit 30x above
    a 100-point one. Recency multiplies at the end.
    """
    if curated or signal <= 0:
        pop = 45.0
    else:
        pop = min(100.0, 18.0 * math.log10(signal + 1) ** 1.6)
    return round(pop * (0.45 + 0.55 * weight) * (0.35 + 0.65 * _recency(published)), 2)


# --------------------------------------------------------------------------
# kind: rss (RSS 2.0 and Atom, including Blogger's odd Atom)
# --------------------------------------------------------------------------
def _parse_feed(r: httpx.Response, key: str) -> ET.Element:
    """Parse a feed response, failing loudly when a site serves HTML at 200.

    Several sites (tldrsec, any SPA with a catch-all route) answer unknown paths
    with their app shell and a 200 status. Checking only the status code hides
    that; the XML parse error it eventually causes points at a byte offset in
    minified JavaScript, which is useless. Diagnose it here instead.

    Judge the body, not the content-type header: plenty of real feeds are served
    as text/html (WordPress sites like Krebs do this), so the header alone would
    reject working sources.
    """
    head = r.content[:512].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        ctype = r.headers.get("content-type", "none")
        raise RuntimeError(
            f"{key}: served an HTML page, not a feed (content-type: {ctype}) "
            f"- the feed URL is probably wrong")
    return ET.fromstring(r.content)


def fetch_rss(src: Source) -> list[Item]:
    with _client(src.ua) as c:
        r = _get(c, src.url)
        r.raise_for_status()
        root = _parse_feed(r, src.key)

    out: list[Item] = []
    entries = root.findall(".//item") or root.findall(".//atom:entry", NS)
    for e in entries[:40]:
        title = _clean(_text(e, "title") or _text(e, "atom:title"))
        link = _text(e, "link") or _text(e, "atom:link[@rel='alternate']", attr="href")
        if not link:
            # Atom without rel, or Blogger: take the first link with an href.
            for le in e.findall("atom:link", NS):
                if le.get("href") and le.get("rel") in (None, "alternate"):
                    link = le.get("href")
                    break
        if not (title and link):
            continue
        published = _ts(_text(e, "pubDate") or _text(e, "atom:published")
                        or _text(e, "atom:updated") or _text(e, "dc:date"))
        author = _clean(_text(e, "dc:creator") or _text(e, "atom:author/atom:name") or "") or None
        out.append(Item(
            url=link.strip(), title=title, source_key=src.key, source_name=src.name,
            category=src.category, author=author, published=published,
            signal_label="", base_score=_score(0, src.weight, published, src.curated),
        ))
    return out


def _text(el: ET.Element, path: str, attr: str | None = None) -> str | None:
    found = el.find(path, NS) if (":" in path or "[" in path) else el.find(path)
    if found is None:
        return None
    return found.get(attr) if attr else (found.text or None)


# --------------------------------------------------------------------------
# kind: hn_algolia
# --------------------------------------------------------------------------
def fetch_hn_algolia(src: Source) -> list[Item]:
    min_pts = src.opts.get("min_points", 120)
    since = int(time.time() - src.opts.get("hours", 30) * 3600)
    url = ("https://hn.algolia.com/api/v1/search?tags=story"
           f"&numericFilters=points>{min_pts},created_at_i>{since}"
           "&hitsPerPage=60")
    with _client() as c:
        r = _get(c, url)
        r.raise_for_status()
        data = r.json()

    out = []
    for h in data.get("hits", []):
        title = _clean(h.get("title"))
        pts = float(h.get("points") or 0)
        hn_url = f"https://news.ycombinator.com/item?id={h['objectID']}"
        # Prefer the article; Ask HN / text posts have no url, use the HN page.
        link = h.get("url") or hn_url
        if not title:
            continue
        published = float(h.get("created_at_i") or 0) or None
        out.append(Item(
            url=link, title=title, source_key=src.key, source_name=src.name,
            category=src.category, author=h.get("author"), published=published,
            signal=pts, signal_label=f"{int(pts)} pts",
            extra=json.dumps({"comments": hn_url, "n_comments": h.get("num_comments")}),
            base_score=_score(pts, src.weight, published, False),
        ))
    return out


# --------------------------------------------------------------------------
# kind: reddit  (needs the descriptive UA from sources.py; rate-limits hard)
# --------------------------------------------------------------------------
def fetch_reddit(src: Source) -> list[Item]:
    subs: dict[str, str] = src.opts.get("subs", {})
    url = (f"https://www.reddit.com/r/{'+'.join(subs)}/.rss"
           f"?limit={src.opts.get('limit', 60)}")
    with _client(src.ua) as c:
        r = _get(c, url)
        if r.status_code in (403, 429):
            raise RuntimeError(
                f"reddit: {r.status_code} rate-limited/blocked - this is one "
                f"request, so back off the whole run rather than the source")
        r.raise_for_status()
        root = _parse_feed(r, src.key)

    out = []
    for e in root.findall(".//atom:entry", NS):
        title = _clean(_text(e, "atom:title"))
        link = _text(e, "atom:link", attr="href")
        if not (title and link):
            continue
        # <category label="r/netsec"> tells us which sub this came from, so a
        # merged multireddit still routes to the right category.
        label = _text(e, "atom:category", attr="label") or ""
        sub = label.removeprefix("r/")
        category = subs.get(sub, src.category)
        published = _ts(_text(e, "atom:published") or _text(e, "atom:updated"))
        author = _clean(_text(e, "atom:author/atom:name") or "") or None
        # Reddit's RSS carries no score, so rank on source weight + recency.
        out.append(Item(
            url=link, title=title, source_key=src.key,
            source_name=f"r/{sub}" if sub else src.name,
            category=category, author=author, published=published,
            signal_label="", base_score=_score(0, src.weight, published, True),
        ))
    return out


# --------------------------------------------------------------------------
# kind: gh_new  - repos created recently that already have traction
# --------------------------------------------------------------------------
def fetch_gh_new(src: Source) -> list[Item]:
    days = src.opts.get("days", 10)
    min_stars = src.opts.get("min_stars", 60)
    since = datetime.fromtimestamp(time.time() - days * 86400, timezone.utc).strftime("%Y-%m-%d")
    url = ("https://api.github.com/search/repositories"
           f"?q=created:>{since}+stars:>{min_stars}&sort=stars&order=desc&per_page=40")
    headers = {"Accept": "application/vnd.github+json"}
    import os
    # Unauthenticated search is 10 req/min, which is plenty for one call a day,
    # but a token raises it and is used if present.
    if tok := (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")):
        headers["Authorization"] = f"Bearer {tok}"
    with _client() as c:
        r = _get(c, url, headers=headers)
        r.raise_for_status()
        data = r.json()

    out = []
    for repo in data.get("items", []):
        stars = float(repo.get("stargazers_count") or 0)
        created = _ts(repo.get("created_at"))
        age_days = max(0.5, (time.time() - (created or time.time())) / 86400)
        velocity = stars / age_days
        desc = _clean(repo.get("description"))
        title = f"{repo['full_name']} — {desc}" if desc else repo["full_name"]
        out.append(Item(
            url=repo["html_url"], title=title, source_key=src.key,
            source_name=src.name, category=src.category,
            author=repo.get("owner", {}).get("login"), published=created,
            signal=velocity,
            signal_label=f"★{_k(stars)} in {int(age_days)}d",
            extra=json.dumps({"lang": repo.get("language"), "stars": int(stars),
                              "velocity": round(velocity)}),
            # Score on stars-per-day, not raw stars: a repo at 2k stars in 4 days
            # is a bigger deal than one at 3k over 10.
            base_score=_score(velocity * 8, src.weight, created, False),
        ))
    return out


def _k(n: float) -> str:
    return f"{n/1000:.1f}k" if n >= 1000 else str(int(n))


# --------------------------------------------------------------------------
# kind: gh_trending  - no API, so parse the HTML (loosely, on purpose)
# --------------------------------------------------------------------------
def fetch_gh_trending(src: Source) -> list[Item]:
    with _client(src.ua or BROWSER_UA) as c:
        r = _get(c, src.url)
        r.raise_for_status()
        body = r.text

    out = []
    # The anchor carries a data-hydro-click blob before href, so href is not the
    # first attribute: <h2 class="h3 lh-condensed"> <a data-hydro-click="..."
    # ... href="/owner/repo">. Match any attributes ahead of it.
    for m in re.finditer(
        r'<h2 class="h3 lh-condensed">\s*<a\b[^>]*?href="/([^"/]+)/([^"/?#]+)"', body):
        owner, repo = m.group(1), m.group(2)
        tail = body[m.end():m.end() + 1200]
        desc = ""
        if d := re.search(r'<p class="col-9[^"]*">\s*(.*?)\s*</p>', tail, re.S):
            desc = _clean(d.group(1))
        today = 0.0
        if t := re.search(r'([\d,]+)\s*stars today', tail):
            today = float(t.group(1).replace(",", ""))
        full = f"{owner}/{repo}"
        out.append(Item(
            url=f"https://github.com/{full}", title=f"{full} — {desc}" if desc else full,
            source_key=src.key, source_name=src.name, category=src.category,
            author=owner, published=None, signal=today,
            signal_label=f"★{int(today)} today" if today else "trending",
            base_score=_score(today * 10, src.weight, time.time(), False),
        ))
    if not out:
        raise RuntimeError("github trending: markup changed, 0 repos parsed")
    return out


# --------------------------------------------------------------------------
# kind: hf_models
# --------------------------------------------------------------------------
def fetch_hf_models(src: Source) -> list[Item]:
    url = ("https://huggingface.co/api/models"
           f"?sort={src.opts.get('sort','likes7d')}&direction=-1"
           f"&limit={src.opts.get('limit',25)}")
    with _client() as c:
        r = _get(c, url)
        r.raise_for_status()
        data = r.json()

    out = []
    for m in data:
        mid = m.get("modelId") or m.get("id")
        if not mid:
            continue
        likes = float(m.get("likes") or 0)
        dls = float(m.get("downloads") or 0)
        tags = [t for t in (m.get("tags") or []) if not t.startswith(("license:", "region:"))][:5]
        out.append(Item(
            url=f"https://huggingface.co/{mid}", title=f"{mid}",
            source_key=src.key, source_name=src.name, category=src.category,
            author=mid.split("/")[0] if "/" in mid else None,
            published=_ts(m.get("createdAt")), signal=likes,
            signal_label=f"♥{int(likes)}",
            extra=json.dumps({"pipeline": m.get("pipeline_tag"), "tags": tags,
                              "downloads": int(dls)}),
            base_score=_score(likes * 3, src.weight, _ts(m.get("createdAt")), False),
        ))
    return out


# --------------------------------------------------------------------------
# kind: kev  - CISA Known Exploited Vulnerabilities
# --------------------------------------------------------------------------
def _kev_title(cve: str, vendor: str, product: str, name: str) -> str:
    """Build a readable KEV headline.

    KEV records repeat themselves heavily: vendorProject "Langflow", shortName
    "Langflow" and vulnerabilityName "Langflow Inclusion of Functionality from
    Untrusted Control Sphere Vulnerability" concatenate into unreadable mush.
    Collapse the repeats and drop the boilerplate "Vulnerability" suffix.
    """
    vendor, product, name = vendor.strip(), product.strip(), name.strip()
    # "Langflow" + "Langflow" -> one label; "Microsoft" + "SharePoint" -> both.
    label = vendor if product.lower() == vendor.lower() else f"{vendor} {product}".strip()

    # Strip a leading restatement of the label from the vulnerability name.
    for prefix in (f"{label} ", f"{vendor} ", f"{product} "):
        if prefix.strip() and name.lower().startswith(prefix.lower()):
            name = name[len(prefix):]
            break
    name = re.sub(r"\s+Vulnerability$", "", name).strip()

    return f"{cve}: {label} — {name}" if name else f"{cve}: {label}"


def fetch_kev(src: Source) -> list[Item]:
    with _client() as c:
        r = _get(c, src.url)
        r.raise_for_status()
        data = r.json()

    cutoff = time.time() - src.opts.get("days", 14) * 86400
    out = []
    for v in data.get("vulnerabilities", []):
        added = _ts(v.get("dateAdded"))
        if not added or added < cutoff:
            continue
        cve = v.get("cveID", "")
        ransomware = (v.get("knownRansomwareCampaignUse") or "").lower() == "known"
        title = _kev_title(cve, v.get("vendorProject", ""),
                           v.get("shortName") or v.get("product", ""),
                           _clean(v.get("vulnerabilityName") or ""))
        out.append(Item(
            url=f"https://nvd.nist.gov/vuln/detail/{cve}", title=title,
            source_key=src.key, source_name=src.name, category=src.category,
            published=added,
            # Everything in KEV is confirmed exploited in the wild, so the floor
            # is high; ransomware-linked entries pin to the top.
            signal=100.0 if ransomware else 70.0,
            signal_label="KEV+ransomware" if ransomware else "KEV exploited",
            extra=json.dumps({"cve": cve, "due": v.get("dueDate"),
                              "action": _clean(v.get("requiredAction")),
                              "ransomware": ransomware}),
            base_score=(96.0 if ransomware else 88.0) * (0.5 + 0.5 * _recency(added, 120)),
        ))
    return out


# --------------------------------------------------------------------------
# kind: bluesky  - public API, no auth
# --------------------------------------------------------------------------
def fetch_bluesky(src: Source) -> list[Item]:
    handles = src.opts.get("handles", [])
    limit = src.opts.get("limit", 15)
    out: list[Item] = []
    with _client() as c:
        for h in handles:
            try:
                r = _get(c, "https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
                          params={"actor": h, "limit": limit, "filter": "posts_no_replies"})
                if r.status_code != 200:
                    continue
                feed = r.json().get("feed", [])
            except (httpx.HTTPError, json.JSONDecodeError):
                continue

            for entry in feed:
                post = entry.get("post", {})
                rec = post.get("record", {})
                text = _clean(rec.get("text"))
                # Skip reposts (they carry a `reason`) and trivial one-liners.
                if entry.get("reason") or len(text) < 60:
                    continue
                likes = float(post.get("likeCount") or 0)
                reposts = float(post.get("repostCount") or 0)
                # Prefer a link the post is pointing at; that's the actual
                # artifact. Fall back to the post itself.
                link = _bsky_link(post) or _bsky_permalink(h, post.get("uri", ""))
                if not link:
                    continue
                engagement = likes + 2 * reposts
                published = _ts(rec.get("createdAt"))
                out.append(Item(
                    url=link, title=text[:280], source_key=src.key,
                    source_name=f"bsky/{h.split('.')[0]}", category=src.category,
                    author=h, published=published, signal=engagement,
                    signal_label=f"♥{int(likes)}",
                    extra=json.dumps({"post": _bsky_permalink(h, post.get("uri", ""))}),
                    base_score=_score(engagement, src.weight, published, False),
                ))
    return out


def _bsky_link(post: dict) -> str | None:
    embed = post.get("embed") or {}
    for key in ("external", "record"):
        ext = embed.get(key) or {}
        if uri := (ext.get("uri") if key == "external" else None):
            return uri
    # Also check facets for the first outbound link in the text.
    for facet in (post.get("record", {}).get("facets") or []):
        for feat in facet.get("features", []):
            if feat.get("$type", "").endswith("#link") and feat.get("uri"):
                return feat["uri"]
    return None


def _bsky_permalink(handle: str, at_uri: str) -> str:
    rkey = at_uri.rsplit("/", 1)[-1] if at_uri else ""
    return f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else ""


FETCHERS = {
    "rss": fetch_rss,
    "hn_algolia": fetch_hn_algolia,
    "reddit": fetch_reddit,
    "gh_new": fetch_gh_new,
    "gh_trending": fetch_gh_trending,
    "hf_models": fetch_hf_models,
    "kev": fetch_kev,
    "bluesky": fetch_bluesky,
}


def fetch_all(sources: list[Source] | None = None,
              on_progress=None) -> tuple[list[Item], list[str]]:
    """Fetch every source. Returns (items, error strings).

    Reddit sources run serially with a pause afterwards - concurrent requests
    reliably trip its rate limiter and return 429 for the whole batch.
    """
    srcs = sources if sources is not None else enabled()
    concurrent = [s for s in srcs if s.kind != "reddit"]
    serial = [s for s in srcs if s.kind == "reddit"]

    items: list[Item] = []
    errors: list[str] = []

    def run(src: Source) -> tuple[Source, list[Item] | Exception]:
        try:
            return src, FETCHERS[src.kind](src)
        except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
            return src, exc

    def absorb(src: Source, res: list[Item] | Exception) -> None:
        if isinstance(res, Exception):
            errors.append(f"{src.key}: {type(res).__name__}: {res}".strip()[:200])
            if on_progress:
                on_progress(src, None)
        else:
            items.extend(res)
            if on_progress:
                on_progress(src, len(res))

    with ThreadPoolExecutor(max_workers=8) as pool:
        for src, res in pool.map(run, concurrent):
            absorb(src, res)

    for i, src in enumerate(serial):
        if i:
            time.sleep(2.5)
        absorb(*run(src))

    return items, errors
