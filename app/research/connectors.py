"""Research connectors.

Every connector uses an official API, or fetches a page that robots.txt
allows, and identifies itself honestly. There is no scraping of search-engine
or YouTube results pages. Each returns a list of raw source dicts and raises
on failure; the orchestrator decides what a failure means.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
from functools import lru_cache
from urllib.parse import quote, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from .. import llm
from ..config import get_settings, user_agent

TIMEOUT = httpx.Timeout(15.0, connect=8.0)
MAX_PAGE_CHARS = 60_000


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": user_agent()}, timeout=TIMEOUT, follow_redirects=True)


# ---- page fetching -----------------------------------------------------
def is_public_url(url: str) -> bool:
    """Reject non-web schemes and hosts on private networks."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    try:
        for info in socket.getaddrinfo(parsed.hostname, None):
            if not ipaddress.ip_address(info[4][0]).is_global:
                return False
    except (socket.gaierror, ValueError):
        return False
    return True


@lru_cache(maxsize=128)
def _robots(origin: str) -> RobotFileParser | None:
    parser = RobotFileParser()
    try:
        with client() as http:
            response = http.get(f"{origin}/robots.txt")
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None  # no robots.txt means no stated restriction
    parser.parse(response.text.splitlines())
    return parser


def allowed_by_robots(url: str) -> bool:
    parsed = urlparse(url)
    parser = _robots(f"{parsed.scheme}://{parsed.netloc}")
    return True if parser is None else parser.can_fetch("LessonForge", url)


def extract_text(html: str) -> tuple[str, str]:
    """Return (title, main text) from an HTML page."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript", "svg"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    lines = [re.sub(r"\s+", " ", line).strip() for line in root.get_text("\n").splitlines()]
    text = "\n".join(line for line in lines if len(line) > 1)
    return title, text[:MAX_PAGE_CHARS]


def fetch_page(url: str) -> dict:
    if not is_public_url(url):
        raise ValueError("not a public web address")
    if not allowed_by_robots(url):
        raise PermissionError("robots.txt disallows this page")
    with client() as http:
        response = http.get(url)
    response.raise_for_status()
    if "html" not in response.headers.get("content-type", ""):
        raise ValueError("not an HTML page")
    title, text = extract_text(response.text)
    return {"title": title, "text": text, "url": str(response.url)}


# ---- Wikipedia (keyless, MediaWiki API) ---------------------------------
WIKI_API = "https://en.wikipedia.org/w/api.php"
_wiki_lock = threading.Lock()  # Wikimedia asks API clients to send requests serially


def wikipedia(query: str, limit: int = 3) -> list[dict]:
    with _wiki_lock, client() as http:
        found = http.get(WIKI_API, params={
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": limit, "format": "json",
        })
        if found.status_code == 403:
            raise PermissionError("Wikipedia refused this client under its robot policy. Set "
                                  "LESSONFORGE_CONTACT in .env to your project URL or email.")
        found.raise_for_status()
        results = []
        for hit in found.json()["query"]["search"]:
            page = http.get(WIKI_API, params={
                "action": "query", "prop": "extracts", "explaintext": 1,
                "titles": hit["title"], "format": "json",
            })
            page.raise_for_status()
            extract = next(iter(page.json()["query"]["pages"].values())).get("extract", "")
            results.append({
                "kind": "web", "via": "wikipedia", "title": f"{hit['title']} (Wikipedia)",
                "url": "https://en.wikipedia.org/wiki/" + quote(hit["title"].replace(" ", "_")),
                "text": extract[:MAX_PAGE_CHARS], "evidence": True,
            })
    return results


# ---- MDN (keyless, public search API) -----------------------------------
def mdn(query: str, limit: int = 3) -> list[dict]:
    with client() as http:
        found = http.get("https://developer.mozilla.org/api/v1/search",
                         params={"q": query, "locale": "en-US"})
    found.raise_for_status()
    results = []
    for doc in found.json().get("documents", [])[:limit]:
        url = "https://developer.mozilla.org" + doc["mdn_url"]
        try:
            page = fetch_page(url)
            text = page["text"]
        except (httpx.HTTPError, ValueError, PermissionError):
            text = doc.get("summary", "")
        results.append({"kind": "web", "via": "mdn", "title": f"{doc['title']} (MDN)",
                        "url": url, "text": text, "evidence": bool(text)})
    return results


# ---- Open Library (keyless) ---------------------------------------------
def books(query: str, limit: int = 3) -> list[dict]:
    """Book metadata. No text, so these are reading suggestions, not evidence."""
    with client() as http:
        found = http.get("https://openlibrary.org/search.json", params={
            "q": query, "limit": limit,
            "fields": "title,author_name,first_publish_year,key,publisher",
        })
    found.raise_for_status()
    return [{
        "kind": "book", "via": "openlibrary", "title": doc.get("title", "Untitled"),
        "url": "https://openlibrary.org" + doc.get("key", ""),
        "author": ", ".join(doc.get("author_name", [])[:3]),
        "year": doc.get("first_publish_year"), "text": "", "evidence": False,
    } for doc in found.json().get("docs", [])]


def _is_prose(snippet: str) -> bool:
    """Drop index pages, listings and code, which match searches but teach nothing."""
    if len(snippet) < 60 or re.search(r"[_$;{}]|\w+\(\)|, \d{2,4}\b", snippet):
        return False
    return sum(ch.isalpha() or ch.isspace() for ch in snippet) / len(snippet) > 0.85


def book_passages(query: str, limit: int = 4) -> list[dict]:
    """Real passages from digitised books, via Open Library full-text search."""
    with client() as http:
        found = http.get("https://openlibrary.org/search/inside.json",
                         params={"q": query, "limit": limit}, timeout=30)
    found.raise_for_status()
    results = []
    for hit in found.json().get("hits", {}).get("hits", []):
        fields = hit.get("fields", {})
        snippets = [re.sub(r"\s+", " ", s.replace("{{{", "").replace("}}}", "")).strip()
                    for s in hit.get("highlight", {}).get("text", [])]
        text = "\n\n".join(s for s in snippets if _is_prose(s))
        identifier = hit.get("_id", "").split("|")[0]
        if not text or not identifier:
            continue
        results.append({
            "kind": "book", "via": "openlibrary_inside",
            "title": (fields.get("meta_title") or ["Untitled"])[0],
            "url": f"https://archive.org/details/{identifier}",
            "author": ", ".join(fields.get("meta_creator", [])[:3]),
            "year": (fields.get("meta_year") or [None])[0],
            "text": text, "evidence": True,
        })
    return results


# ---- web search (keyed) --------------------------------------------------
def web_search_available() -> bool:
    settings = get_settings()
    return bool(settings.tavily_api_key) or (
        settings.llm_provider == "anthropic" and llm.llm_ready()[0])


def web_search(query: str, domains: list[str] | None = None, limit: int = 5) -> list[dict]:
    settings = get_settings()
    if settings.tavily_api_key:
        return _tavily(query, domains, limit)
    return llm.get_provider().web_search(query, domains)[:limit]


def _tavily(query: str, domains: list[str] | None, limit: int) -> list[dict]:
    body: dict = {"query": query, "max_results": limit, "search_depth": "advanced",
                  "include_raw_content": True}
    if domains:
        body["include_domains"] = domains
    with client() as http:
        response = http.post("https://api.tavily.com/search", json=body,
                             headers={"Authorization": f"Bearer {get_settings().tavily_api_key}"},
                             timeout=30)
    response.raise_for_status()
    return [{"url": r["url"], "title": r.get("title", ""),
             "text": (r.get("raw_content") or r.get("content") or "")[:MAX_PAGE_CHARS]}
            for r in response.json().get("results", [])]


def web(query: str, limit: int = 5) -> list[dict]:
    results = []
    for hit in web_search(query, limit=limit):
        text, title, url = hit.get("text", ""), hit.get("title", ""), hit["url"]
        if not text:
            try:
                page = fetch_page(url)
            except (httpx.HTTPError, ValueError, PermissionError):
                continue  # a page we may not or cannot read is not evidence
            text, title, url = page["text"], title or page["title"], page["url"]
        results.append({"kind": "web", "via": "web_search", "title": title or url,
                        "url": url, "text": text, "evidence": True})
    return results


# ---- YouTube ------------------------------------------------------------
def _duration_seconds(iso: str) -> int:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso or "")
    if not match:
        return 0
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def youtube_id(url: str) -> str | None:
    match = re.search(r"(?:v=|youtu\.be/|/embed/|/shorts/)([\w-]{11})", url)
    return match.group(1) if match else None


def oembed(url: str) -> dict:
    """Confirm a video exists using YouTube's official oEmbed endpoint."""
    video_id = youtube_id(url)
    if not video_id:
        raise ValueError("That is not a YouTube video link")
    canonical = f"https://www.youtube.com/watch?v={video_id}"
    with client() as http:
        response = http.get("https://www.youtube.com/oembed",
                            params={"url": canonical, "format": "json"})
    if response.status_code in (400, 401, 403, 404):
        raise ValueError("YouTube says this video is unavailable or cannot be embedded")
    response.raise_for_status()
    data = response.json()
    return {"kind": "video", "via": "oembed", "title": data.get("title", canonical), "url": canonical,
            "channel": data.get("author_name", ""), "text": "", "evidence": False}


def youtube_api(query: str, limit: int = 5) -> list[dict]:
    key = get_settings().youtube_api_key
    base = "https://www.googleapis.com/youtube/v3"
    with client() as http:
        found = http.get(f"{base}/search", params={
            "part": "snippet", "type": "video", "q": query, "maxResults": limit,
            "relevanceLanguage": "en", "safeSearch": "strict", "videoEmbeddable": "true", "key": key,
        })
        found.raise_for_status()
        ids = [item["id"]["videoId"] for item in found.json().get("items", [])]
        if not ids:
            return []
        details = http.get(f"{base}/videos", params={
            "part": "snippet,contentDetails,statistics", "id": ",".join(ids), "key": key,
        })
        details.raise_for_status()
    results = []
    for item in details.json().get("items", []):
        snippet = item["snippet"]
        results.append({
            "kind": "video", "via": "youtube_api", "title": snippet["title"],
            "url": f"https://www.youtube.com/watch?v={item['id']}",
            "channel": snippet.get("channelTitle", ""),
            "description": snippet.get("description", "")[:500],
            "year": int(snippet["publishedAt"][:4]) if snippet.get("publishedAt") else None,
            "duration_s": _duration_seconds(item["contentDetails"].get("duration", "")),
            "views": int(item.get("statistics", {}).get("viewCount", 0)),
            "text": "", "evidence": False,
        })
    return results


def youtube_via_search(query: str, limit: int = 5) -> list[dict]:
    results = []
    for hit in web_search(query, domains=["youtube.com"], limit=limit):
        if not youtube_id(hit["url"]):
            continue
        try:
            results.append(oembed(hit["url"]))
        except (httpx.HTTPError, ValueError):
            continue
    return results
