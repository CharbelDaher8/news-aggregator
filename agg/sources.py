"""Source registry.

Every endpoint here was verified reachable before being added. Sources that
needed a quirk to work carry it explicitly (see `ua` and the notes).

`weight` is hand-curated editorial trust, 0.0-1.0. It multiplies the popularity
signal during scoring, so a low-volume high-signal blog can outrank a noisy
aggregator. It is not a measure of importance; it answers "if this source and
another both surfaced an item, which do I believe knows why it matters".
"""

from dataclasses import dataclass, field

# Reddit 403s on generic UAs and on anything that looks like a bot. It wants a
# descriptive UA with contact info. It also rate-limits hard (429 after only a
# few rapid requests), so reddit sources are fetched with a delay - see fetch.py.
REDDIT_UA = "news-aggregator/1.0 (personal feed reader; +local)"

# BleepingComputer rejects plain curl-ish UAs with 403 but serves a browser one.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

AI, SEC, TECH = "ai", "security", "tech"
# "repos" is a VIEW over source_key, not a topic a source can declare - see
# store.feed(). Sources carry the topic their items are about, so an exploit PoC
# repo lands in security and an inference engine in ai.
REPOS = "repos"


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    kind: str  # rss | hn_algolia | reddit | gh_new | gh_trending | hf_models | kev | bluesky
    category: str
    url: str = ""
    weight: float = 0.5
    ua: str | None = None
    # Sources whose items carry no popularity number of their own. Their score
    # comes from weight alone, so they are never buried by a 900-point HN post.
    curated: bool = False
    opts: dict = field(default_factory=dict)


SOURCES: list[Source] = [
    # ---------- high-signal aggregators (the "cool stuff" proxy layer) ----------
    # HN via Algolia rather than the RSS feed: RSS gives the front page, Algolia
    # lets us set a points floor and a time window, which is the actual filter
    # we want. No auth, no rate limit in practice.
    Source("hn", "Hacker News", "hn_algolia", TECH, weight=0.7,
           opts={"min_points": 120, "hours": 30}),
    Source("lobsters", "Lobsters", "rss", TECH, "https://lobste.rs/rss", weight=0.75),

    # ---------- reddit ----------
    # ONE request covering every subreddit, via multireddit syntax
    # (/r/a+b+c/.rss). Reddit rate-limits unauthenticated RSS to roughly one
    # request per run per IP - three separate sources reliably got 403 then 429
    # even with delays between them. The feed tags each entry with
    # <category label="r/netsec">, so per-sub categories survive the merge.
    Source("reddit", "Reddit", "reddit", AI, weight=0.7, ua=REDDIT_UA,
           opts={"subs": {"netsec": SEC, "LocalLLaMA": AI, "MachineLearning": AI},
                 "limit": 60}),

    # ---------- github / new things ----------
    # Repos *created* recently that already have traction. This is the query that
    # surfaces genuinely new projects; /trending is dominated by repos that have
    # been popular for months.
    Source("gh_new", "New repos", "gh_new", TECH, weight=0.65,
           opts={"days": 10, "min_stars": 60}),
    Source("gh_trending", "GitHub Trending", "gh_trending", TECH,
           "https://github.com/trending", weight=0.5, ua=BROWSER_UA),
    Source("hf_models", "HuggingFace models", "hf_models", AI, weight=0.6,
           opts={"sort": "likes7d", "limit": 25}),

    # ---------- AI ----------
    Source("simonw", "Simon Willison", "rss", AI,
           "https://simonwillison.net/atom/everything/", weight=0.95, curated=True),
    Source("interconnects", "Interconnects", "rss", AI,
           "https://www.interconnects.ai/feed", weight=0.85, curated=True),
    Source("latent", "Latent Space", "rss", AI,
           "https://www.latent.space/feed", weight=0.8, curated=True),
    Source("zvi", "Don't Worry About the Vase", "rss", AI,
           "https://thezvi.substack.com/feed", weight=0.7, curated=True),
    Source("hf_blog", "HuggingFace blog", "rss", AI,
           "https://huggingface.co/blog/feed.xml", weight=0.65, curated=True),
    Source("openai", "OpenAI", "rss", AI,
           "https://openai.com/news/rss.xml", weight=0.8, curated=True),
    Source("deepmind", "DeepMind", "rss", AI,
           "https://deepmind.google/blog/rss.xml", weight=0.8, curated=True),
    Source("google_ai", "Google AI", "rss", AI,
           "https://blog.google/technology/ai/rss/", weight=0.6, curated=True),
    Source("arxiv_ai", "arXiv cs.AI", "rss", AI,
           "https://arxiv.org/rss/cs.AI", weight=0.3),
    Source("arxiv_lg", "arXiv cs.LG", "rss", AI,
           "https://arxiv.org/rss/cs.LG", weight=0.3),

    # ---------- security ----------
    # KEV is the highest-signal security source that exists: every entry is a
    # vulnerability confirmed to be exploited in the wild, not merely disclosed.
    Source("kev", "CISA KEV", "kev", SEC, weight=1.0,
           url="https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
           opts={"days": 14}),
    # /feed and /rss both return the SPA's HTML shell with status 200 - only
    # /feed.xml serves real application/xml. fetch_rss content-type-checks for
    # exactly this failure mode.
    Source("tldrsec", "tl;dr sec", "rss", SEC,
           "https://tldrsec.com/feed.xml", weight=0.9, curated=True),
    Source("projectzero", "Project Zero", "rss", SEC,
           "https://googleprojectzero.blogspot.com/feeds/posts/default", weight=0.95, curated=True),
    Source("portswigger", "PortSwigger Research", "rss", SEC,
           "https://portswigger.net/research/rss", weight=0.85, curated=True),
    Source("unit42", "Unit 42", "rss", SEC,
           "https://feeds.feedburner.com/Unit42", weight=0.7, curated=True),
    Source("krebs", "Krebs on Security", "rss", SEC,
           "https://krebsonsecurity.com/feed/", weight=0.8, curated=True),
    Source("thn", "The Hacker News", "rss", SEC,
           "https://feeds.feedburner.com/TheHackersNews", weight=0.45, curated=True),
    Source("schneier", "Schneier", "rss", SEC,
           "https://www.schneier.com/feed/atom/", weight=0.7, curated=True),
    Source("bleeping", "BleepingComputer", "rss", SEC,
           "https://www.bleepingcomputer.com/feed/", weight=0.5, curated=True, ua=BROWSER_UA),
    Source("arxiv_cr", "arXiv cs.CR", "rss", SEC,
           "https://arxiv.org/rss/cs.CR", weight=0.3),

    # ---------- bluesky ----------
    # The closest free, no-auth stand-in for AI/security Twitter. getAuthorFeed
    # is public - no app password, no API key. Every handle below was checked to
    # resolve AND return posts; handles that 404 or sit empty were dropped rather
    # than left in to fail silently. Add more with: news check-handle <handle>
    Source("bsky", "Bluesky", "bluesky", AI, weight=0.7,
           opts={"handles": [
               "simonwillison.net",
               "karpathy.bsky.social",
               "swyx.io",
               "danluu.com",
               "filippo.abyssdomain.expert",   # crypto/Go security
               "briankrebs.bsky.social",
               "hynek.me",
               "mcp.bsky.social",
           ], "limit": 15}),

    # ---------- tech ----------
    Source("tldr", "TLDR", "rss", TECH,
           "https://tldr.tech/api/rss/tech", weight=0.5, curated=True),
    Source("pragmatic", "Pragmatic Engineer", "rss", TECH,
           "https://newsletter.pragmaticengineer.com/feed", weight=0.7, curated=True),
]


def enabled() -> list[Source]:
    return SOURCES


def by_key(key: str) -> Source | None:
    return next((s for s in SOURCES if s.key == key), None)
