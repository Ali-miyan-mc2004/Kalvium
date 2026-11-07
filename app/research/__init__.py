"""Research orchestration: fan out to connectors, then filter, rank and number."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from ..config import get_settings
from ..schemas import QueryPlan
from . import connectors
from .corpus import domain_of, rate_source, relevance

MAX_EVIDENCE = 11
MAX_BOOK_EVIDENCE = 3
MAX_BOOKS = 6
MAX_VIDEOS = 6
MIN_EVIDENCE = 2
MIN_RELEVANCE = 0.34        # share of title+objective terms a web page must contain
MIN_QUERY_RELEVANCE = 0.5   # share of its search query's terms a book or video must contain


class ResearchError(Exception):
    """Not enough usable sources to write a grounded lesson."""


def fallback_plan(title: str, objective: str) -> QueryPlan:
    """Queries that need no LLM, used when the planning call fails."""
    return QueryPlan(
        subtopics=[title, objective],
        wikipedia_queries=[title],
        web_queries=[title, f"{title} explained"],
        book_queries=[title],
        video_queries=[f"{title} explained"],
    )


def _jobs(plan: QueryPlan, title: str) -> tuple[list[tuple[str, str, Callable]], list[str]]:
    jobs: list[tuple[str, str, Callable]] = []
    warnings: list[str] = []
    for query in plan.wikipedia_queries[:3]:
        jobs.append(("wikipedia", query, connectors.wikipedia))
    for query in plan.web_queries[:4]:
        jobs.append(("mdn", query, connectors.mdn))
    for query in dict.fromkeys(plan.book_queries[:3] + [title]):
        jobs.append(("books", query, connectors.books))
        jobs.append(("book_passages", query, connectors.book_passages))

    search_ready = connectors.web_search_available()
    if search_ready:
        for query in plan.web_queries[:4]:
            jobs.append(("web_search", query, connectors.web))
    else:
        warnings.append("General web search is off (no TAVILY_API_KEY or Anthropic key), so "
                        "website research is limited to Wikipedia and MDN.")

    if get_settings().youtube_api_key:
        for query in plan.video_queries[:3]:
            jobs.append(("youtube", query, connectors.youtube_api))
    elif search_ready:
        for query in plan.video_queries[:3]:
            jobs.append(("youtube_search", query, connectors.youtube_via_search))
    else:
        warnings.append("Video search is off (no YOUTUBE_API_KEY or web search). Add videos by "
                        "link on the Sources tab.")
    return jobs, warnings


def _dedupe(raw: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for item in raw:
        key = item["url"].split("#")[0].rstrip("/").lower()
        current = seen.get(key)
        # Prefer the record that carries text we can check claims against.
        if current is None or len(item.get("text", "")) > len(current.get("text", "")):
            seen[key] = item
    return list(seen.values())


def enrich(item: dict) -> dict:
    tier, reason = rate_source(item["url"], item["kind"])
    return {**item, "domain": domain_of(item["url"]), "tier": tier, "tier_reason": reason}


def run_research(title: str, objective: str, plan: QueryPlan) -> dict:
    jobs, warnings = _jobs(plan, title)
    diagnostics: list[dict] = []
    raw: list[dict] = []

    def run(job: tuple[str, str, Callable]) -> tuple[str, str, list[dict], str | None]:
        name, query, fn = job
        try:
            return name, query, fn(query), None
        except Exception as exc:  # one failing connector must not stop the others
            return name, query, [], f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        for name, query, items, error in pool.map(run, jobs):
            raw.extend({**item, "query": query} for item in items)
            diagnostics.append({"connector": name, "query": query, "found": len(items), "error": error})

    topic = f"{title} {objective}"
    scored = []
    rejected = 0
    for item in _dedupe(raw):
        item = enrich(item)
        haystack = " ".join([item["title"], item.get("text", "")[:20000], item.get("description", "")])
        item["relevance"] = round(relevance(haystack, topic), 2)
        # A web page has room to match the whole objective. A book title or video
        # name does not, so those must match the query that found them.
        if item["kind"] == "web":
            keep = item["relevance"] >= MIN_RELEVANCE
        else:
            keep = relevance(haystack, item["query"]) >= MIN_QUERY_RELEVANCE
        if keep:
            scored.append(item)
        else:
            rejected += 1

    def rank(item: dict) -> tuple:
        return (item["tier"], -item["relevance"])

    pages = sorted([s for s in scored if s["kind"] == "web" and s.get("evidence")], key=rank)
    passages = sorted([s for s in scored if s["kind"] == "book" and s.get("evidence")], key=rank)
    evidence = pages[:MAX_EVIDENCE] + passages[:MAX_BOOK_EVIDENCE]
    reading = sorted([s for s in scored if s["kind"] == "book" and not s.get("evidence")], key=rank)[:MAX_BOOKS]
    videos = sorted([s for s in scored if s["kind"] == "video"],
                    key=lambda v: (-v["relevance"], -v.get("views", 0)))[:MAX_VIDEOS]

    sources = evidence + reading + videos
    for number, source in enumerate(sources, start=1):
        source["id"] = f"S{number}"

    failed = [d for d in diagnostics if d["error"]]
    if failed:
        warnings.append(f"{len(failed)} of {len(diagnostics)} research calls failed; see the Sources tab.")
    if not videos:
        warnings.append("No YouTube videos were found for this lesson.")

    if len(evidence) < MIN_EVIDENCE:
        problems = "; ".join(sorted({d["error"] for d in failed}))[:300]
        raise ResearchError(
            f"Only {len(evidence)} usable source(s) found, and at least {MIN_EVIDENCE} are needed to "
            f"ground a lesson. {('Errors: ' + problems) if problems else 'Try a more specific title.'}")

    return {"plan": plan.model_dump(), "sources": sources, "diagnostics": diagnostics,
            "warnings": warnings, "rejected_as_irrelevant": rejected}
