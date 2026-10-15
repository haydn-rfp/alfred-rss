#!/usr/bin/python3
"""Alfred RSS: an RSS reader for Alfred.

Commands:
  list [query]    Script Filter: actions + unread items; typing searches every item;
                  "add <url>" (or a pasted URL) offers to subscribe
  add <url>       Add a feed (accepts a feed URL or a site URL with feed autodiscovery)
  open <arg>      Act on a result; $action is open | markread | markfeed | markall | add

Items are kept in an archive (archive.json) even after they drop out of the feed,
so old articles and episodes stay searchable.
"""

import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlparse

DATA_DIR = os.environ.get("alfred_workflow_data") or os.path.expanduser(
    "~/Library/Application Support/Alfred/Workflow Data/uk.co.rosefpsolutions.rssreader"
)
CACHE_DIR = os.environ.get("alfred_workflow_cache") or os.path.join(DATA_DIR, "cache")
FEEDS_FILE = os.path.join(DATA_DIR, "feeds.json")
READ_FILE = os.path.join(DATA_DIR, "read.json")
SPOTIFY_FILE = os.path.join(DATA_DIR, "spotify.json")  # link -> Spotify episode id
HEALTH_FILE = os.path.join(DATA_DIR, "health.json")  # url -> {"ok", "error", "since", "fallback"}
CATEGORIES_FILE = os.path.join(DATA_DIR, "categories.json")  # slug -> {"name", "feeds": [urls]}
ARCHIVE_FILE = os.path.join(DATA_DIR, "archive.json")
LEGACY_CACHE_FILE = os.path.join(CACHE_DIR, "items.json")
ICON_DIR = os.path.join(DATA_DIR, "icons")
ARCHIVE_VERSION = 1



def setting(name, default):
    """Integer from Alfred's Workflow Configuration (passed as an environment variable)."""
    try:
        return int(os.environ.get(name, ""))
    except ValueError:
        return default


# User-configurable in Alfred: Workflows → Alfred RSS → Configure Workflow
ARCHIVE_LIMIT = setting("archive_limit", 250)    # items kept per feed; 0 = newest only (no history), -1 = unlimited
PER_FEED = setting("unread_per_feed", 25)        # newest items per feed eligible for the unread list
CACHE_TTL = setting("refresh_minutes", 15) * 60  # seconds before feeds are fetched again
SHOW_FEED_NAME = os.environ.get("show_feed_name", "1") != "0"  # feed / channel name under each title
SHOW_SUMMARY = os.environ.get("show_summary", "1") != "0"      # start of the description under each title
PLAY_IN_SPOTIFY = os.environ.get("spotify", "1") != "0"        # play Spotify-hosted episodes in the Spotify app
SOUNDS = os.environ.get("sounds", "1") != "0"                  # chime when a feed is added, low tone when it can't be
ALFRED_PLAYER = os.environ.get("alfred_player") == "1"         # ↵ plays podcasts / YouTube in Alfred RSS Player
PLAYER_APPS = ("~/Applications/Alfred RSS Player.app", "/Applications/Alfred RSS Player.app")
PLAYER_URL = "https://github.com/haydn-rfp/alfred-rss-player"
MAX_ITEMS = 300
USER_AGENT = "Mozilla/5.0 (Macintosh) AlfredRSS/1.12"

ATOM = "{http://www.w3.org/2005/Atom}"
RDF_ITEM = "{http://purl.org/rss/1.0/}item"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"
MEDIA = "{http://search.yahoo.com/mrss/}"  # YouTube puts video descriptions in media:group


# ---------- storage ----------

def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---------- fetching & parsing ----------

def fetch(url):
    result = subprocess.run(
        ["curl", "-sSLf", "--compressed", "--max-time", "12", "-A", USER_AGENT, url],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "download failed")
    return result.stdout


def text(el, *paths):
    for path in paths:
        found = el.find(path)
        if found is not None and (found.text or "").strip():
            return found.text.strip()
    return ""


def parse_date(value):
    if not value:
        return 0
    try:
        return parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        v = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return 0


def clean(s):
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# WordPress's default excerpt: "The post <title> appeared first on <site>."
WP_BOILERPLATE = re.compile(r"\s*The post .{0,300}? appeared first on .{0,120}?\.?\s*$", re.S)


def summary_of(entry):
    """Preview text for an RSS item: the description, or the full content when the description is boilerplate."""
    for path in ("description", "{http://purl.org/rss/1.0/}description", CONTENT_NS + "encoded"):
        value = WP_BOILERPLATE.sub("", clean(text(entry, path)))
        if value:
            return value[:300]
    return ""


AUDIO_EXT = re.compile(r"\.(mp3|m4a|aac|ogg|opus|wav)(\?|$)", re.I)


def audio_of(entry):
    """The item's audio file (podcast enclosure), or ''."""
    enclosure = entry.find("enclosure")
    if enclosure is not None and enclosure.get("url"):
        if (enclosure.get("type") or "").startswith("audio") or AUDIO_EXT.search(enclosure.get("url")):
            return enclosure.get("url")
    return ""


def item_link(entry):
    """Best web link for an RSS item: <link> text, <link href>, a URL guid, then the enclosure (podcasts)."""
    for l in list(entry.findall("link")) + list(entry.findall(ATOM + "link")) + \
             list(entry.findall("{http://purl.org/rss/1.0/}link")):
        if (l.text or "").strip():
            return l.text.strip()
        if l.get("href") and l.get("rel", "alternate") == "alternate":
            return l.get("href")
    guid = text(entry, "guid")
    if re.match(r"^https?://", guid):
        return guid
    enclosure = entry.find("enclosure")
    if enclosure is not None and enclosure.get("url"):
        return enclosure.get("url")
    return ""


def parse_feed(raw, feed_url):
    """Return (feed_title, site_url, [items newest first]) for RSS 2.0, RSS 1.0/RDF or Atom."""
    title, site, items = _parse_feed(raw, feed_url)
    items = [i for i in items if i["link"]]
    items.sort(key=lambda i: i["date"], reverse=True)
    return title, site, items


class NotAFeed(ValueError):
    pass


def _parse_feed(raw, feed_url):
    root = ET.fromstring(raw)
    if root.tag != ATOM + "feed" and root.find("channel") is None and \
            root.find("{http://purl.org/rss/1.0/}channel") is None:
        raise NotAFeed("not an RSS or Atom feed")  # e.g. XHTML pages or sitemaps
    items = []

    if root.tag == ATOM + "feed":
        title = text(root, ATOM + "title")
        site = ""
        for l in root.findall(ATOM + "link"):
            if l.get("rel", "alternate") == "alternate" and l.get("href"):
                site = urljoin(feed_url, l.get("href"))
                break
        for entry in root.findall(ATOM + "entry"):
            link = ""
            for l in entry.findall(ATOM + "link"):
                if l.get("rel", "alternate") == "alternate" and l.get("href"):
                    link = l.get("href")
                    break
            items.append({
                "id": text(entry, ATOM + "id") or link,
                "title": clean(text(entry, ATOM + "title")) or "(untitled)",
                "link": urljoin(feed_url, link),
                "date": parse_date(text(entry, ATOM + "updated", ATOM + "published")),
                "summary": clean(text(entry, ATOM + "summary", ATOM + "content",
                                      MEDIA + "group/" + MEDIA + "description"))[:300],
            })
        return clean(title), site, items

    channel = root.find("channel")
    title = text(channel, "title") if channel is not None else ""
    site = text(channel, "link") if channel is not None else ""
    entries = root.iter("item") if channel is not None else root.iter(RDF_ITEM)
    if channel is None:
        rdf_channel = root.find("{http://purl.org/rss/1.0/}channel")
        if rdf_channel is not None:
            title = text(rdf_channel, "{http://purl.org/rss/1.0/}title")
            site = text(rdf_channel, "{http://purl.org/rss/1.0/}link")
    for entry in entries:
        link = item_link(entry)
        items.append({
            "id": text(entry, "guid") or link or text(entry, "title"),
            "title": clean(text(entry, "title", "{http://purl.org/rss/1.0/}title")) or "(untitled)",
            "link": urljoin(feed_url, link),
            "date": parse_date(text(entry, "pubDate", DC_DATE)),
            "summary": summary_of(entry),
            "audio": audio_of(entry),
        })
    return clean(title), urljoin(feed_url, site), items


def discover_feed(html, page_url):
    """Find a <link rel="alternate" type="application/rss+xml|atom+xml"> on a web page."""
    for tag in re.findall(r"<link\b[^>]*>", html, re.I):
        if re.search(r"rel=[\"']?alternate", tag, re.I) and re.search(
            r"type=[\"']?application/(rss|atom)\+xml", tag, re.I
        ):
            m = re.search(r"href=[\"']([^\"']+)", tag, re.I)
            if m:
                return urljoin(page_url, m.group(1).replace("&amp;", "&"))
    return None


def find_icon_urls(site):
    """Candidate icon URLs for a site: apple-touch-icon first (larger), then rel=icon, then /favicon.ico.

    YouTube channels use the channel's profile picture (its og:image) instead of YouTube's logo.
    """
    candidates = []
    try:
        page = fetch(site).decode("utf-8", errors="replace")
        if re.search(r"(^|\.)youtube\.com/(channel/|@)", site):
            m = re.search(r"<meta\s+property=[\"']og:image[\"']\s+content=[\"']([^\"']+)", page, re.I)
            if m:
                candidates.append(html.unescape(m.group(1)))
        for rel in ("apple-touch-icon", "icon"):
            for tag in re.findall(r"<link\b[^>]*>", page, re.I):
                if re.search(r"rel=[\"'][^\"']*\b" + rel + r"\b", tag, re.I):
                    m = re.search(r"href=[\"']([^\"']+)", tag, re.I)
                    if m:
                        candidates.append(urljoin(site, html.unescape(m.group(1))))
    except Exception:
        pass
    candidates.append(urljoin(site, "/favicon.ico"))
    return candidates


def icon_path(feed_url):
    return os.path.join(ICON_DIR, hashlib.md5(feed_url.encode()).hexdigest() + ".png")


def ensure_icon(feed_url, site):
    """Download the feed's site icon once, converted to PNG. Returns the path or None."""
    path = icon_path(feed_url)
    if os.path.exists(path):
        return path
    if not site:
        return None
    os.makedirs(ICON_DIR, exist_ok=True)
    for url in find_icon_urls(site):
        try:
            data = fetch(url)
        except Exception:
            continue
        head = data[:200].lstrip().lower()
        if len(data) < 64 or head.startswith(b"<!doctype") or head.startswith(b"<html"):
            continue
        raw = path + ".src"
        with open(raw, "wb") as f:
            f.write(data)
        ok = subprocess.run(["sips", "-s", "format", "png", "-Z", "128", raw, "--out", path],
                            capture_output=True).returncode == 0 and os.path.exists(path)
        os.remove(raw)
        if ok:
            return path
    return None


# ---------- sites without RSS ----------
# Feeds can come from three kinds of source, stored as "kind" in feeds.json:
#   rss      a real RSS/Atom feed (advertised, or found at a common address)
#   sitemap  the site's sitemap (news sitemaps carry titles and publish times)
#   page     links read from a listing page, dated when first seen

SM = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
NEWS = "{http://www.google.com/schemas/sitemap-news/0.9}"
COMMON_FEED_PATHS = ("feed", "rss", "rss.xml", "feed.xml", "atom.xml", "index.xml", "feed/atom", "?feed=rss2")
SITEMAP_ITEMS = 100  # newest sitemap entries considered per refresh


def site_root(url):
    p = urlparse(url)
    return "%s://%s/" % (p.scheme, p.netloc)


def first_success(candidates, attempt):
    """Run attempt(c) for each candidate in parallel; return the first non-None result in candidate order."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(attempt, candidates):
            if result:
                return result
    return None


def probe_feeds(url):
    """Look for an unadvertised feed at common addresses, under the given page and at the site root."""
    bases = []
    for base in (url if url.endswith("/") else url + "/", site_root(url)):
        if base not in bases:
            bases.append(base)

    def attempt(candidate):
        try:
            title, site, items = parse_feed(fetch(candidate), candidate)
        except Exception:
            return None
        return (candidate, title, site, items) if items else None

    return first_success([urljoin(b, p) for b in bases for p in COMMON_FEED_PATHS], attempt)


def slug_title(link):
    """Readable title from a URL slug (/2026/10/my-great-post → 'My great post'), or '' for ids."""
    seg = re.sub(r"\.\w+$", "", urlparse(link).path.rstrip("/").rsplit("/", 1)[-1])
    if not seg or re.fullmatch(r"[0-9a-fA-F-]{16,}|\d+", seg):
        return ""
    return seg.replace("-", " ").replace("_", " ").strip().capitalize()


def parse_sitemap(raw, sitemap_url):
    """(title, site, items) from a <urlset> sitemap, newest first. Raises NotAFeed for anything else."""
    root = ET.fromstring(raw)
    if root.tag != SM + "urlset":
        raise NotAFeed("not a sitemap")
    publication, items = "", []
    for u in root.findall(SM + "url"):
        loc = text(u, SM + "loc")
        if not loc:
            continue
        title, date = "", 0
        news = u.find(NEWS + "news")
        if news is not None:
            title = clean(text(news, NEWS + "title"))
            date = parse_date(text(news, NEWS + "publication_date"))
            publication = publication or text(news, NEWS + "publication/" + NEWS + "name")
        items.append({"id": loc, "link": loc, "summary": "",
                      "title": title or slug_title(loc) or loc,
                      "date": date or parse_date(text(u, SM + "lastmod"))})
    items.sort(key=lambda i: i["date"], reverse=True)
    name = publication or urlparse(sitemap_url).netloc.replace("www.", "")
    return name, site_root(sitemap_url), items[:SITEMAP_ITEMS]


def find_sitemap(url):
    """Find a sitemap usable as a feed: a news sitemap if the site has one, else the freshest dated one.

    Returns (sitemap_url, title, site, items) or None.
    """
    root_url = site_root(url)
    queue = []
    try:
        robots = fetch(urljoin(root_url, "robots.txt")).decode("utf-8", errors="replace")
        queue += re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
    except Exception:
        pass
    for path in ("news-sitemap.xml", "sitemap_news.xml", "sitemap.xml", "sitemap_index.xml"):
        if urljoin(root_url, path) not in queue:
            queue.append(urljoin(root_url, path))

    best, seen = None, set()
    while queue and len(seen) < 10:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        try:
            raw = fetch(sm)
            root = ET.fromstring(raw)
        except Exception:
            continue
        if root.tag == SM + "sitemapindex":
            children = [(text(c, SM + "loc"), parse_date(text(c, SM + "lastmod"))) for c in root.findall(SM + "sitemap")]
            news = [loc for loc, _ in children if "news" in loc.lower()]
            freshest = [loc for loc, _ in sorted((c for c in children if c[1]), key=lambda c: c[1], reverse=True)[:2]]
            queue = news + freshest + queue
            continue
        try:
            title, site, items = parse_sitemap(raw, sm)
        except Exception:
            continue
        if not any(i["date"] for i in items):
            continue  # undated sitemaps can't say what's new
        is_news = root.find(SM + "url/" + NEWS + "news") is not None
        score = (is_news, items[0]["date"])
        if best is None or score > best[0]:
            best = (score, (sm, title, site, items))
        if is_news:
            break
    return best[1] if best else None


class _LinkCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.title, self._href, self._text, self._in_title = [], "", None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)
        if self._in_title:
            self.title += data


def parse_page(raw, page_url):
    """(title, site, items) from a listing page: the biggest group of headline-like links on it."""
    collector = _LinkCollector()
    collector.feed(raw.decode("utf-8", errors="replace"))
    host = urlparse(page_url).netloc
    groups, seen = {}, set()
    for href, label in collector.links:
        link = urljoin(page_url, href).split("#")[0]
        path = urlparse(link).path
        if urlparse(link).netloc != host or link in seen or link.rstrip("/") == page_url.rstrip("/"):
            continue
        if len(label) < 20 or len(label.split()) < 4 or path.count("/") < 2:
            continue  # headlines are long; menus and buttons aren't
        seen.add(link)
        prefix = "/".join(path.strip("/").split("/")[:1])  # group by first path segment
        groups.setdefault(prefix, []).append({"id": link, "link": link, "title": clean(label),
                                              "summary": "", "date": 0})
    best = max(groups.values(), key=len, default=[])
    if len(best) < 4:
        raise NotAFeed("no list of articles found on the page")
    return clean(collector.title).split(" | ")[0].split(" - ")[0], site_root(page_url), best


# ---------- YouTube fallback ----------
# YouTube's feed service regularly returns 404/500 for every channel for hours or days at a time.
# When that happens, read the channel's Videos page instead (same video ids, so read marks still match).

YT_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "mo": 2592000, "y": 31536000}
YT_AGO = re.compile(r"(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|wks?|months?|mos?|years?|yrs?)\s+ago")


def approx_date(text_ago):
    """'3 days ago' / 'Streamed 2 hours ago' → a timestamp (approximate)."""
    m = YT_AGO.search(text_ago or "")
    if not m:
        return 0
    unit = m.group(2)
    key = "mo" if unit.startswith("mo") else "m" if unit.startswith("mi") else unit[0]
    return time.time() - int(m.group(1)) * YT_UNITS[key]


def youtube_videos_page(channel_id):
    """(title, site, items) from a channel's Videos page."""
    site = "https://www.youtube.com/channel/" + channel_id
    page = fetch(site + "/videos").decode("utf-8", errors="replace")
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page, re.S)
    if not m:
        raise NotAFeed("YouTube's Videos page couldn't be read")
    items = []

    def walk(o):
        if isinstance(o, dict):
            if "lockupViewModel" in o:  # current page format
                v = o["lockupViewModel"]
                meta = v.get("metadata", {}).get("lockupMetadataViewModel", {})
                rows = meta.get("metadata", {}).get("contentMetadataViewModel", {}).get("metadataRows", [])
                parts = [p.get("text", {}).get("content", "") for r in rows for p in r.get("metadataParts", [])]
                add(v.get("contentId"), meta.get("title", {}).get("content", ""), " ".join(parts))
                return
            if "videoRenderer" in o:  # older page format
                v = o["videoRenderer"]
                add(v.get("videoId"), "".join(r.get("text", "") for r in v.get("title", {}).get("runs", [])),
                    v.get("publishedTimeText", {}).get("simpleText", ""))
                return
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    def add(video_id, title, ago):
        if video_id and title:
            items.append({"id": "yt:video:" + video_id, "title": title, "summary": "", "approx": True,
                          "link": "https://www.youtube.com/watch?v=" + video_id, "date": approx_date(ago)})

    walk(json.loads(m.group(1)))
    if not items:
        raise NotAFeed("no videos found on the channel's Videos page")
    name = re.search(r'<meta property="og:title" content="([^"]+)"', page)
    return html.unescape(name.group(1)) if name else "", site, items


def load_source(url, kind="rss", scope=""):
    """Fetch and parse any kind of source. scope limits sitemap items to a path, e.g. /opinion/.

    Returns (title, site, items, note); note is "" normally, or says when a fallback was used.
    """
    channel = re.search(r"youtube\.com/feeds/videos\.xml\?channel_id=(UC[\w-]{22})", url)
    if channel:
        try:
            return parse_feed(fetch(url), url) + ("",)
        except Exception:
            title, site, items = youtube_videos_page(channel.group(1))
            return title, site, items, "YouTube's feed is down, so this channel is read from its Videos page"
    raw = fetch(url)
    if kind == "sitemap":
        title, site, items = parse_sitemap(raw, url)
    elif kind == "page":
        title, site, items = parse_page(raw, url)
    else:
        return parse_feed(raw, url) + ("",)
    if scope:
        items = [i for i in items if urlparse(i["link"]).path.startswith(scope)]
    return title, site, items, ""


def refresh(feeds):
    """Fetch the given feeds ({url: info}) concurrently.

    Returns {url: {"title", "site", "items", "note"}} for successes and {url: {"error": reason}} for failures.
    """
    def one(entry):
        url, info = entry
        try:
            title, site, items, note = load_source(url, info.get("kind", "rss"), info.get("scope", ""))
            ensure_icon(url, site or url)
            return url, {"title": title, "site": site, "items": items, "note": note}
        except Exception as e:
            return url, {"error": friendly_error(e)}

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, list(feeds.items())))


# ---------- archive ----------

def load_archive():
    archive = load(ARCHIVE_FILE, None)
    if archive is None or archive.get("version") != ARCHIVE_VERSION:
        archive = {"version": ARCHIVE_VERSION, "fetched": 0, "feeds": {}}
        # Carry over items from the older cache format so nothing already seen is lost
        legacy = load(LEGACY_CACHE_FILE, {})
        for url, data in legacy.get("feeds", {}).items():
            archive["feeds"][url] = {"title": data.get("title", ""), "site": data.get("site", ""),
                                     "items": [i for i in data.get("items", []) if i.get("link")]}
    return archive


def merge(archive_feed, fresh_items):
    """Merge freshly fetched items into a feed's archive (fresh copy wins), newest first.

    Returns the ids of items that weren't in the archive before.
    """
    by_id = {i["id"]: i for i in archive_feed.get("items", [])}
    new_ids = [i["id"] for i in fresh_items if i["id"] not in by_id]
    fresh_ids = []
    now = time.time()
    for index, item in enumerate(fresh_items):
        old = by_id.get(item["id"])
        if item.pop("approx", False) and old:
            item = dict(old)  # fallback copy is rougher than what's archived (exact date, description)
        if not item["date"]:  # undated (page) items: date them when first seen, keeping page order
            item["date"] = old["date"] if old else now - index
        by_id[item["id"]] = item
        fresh_ids.append(item["id"])
    items = sorted(by_id.values(), key=lambda i: i["date"], reverse=True)
    if ARCHIVE_LIMIT >= 0:  # 0 = no history: keep just the items that can appear as unread
        items = items[:max(ARCHIVE_LIMIT, PER_FEED)]
    archive_feed["items"] = items
    archive_feed["current"] = fresh_ids  # ids present in the live feed right now
    return new_ids


def update(feeds, archive, urls):
    """Fetch the given feeds into the archive. Returns the urls that failed."""
    results = refresh({u: feeds[u] for u in urls})
    failed = []
    health = load(HEALTH_FILE, {})
    now = time.time()
    for url, res in results.items():
        status = health.setdefault(url, {})
        if "error" in res:
            failed.append(url)
            status.setdefault("since", now)
            status["error"], status["tried"] = res["error"], now
            continue
        health[url] = {"ok": now, "tried": now, "fallback": res["note"]} if res["note"] else {"ok": now, "tried": now}
        feed = archive["feeds"].setdefault(url, {})
        feed["title"], feed["site"] = res["title"], res["site"]
        newest_before = max((i["date"] for i in feed.get("items", [])), default=0)
        dates = {i["id"]: i["date"] for i in res["items"]}
        new_ids = merge(feed, res["items"])
        if res["note"] and newest_before:
            # A fallback source can list older items the feed never showed; file them away as read
            # rather than flooding the unread list with weeks-old videos.
            mark([(url, i) for i in new_ids if dates.get(i, 0) < newest_before])
        if res["title"] and feeds[url].get("title") != res["title"] and not feeds[url].get("custom"):
            feeds[url]["title"] = res["title"]
    save(HEALTH_FILE, {u: v for u, v in health.items() if u in feeds})
    archive["feeds"] = {u: v for u, v in archive["feeds"].items() if u in feeds}
    archive["fetched"] = time.time()
    save(ARCHIVE_FILE, archive)
    save(FEEDS_FILE, feeds)

    # Read marks only need keeping for items still in the archive
    read = load(READ_FILE, {})
    for url in list(read):
        if url not in archive["feeds"]:
            del read[url]
            continue
        known = {i["id"] for i in archive["feeds"][url]["items"]}
        read[url] = [i for i in read[url] if i in known]
    save(READ_FILE, read)
    return failed


def unread_window(feed):
    """Ids eligible for the unread list: the newest PER_FEED items in the archive."""
    return {i["id"] for i in feed.get("items", [])[:PER_FEED]}


# ---------- commands ----------

def alfred(items, **extra):
    print(json.dumps(dict(items=items, **extra)))


def ago(ts):
    if not ts:
        return ""
    d = time.time() - ts
    if d < 3600:
        return "%dm ago" % max(1, d // 60)
    if d < 86400:
        return "%dh ago" % (d // 3600)
    if d < 86400 * 30:
        return "%dd ago" % (d // 86400)
    return datetime.fromtimestamp(ts).strftime("%d %b %Y")


def display_name(title):
    """Readable feed name: path-like titles such as 'category/lads-hour' become 'Lads Hour'."""
    if re.fullmatch(r"[\w.-]+(/[\w.-]+)+/?", title or ""):
        return title.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").replace("_", " ").title()
    return title


def alfred_item(item, url, name, is_read, category=None):
    when = ago(item["date"])
    summary = item.get("summary", "")
    if summary == item["title"]:
        summary = ""
    meta = " · ".join(p for p in (name if SHOW_FEED_NAME else "", when) if p)
    if summary and SHOW_SUMMARY:
        meta += " — " + summary
    if is_read:
        meta = "✓ " + meta
    icon = icon_path(url)
    ids = {"item_id": item["id"], "feed_url": url, "step": "act"}
    playable = playable_url(item) if ALFRED_PLAYER else ""
    if playable:
        meta = "▶ " + meta
        ids.update(play_url=playable, play_title=item["title"], play_artist=name,
                   play_artwork=icon if os.path.exists(icon) and not is_youtube(playable) else "")
    return {
        "uid": item["id"],
        "title": item["title"],
        "subtitle": meta,
        "arg": item["link"],
        "icon": {"path": icon} if os.path.exists(icon) else {"path": "icon.png"},
        "match": "%s %s %s" % (item["title"], name, summary),
        "quicklookurl": item["link"],
        "variables": dict(ids, action="play" if playable else "open"),
        "mods": {
            **({"cmd+shift": {"subtitle": "Open in the browser instead", "arg": item["link"],
                              "variables": dict(ids, action="open")}} if playable else {}),
            "cmd": {"subtitle": "Mark as read without opening", "arg": item["link"],
                    "variables": dict(ids, action="markread")},
            "alt": {"subtitle": "Mark everything from %s as read" % name, "arg": item["link"],
                    "variables": dict(ids, action="markfeed")},
            "ctrl": ({"subtitle": "Remove %s from %s" % (name, category["name"]), "arg": item["link"],
                      "variables": dict(ids, action="uncategorise", category=category["slug"])}
                     if category else
                     {"subtitle": "Add %s to a category…" % name, "arg": "",
                      "variables": nav_vars("addto", feed_url=url)}),
        },
        "text": {"copy": item["link"], "largetype": "%s\n\n%s" % (item["title"], summary)},
    }


def prepare(query):
    """Load feeds + archive, fetching if stale. Returns (feeds, archive, failed)."""
    feeds = load(FEEDS_FILE, {})
    archive = load_archive()
    if not feeds:
        return feeds, archive, []
    force = query.strip().lower() == "refresh"
    stale = time.time() - archive.get("fetched", 0) > CACHE_TTL
    missing = [u for u in feeds if u not in archive["feeds"]]
    failed = update(feeds, archive, list(feeds)) if (force or stale or missing) else []
    return feeds, archive, failed


def matches(query, item):
    words = query.lower().split()
    hay = item["match"].lower()
    return all(w in hay for w in words)


def health_rows(feeds, only=None):
    """A single warning row when feeds aren't updating (it stays until they recover)."""
    health = load(HEALTH_FILE, {})
    urls = [u for u in feeds if only is None or u in only]
    failing = [u for u in urls if health.get(u, {}).get("error")]
    if not failing:
        return []
    offline = len(failing) == len(urls) and all(
        "couldn't be found" in health[u]["error"] or "too long" in health[u]["error"] for u in failing)
    if offline:
        return [nav_item("⚠ Can't reach your feeds", "You may be offline · showing saved items · ↵ for details",
                         "health")]
    return [nav_item("⚠ %d feed%s not updating" % (len(failing), "" if len(failing) == 1 else "s"),
                     ", ".join(feed_title(feeds, u) for u in failing)[:90] + " · ↵ for details", "health")]


ADD_PREFIX = re.compile(r"^add\b\s*", re.I)
YT_CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
YT_HANDLE = re.compile(r"^@[\w.-]{3,30}$")
YT_FEED = "https://www.youtube.com/feeds/videos.xml?"


def youtube_feed(url):
    """Feed address for YouTube inputs the page itself can't give us, or None to carry on as normal.

    UC… channel id → channel feed · @handle → channel page · playlist link → playlist feed ·
    video link (watch, youtu.be, shorts, live) → the uploading channel's feed.
    """
    if YT_CHANNEL_ID.match(url):
        return YT_FEED + "channel_id=" + url
    if YT_HANDLE.match(url):
        return "https://www.youtube.com/" + url
    if not re.search(r"(^|//|\.)(youtube\.com|youtu\.be)/", url):
        return None
    playlist = re.search(r"[?&]list=([\w-]+)", url)
    if playlist and "/playlist" in url:
        return YT_FEED + "playlist_id=" + playlist.group(1)
    video = re.search(r"youtu\.be/([\w-]{11})|[?&]v=([\w-]{11})|/(?:shorts|live)/([\w-]{11})", url)
    if video:
        vid = next(g for g in video.groups() if g)
        try:
            page = fetch("https://www.youtube.com/watch?v=" + vid).decode("utf-8", errors="replace")
        except Exception:
            return None
        channel = re.search(r'itemprop="channelId" content="(UC[\w-]{22})"|"channelId":"(UC[\w-]{22})"', page)
        if channel:
            return YT_FEED + "channel_id=" + next(g for g in channel.groups() if g)
    return None
LOOKS_LIKE_URL = re.compile(r"^(https?://)?[\w-]+(\.[\w-]+)*\.[a-z]{2,}(/\S*)?$", re.I)


def add_items(query):
    """Items for the 'add a feed' flow, or None if the query isn't about adding."""
    q = query.strip()
    url = ADD_PREFIX.sub("", q) if ADD_PREFIX.match(q) else (
        q if LOOKS_LIKE_URL.match(q) or YT_CHANNEL_ID.match(q) or YT_HANDLE.match(q) else None)
    if url is None:
        return None
    if not url:
        return [{"title": "Add RSS feed", "subtitle": "Paste a feed or website URL",
                 "icon": {"path": "icon.png"}, "valid": False}]
    return [{"title": "Add feed: " + url, "subtitle": "↵ to subscribe · website addresses work too",
             "arg": url, "icon": {"path": "icon.png"}, "variables": {"action": "add"}}]


UNSUB_PREFIX = re.compile(r"^unsub(scribe)?\b\s*", re.I)


def unsubscribe_items(query):
    """Items listing subscribed feeds for removal, or None if the query isn't about unsubscribing."""
    q = query.strip()
    if not UNSUB_PREFIX.match(q):
        return None
    search = UNSUB_PREFIX.sub("", q).lower().split()
    feeds = load(FEEDS_FILE, {})
    archive = load(ARCHIVE_FILE, {}).get("feeds", {})
    items = []
    for url, info in sorted(feeds.items(), key=lambda kv: (kv[1].get("title") or kv[0]).lower()):
        title = display_name(info.get("title") or url)
        if not all(w in (title + " " + url).lower() for w in search):
            continue
        icon = icon_path(url)
        count = len(archive.get(url, {}).get("items", []))
        items.append({
            "uid": "unsub:" + url,
            "title": title,
            "subtitle": "↵ unsubscribe and delete its %d saved items · %s" % (count, url),
            "arg": url,
            "icon": {"path": icon} if os.path.exists(icon) else {"path": "icon.png"},
            "variables": {"action": "unsubscribe", "feed_url": url},
        })
    if not items:
        items.append({"title": "No subscribed feeds match" if feeds else "You're not subscribed to any feeds",
                      "valid": False})
    return items


def cmd_list(query):
    q = query.strip()
    if q.startswith("#"):  # typed shortcuts: "#" categories, "#new <name>", "#<category> [search]"
        body = q[1:]
        if re.match(r"new(\s|$)", body, re.I):
            return alfred(render("newcategory", body[3:].strip()))
        parts = body.split(None, 1)
        if parts and parts[0].lower() in load(CATEGORIES_FILE, {}):
            return alfred(render("category", parts[1] if len(parts) > 1 else "", category=parts[0].lower()),
                          skipknowledge=True)
        return alfred(render("categories", body))
    adding = add_items(query)
    if adding is not None:
        return alfred(adding)
    removing = unsubscribe_items(query)
    if removing is not None:
        return alfred(removing)
    alfred(render("main", query), skipknowledge=True)


def render_main(query):
    feeds, archive, failed = prepare(query)
    q = query.strip()
    browsing = q.lower() in ("", "refresh")
    found = item_rows(feeds, archive, q, browsing)

    items = []
    if browsing:
        items.append({"title": "Add RSS feed", "subtitle": "Paste a feed or website URL",
                      "autocomplete": "add ", "valid": False, "icon": {"path": "icon.png"}})
        if found:
            items.append({"title": "Mark all as read",
                          "subtitle": "%d unread · they stay searchable" % len(found),
                          "arg": "", "icon": {"path": "icon.png"}, "variables": {"step": "act", "action": "markall"}})
        else:
            items.append({"title": "All caught up", "subtitle": "No unread items · type to search everything",
                          "valid": False, "icon": {"path": "icon.png"}})
        cats = load(CATEGORIES_FILE, {})
        items.append(nav_item("View categories",
                              ("%d categories" % len(cats)) if cats else "Group your feeds, e.g. News, Football, Podcasts",
                              "categories"))
        if feeds:
            items.append({"title": "Unsubscribe from a feed",
                          "subtitle": "Choose from the %d feeds you follow" % len(feeds),
                          "autocomplete": "unsubscribe ", "valid": False, "icon": {"path": "icon.png"}})
    elif not found:
        items.append({"title": "No matches", "subtitle": "Nothing in your feeds matches “%s”" % q,
                      "valid": False})
    return health_rows(feeds) + items + found[:MAX_ITEMS] if browsing else items + found[:MAX_ITEMS]


def item_rows(feeds, archive, q, browsing, only=None, category=None):
    """Alfred items for unread items (browsing) or every matching item (searching), newest first."""
    read = load(READ_FILE, {})
    rows = []
    for url, feed in archive["feeds"].items():
        if only is not None and url not in only:
            continue
        seen = set(read.get(url, []))
        window = unread_window(feed)
        name = display_name(feeds.get(url, {}).get("title") or feed.get("title") or url)
        for item in feed["items"]:
            # Anything older than the unread window counts as read
            is_read = item["id"] in seen or item["id"] not in window
            if browsing and is_read:
                continue  # browsing shows unread only; searching covers everything
            rows.append((item, url, name, is_read))
    rows.sort(key=lambda t: t[0]["date"], reverse=True)
    return [x for x in (alfred_item(*r, category=category) for r in rows) if browsing or matches(q, x)]


# ---------- categories ----------
# Category screens are chained Script Filters: choosing a "nav" item opens the next screen in place.
# Each item says where it leads through variables:
#   step=nav  screen=<main|categories|category|pick|addto|newcategory>  category=<slug>  feed_url=<url>
#   step=act  action=<...>   (handled by the Run Script; it can send the user back to a screen afterwards)

def feed_id(url):
    return hashlib.md5(url.encode()).hexdigest()[:8]


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def feed_title(feeds, url):
    return display_name(feeds.get(url, {}).get("title") or url)


def feed_icon(url):
    icon = icon_path(url)
    return {"path": icon} if os.path.exists(icon) else {"path": "icon.png"}


def nav_vars(screen, category="", feed_url=""):
    return {"step": "nav", "screen": screen, "category": category, "feed_url": feed_url}


def nav_item(title, subtitle, screen, category="", feed_url="", icon=None):
    return {"title": title, "subtitle": subtitle, "arg": "", "icon": icon or {"path": "icon.png"},
            "variables": nav_vars(screen, category, feed_url)}


def act_item(title, subtitle, action, arg="", category="", feed_url="", icon=None, **extra):
    variables = {"step": "act", "action": action, "category": category, "feed_url": feed_url}
    variables.update(extra)
    return {"title": title, "subtitle": subtitle, "arg": arg, "icon": icon or {"path": "icon.png"},
            "variables": variables}


def unread_count(archive, urls):
    read = load(READ_FILE, {})
    return sum(len(unread_window(archive["feeds"].get(u, {})) - set(read.get(u, []))) for u in urls)


def render(screen, query="", category="", feed_url=""):
    """Items for one screen. query is whatever the user has typed on that screen."""
    q = query.strip()
    cats = load(CATEGORIES_FILE, {})
    feeds = load(FEEDS_FILE, {})

    if screen == "main" or (screen in ("category", "pick") and category not in cats):
        return render_main(query)

    if screen == "health":
        health = load(HEALTH_FILE, {})
        items = [nav_item("← Back", "All unread items", "main"),
                 act_item("Check all feeds now", "Fetch every feed again", "refresh")]
        failing = sorted((u for u in feeds if health.get(u, {}).get("error")), key=lambda u: feed_title(feeds, u).lower())
        for url in failing:
            h = health[url]
            last = ("last updated " + ago(h["ok"])) if h.get("ok") else "never updated"
            items.append({"title": feed_title(feeds, url), "icon": feed_icon(url), "arg": url,
                          "subtitle": "%s · failing since %s · %s · ↵ open the feed address" % (
                              h["error"][0].upper() + h["error"][1:], ago(h["since"]).replace(" ago", " ago"), last),
                          "variables": {"step": "act", "action": "openurl"}})
        for url in sorted((u for u in feeds if health.get(u, {}).get("fallback")), key=lambda u: feed_title(feeds, u).lower()):
            items.append({"title": feed_title(feeds, url), "icon": feed_icon(url), "valid": False,
                          "subtitle": "Working · " + health[url]["fallback"]})
        if not failing:
            items.append({"title": "All feeds are updating", "valid": False, "icon": {"path": "icon.png"}})
        ok = len(feeds) - len(failing)
        items.append({"title": "%d of %d feeds are fine" % (ok, len(feeds)), "valid": False, "icon": {"path": "icon.png"}})
        return items

    if screen == "categories":
        archive = load_archive()
        items = [nav_item("← Back", "All unread items", "main"),
                 nav_item("Add a category", "Create a new category, then choose its feeds", "newcategory")]
        for slug, cat in sorted(cats.items(), key=lambda kv: kv[1]["name"].lower()):
            if q and q.lower() not in cat["name"].lower():
                continue
            n = len(cat["feeds"])
            item = nav_item(cat["name"], "%d feed%s · %d unread · ⌘↵ to delete" % (
                n, "" if n == 1 else "s", unread_count(archive, cat["feeds"])), "category", category=slug)
            item["mods"] = {"cmd": {"arg": "", "subtitle": "Delete “%s” (your feeds are kept)" % cat["name"],
                                    "variables": {"step": "act", "action": "deletecategory", "category": slug}}}
            items.append(item)
        if not cats:
            items.append({"title": "No categories yet", "subtitle": "Add one to group your feeds", "valid": False})
        return items

    if screen == "newcategory":
        back = nav_item("← Back", "", "addto" if feed_url else "categories", feed_url=feed_url)
        if not q:
            return [{"title": "Type a name for the new category", "subtitle": "e.g. Football, Podcasts, Work",
                     "valid": False, "icon": {"path": "icon.png"}}, back]
        slug = slugify(q)
        if slug in cats:
            return [nav_item("“%s” already exists" % cats[slug]["name"], "↵ to open it", "category", category=slug), back]
        then = "then add %s to it" % feed_title(feeds, feed_url) if feed_url else "then choose its feeds"
        return [act_item("Create category “%s”" % q, "↵ to create it, " + then, "newcategory", arg=q,
                         feed_url=feed_url), back]

    if screen == "category":
        cat = dict(cats[category], slug=category)
        feeds, archive, failed = prepare("")
        only = set(cat["feeds"])
        found = item_rows(feeds, archive, q, not q, only=only, category=cat)
        items = []
        if not q:
            items += [nav_item("← Back", "All categories", "categories"),
                      nav_item("Add to category", "Choose which feeds are in %s" % cat["name"], "pick", category=category)]
            if not only:
                items.append({"title": "No feeds in %s yet" % cat["name"], "valid": False})
            elif not found:
                items.append({"title": "All caught up in %s" % cat["name"],
                              "subtitle": "Type to search everything in this category", "valid": False})
        elif not found:
            items.append({"title": "No matches in %s" % cat["name"], "valid": False})
        return (health_rows(feeds, only) if not q else []) + items + found[:MAX_ITEMS]

    if screen == "pick":
        cat = cats[category]
        items = [nav_item("Done", "Back to %s" % cat["name"], "category", category=category)]
        for url in sorted(feeds, key=lambda u: feed_title(feeds, u).lower()):
            title = feed_title(feeds, url)
            if not all(w in (title + " " + url).lower() for w in q.lower().split()):
                continue
            inside = url in cat["feeds"]
            items.append(act_item(("✓ " if inside else "") + title,
                                  ("In %s · ↵ to remove" if inside else "↵ to add to %s") % cat["name"],
                                  "togglecategory", category=category, feed_url=url, icon=feed_icon(url)))
        return items

    if screen == "addto":
        name = feed_title(feeds, feed_url)
        items = [nav_item("← Back", "All unread items", "main"),
                 nav_item("Add a category", "Create a new category with %s in it" % name, "newcategory",
                          feed_url=feed_url)]
        for slug, cat in sorted(cats.items(), key=lambda kv: kv[1]["name"].lower()):
            if q and q.lower() not in cat["name"].lower():
                continue
            if feed_url in cat["feeds"]:
                items.append({"title": cat["name"], "subtitle": "✓ %s is already in this category" % name,
                              "valid": False, "icon": feed_icon(feed_url)})
            else:
                items.append(act_item(cat["name"], "↵ add %s to this category" % name, "categorise",
                                      category=slug, feed_url=feed_url, icon=feed_icon(feed_url)))
        return items

    return render_main(query)


def cmd_nav(query):
    """Script Filter for chained screens; where we are comes from the chosen item's variables."""
    env = os.environ
    alfred(render(env.get("screen", "main"), query, env.get("category", ""), env.get("feed_url", "")),
           skipknowledge=True)


def set_category(url, slug, member):
    cats = load(CATEGORIES_FILE, {})
    if slug not in cats:
        return None
    feeds = cats[slug]["feeds"]
    if member and url not in feeds:
        feeds.append(url)
    elif not member and url in feeds:
        feeds.remove(url)
    save(CATEGORIES_FILE, cats)
    return cats[slug]["name"]


# ---------- Spotify ----------

def spotify_episode(link):
    """Spotify episode id for a link, or None.

    Handles open.spotify.com/episode/<id> directly, and Spotify for Creators pages
    (podcasters./creators.spotify.com/.../episodes/<slug>-<code>) by reading the id from the page.
    """
    m = re.search(r"open\.spotify\.com/episode/([A-Za-z0-9]{22})", link)
    if m:
        return m.group(1)
    m = re.search(r"(?:podcasters|creators)\.spotify\.com/.+/episodes/.*-(e[a-z0-9]+)/?$", link)
    if not m:
        return None
    known = load(SPOTIFY_FILE, {})
    if link in known:
        return known[link]
    code = m.group(1)
    try:
        page = fetch(link).decode("utf-8", errors="replace").replace("\\u002F", "/")
    except Exception:
        return None
    # Each episode on the page is a JSON object containing "episodeId":"<code>" and its "spotifyUrl"
    start = page.find('"episodeId":"%s"' % code)
    if start < 0:
        return None
    end = page.find('"episodeId":"', start + 1)
    found = re.search(r'"spotifyUrl":"https://open\.spotify\.com/episode/([A-Za-z0-9]{22})',
                      page[start:end if end > 0 else len(page)])
    if not found:
        return None
    known[link] = found.group(1)
    save(SPOTIFY_FILE, known)
    return found.group(1)


def spotify_running():
    return subprocess.run(["pgrep", "-x", "Spotify"], capture_output=True).returncode == 0


def play_in_spotify(episode_id):
    """Open Spotify (if needed) and start playing the episode. Returns True on success."""
    if not os.path.exists("/Applications/Spotify.app") and not os.path.exists(
            os.path.expanduser("~/Applications/Spotify.app")):
        return False
    if not spotify_running():
        subprocess.run(["open", "-g", "-a", "Spotify"])
        for _ in range(40):  # wait up to ~10s for Spotify to be ready for commands
            time.sleep(0.25)
            if spotify_running():
                break
        time.sleep(1.5)
    uri = "spotify:episode:" + episode_id
    for _ in range(3):
        ok = subprocess.run(["osascript", "-e", 'tell application "Spotify" to play track "%s"' % uri],
                            capture_output=True).returncode == 0
        if ok:
            subprocess.run(["open", "-a", "Spotify"])
            return True
        time.sleep(1)
    return False


def open_link(link):
    episode = spotify_episode(link) if PLAY_IN_SPOTIFY else None
    if episode and play_in_spotify(episode):
        return
    subprocess.run(["open", link])


YOUTUBE_VIDEO = re.compile(r"(youtube\.com/(watch\?|shorts/|live/)|youtu\.be/)")


def is_youtube(link):
    return bool(YOUTUBE_VIDEO.search(link or ""))


def player_installed():
    return any(os.path.exists(os.path.expanduser(p)) for p in PLAYER_APPS)


def playable_url(item):
    """What Alfred Player can play for an item: its audio file or YouTube video, else ''."""
    if item.get("audio"):
        return item["audio"]
    link = item.get("link", "")
    if AUDIO_EXT.search(link) or is_youtube(link):
        return link
    return ""


def play_in_player(env, link):
    """Hand the item to Alfred RSS Player, or open it normally if the player isn't installed."""
    if not player_installed():
        open_link(link)
        print("Alfred RSS Player isn't installed, so this opened normally. Get it at " + PLAYER_URL)
        return
    query = "&".join("%s=%s" % (k, quote(env.get("play_" + k, ""), safe=""))
                     for k in ("url", "title", "artist", "artwork"))
    query += "&id=" + quote(env.get("item_id", ""), safe="")
    subprocess.run(["open", "-g", "alfredplayer://play?" + query])


def mark(pairs):
    """Mark (feed_url, item_id) pairs as read."""
    read = load(READ_FILE, {})
    for url, item_id in pairs:
        ids = read.setdefault(url, [])
        if item_id not in ids:
            ids.append(item_id)
    save(READ_FILE, read)


def cmd_act(link):
    action = "nav" if os.environ.get("step") == "nav" else os.environ.get("action", "open")
    item_id, feed_url = os.environ.get("item_id", ""), os.environ.get("feed_url", "")
    archive = load_archive()

    if action in ("open", "markread", "play"):
        mark([(feed_url, item_id)])
        if action == "play":
            play_in_player(os.environ, link)
        elif action == "open":
            open_link(link)
    elif action == "markfeed":
        feed = archive["feeds"].get(feed_url, {})
        mark([(feed_url, i) for i in unread_window(feed)])
        name = load(FEEDS_FILE, {}).get(feed_url, {}).get("title") or feed.get("title") or "feed"
        print("Marked everything from %s as read" % name)
    elif action == "markall":
        mark([(url, i) for url, feed in archive["feeds"].items() for i in unread_window(feed)])
        print("Marked all as read")
    elif action == "add":
        play_sound("Glass" if cmd_add(link) else "Basso")
    elif action == "unsubscribe":
        cmd_unsubscribe(feed_url or link)
    elif action == "refresh":
        feeds = load(FEEDS_FILE, {})
        failed = update(feeds, load_archive(), list(feeds))
        print("All %d feeds updated" % len(feeds) if not failed else "%d of %d feeds couldn't be updated" % (len(failed), len(feeds)))
        go("health")
    elif action == "openurl":
        subprocess.run(["open", link])
    elif action == "nav":  # a screen chosen deeper than the chain goes: continue via the external trigger
        go(os.environ.get("screen", "main"), os.environ.get("category", ""), feed_url)
    elif action == "newcategory":
        cats = load(CATEGORIES_FILE, {})
        slug = slugify(link)
        cats.setdefault(slug, {"name": link.strip(), "feeds": []})
        save(CATEGORIES_FILE, cats)
        if feed_url:
            set_category(feed_url, slug, True)
            print("Created %s with %s in it" % (link.strip(), feed_title(load(FEEDS_FILE, {}), feed_url)))
        else:
            go("pick", slug)
    elif action == "togglecategory":
        slug = os.environ.get("category", "")
        cats = load(CATEGORIES_FILE, {})
        if slug in cats:
            set_category(feed_url, slug, feed_url not in cats[slug]["feeds"])
        go("pick", slug)
    elif action == "categorise":
        name = set_category(feed_url, os.environ.get("category", ""), True)
        if name:
            print("Added %s to %s" % (feed_title(load(FEEDS_FILE, {}), feed_url), name))
    elif action == "uncategorise":
        slug = os.environ.get("category", "")
        name = set_category(feed_url, slug, False)
        if name:
            print("Removed %s from %s" % (feed_title(load(FEEDS_FILE, {}), feed_url), name))
        go("category", slug)
    elif action == "deletecategory":
        cats = load(CATEGORIES_FILE, {})
        removed = cats.pop(os.environ.get("category", ""), None)
        save(CATEGORIES_FILE, cats)
        if removed:
            print("Deleted category " + removed["name"])
        go("categories")


NEXT = {}


def go(screen, category="", feed_url=""):
    """After an action, show this screen again (the Run Script's output routes to the 'nav' external trigger)."""
    NEXT.update(nav_vars(screen, category, feed_url))


def play_sound(name):
    """Play a macOS alert sound quietly in the background (doesn't hold up Alfred)."""
    path = "/System/Library/Sounds/%s.aiff" % name
    if SOUNDS and os.path.exists(path):
        subprocess.Popen(["afplay", "-v", "0.5", path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)


def friendly_error(e):
    reason = str(e)
    if "Could not resolve host" in reason:
        return "that website couldn't be found — check the address"
    if re.search(r"returned error: (404|410)", reason):
        return "that page doesn't exist (404) — check the address"
    if re.search(r"returned error: (401|403)", reason):
        return "the website refused access (it may need a login, or block automated readers)"
    if "returned error" in reason:
        return "the website returned an error (%s)" % reason.rsplit(":", 1)[-1].strip()
    if "timed out" in reason.lower():
        return "the website took too long to respond"
    if isinstance(e, (ET.ParseError, NotAFeed)):
        return "the page links to a feed, but it isn't valid RSS"
    return reason


def find_source(url):
    """Work out how to follow a URL. Returns (source_url, kind, scope, title, site, items, note)."""
    try:
        raw, fetch_error = fetch(url), None
    except Exception as e:
        raw, fetch_error = None, e
        if "Could not resolve host" in str(e):
            raise

    # 0. YouTube feeds (also reached when the feed service is down)
    if re.search(r"youtube\.com/feeds/videos\.xml\?channel_id=", url):
        title, site, items, note = load_source(url)
        return url, "rss", "", title, site, items, ("— " + note) if note else ""

    # 1. It is a feed, or the page advertises one
    if raw is not None:
        try:
            title, site, items = parse_feed(raw, url)
            return url, "rss", "", title, site, items, ""
        except (ET.ParseError, NotAFeed):
            found = discover_feed(raw.decode("utf-8", errors="replace"), url)
            if found:
                title, site, items, note = load_source(found)
                return found, "rss", "", title, site, items, ("— " + note) if note else ""

    # 2. An unadvertised feed at a common address
    probed = probe_feeds(url)
    if probed:
        feed_url, title, site, items = probed
        return feed_url, "rss", "", title, site, items, ""

    # 3. The site's sitemap, narrowed to the page's section when its links share that path
    sitemap = find_sitemap(url)
    if sitemap:
        sm_url, title, site, items = sitemap
        section = urlparse(url).path
        scoped = [i for i in items if section.strip("/") and urlparse(i["link"]).path.startswith(section)]
        if scoped:
            return sm_url, "sitemap", section, title, site, scoped, "from its sitemap"
        note = "from its sitemap — covers the whole site" if section.strip("/") else "from its sitemap"
        return sm_url, "sitemap", "", title, site, items, note

    # 4. Read the headlines straight off the page
    if raw is not None:
        try:
            title, site, items = parse_page(raw, url)
            return url, "page", "", title, site, items, "by reading the page"
        except NotAFeed:
            pass

    if fetch_error is not None:
        raise fetch_error
    raise NotAFeed("no feed found")


def cmd_add(url):
    """Subscribe to a URL. Returns True if a feed was added."""
    url = url.strip()
    if not url:
        print("No URL given")
        return False
    if not re.match(r"^https?://", url, re.I) and not YT_CHANNEL_ID.match(url) and not YT_HANDLE.match(url):
        url = "https://" + url
    url = youtube_feed(url) or url

    feeds = load(FEEDS_FILE, {})
    try:
        source, kind, scope, title, site, items, note = find_source(url)
    except NotAFeed:
        print("No RSS feed, sitemap or article list found at " + url)
        return False
    except Exception as e:
        print("Couldn't add %s: %s" % (url, friendly_error(e)))
        return False

    if source in feeds and feeds[source].get("scope", "") == scope:
        print("Already subscribed to " + (feeds[source].get("title") or source))
        return False

    feeds[source] = {"title": title or source, "added": time.time()}
    if kind != "rss":
        feeds[source]["kind"] = kind
        feeds[source]["page"] = url
    if scope:
        feeds[source]["scope"] = scope
    save(FEEDS_FILE, feeds)

    ensure_icon(source, site or url)
    archive = load_archive()
    feed = archive["feeds"].setdefault(source, {"title": title, "site": site})
    merge(feed, items)
    save(ARCHIVE_FILE, archive)
    print("Added %s%s (%d items)" % (title or source, " " + note if note else "", len(items)))
    return True


def cmd_unsubscribe(url):
    feeds = load(FEEDS_FILE, {})
    if url not in feeds:
        print("Not subscribed to " + url)
        return
    title = feeds.pop(url).get("title") or url
    save(FEEDS_FILE, feeds)

    archive = load_archive()
    archive["feeds"].pop(url, None)
    save(ARCHIVE_FILE, archive)

    read = load(READ_FILE, {})
    read.pop(url, None)
    save(READ_FILE, read)

    cats = load(CATEGORIES_FILE, {})
    for cat in cats.values():
        if url in cat["feeds"]:
            cat["feeds"].remove(url)
    save(CATEGORIES_FILE, cats)

    try:
        os.remove(icon_path(url))
    except OSError:
        pass
    print("Unsubscribed from " + title)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    arg = sys.argv[2] if len(sys.argv) > 2 else ""

    if cmd == "list":
        cmd_list(arg)
    elif cmd == "add":
        cmd_add(arg)
    elif cmd == "nav":
        cmd_nav(arg)
    elif cmd == "open":
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cmd_act(arg)
        variables = dict(NEXT, next="nav") if NEXT else {"next": "notify"}
        print(json.dumps({"alfredworkflow": {"arg": buffer.getvalue().strip(), "variables": variables}}))


if __name__ == "__main__":
    main()
