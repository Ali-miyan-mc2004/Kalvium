"""Research orchestration with stubbed connectors."""
from __future__ import annotations

import pytest

from app import research
from app.research import ResearchError, connectors, run_research
from app.schemas import QueryPlan

TITLE = "HTTP Requests and Responses"
OBJECTIVE = "Explain GET and POST methods and distinguish status categories."
PLAN = QueryPlan(subtopics=["GET"], wikipedia_queries=["HTTP"], web_queries=["HTTP methods"],
                 book_queries=["HTTP guide"], video_queries=["HTTP explained"])
PAGE_TEXT = "HTTP requests and responses. The GET and POST methods. Status categories distinguish results."


def page(url: str, text: str = PAGE_TEXT) -> dict:
    return {"kind": "web", "via": "stub", "title": "Page", "url": url, "text": text, "evidence": True}


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(connectors, "wikipedia", lambda q: [page("https://en.wikipedia.org/wiki/HTTP")])
    monkeypatch.setattr(connectors, "mdn", lambda q: [page("https://developer.mozilla.org/a")])
    monkeypatch.setattr(connectors, "books", lambda q: [
        {"kind": "book", "via": "ol", "title": "HTTP: The Definitive Guide", "url": "https://openlibrary.org/1",
         "text": "", "evidence": False},
        {"kind": "book", "via": "ol", "title": "Gardening for Beginners", "url": "https://openlibrary.org/2",
         "text": "", "evidence": False}])
    monkeypatch.setattr(connectors, "book_passages", lambda q: [])
    monkeypatch.setattr(connectors, "web_search_available", lambda: False)
    monkeypatch.setattr(research, "get_settings", lambda: type("S", (), {"youtube_api_key": ""})())


def test_books_match_their_query_and_irrelevant_ones_are_dropped(stubbed):
    result = run_research(TITLE, OBJECTIVE, PLAN)
    titles = [s["title"] for s in result["sources"]]
    assert "HTTP: The Definitive Guide" in titles
    assert "Gardening for Beginners" not in titles
    assert result["rejected_as_irrelevant"] == 1


def test_sources_are_numbered_and_ranked_most_reliable_first(stubbed):
    sources = run_research(TITLE, OBJECTIVE, PLAN)["sources"]
    assert [s["id"] for s in sources] == [f"S{n}" for n in range(1, len(sources) + 1)]
    assert sources[0]["domain"] == "developer.mozilla.org"


def test_irrelevant_web_pages_are_dropped(stubbed, monkeypatch):
    monkeypatch.setattr(connectors, "mdn", lambda q: [
        page("https://developer.mozilla.org/a"), page("https://developer.mozilla.org/b", "Recipes for soup.")])
    urls = [s["url"] for s in run_research(TITLE, OBJECTIVE, PLAN)["sources"]]
    assert "https://developer.mozilla.org/b" not in urls


def test_one_failing_connector_is_recorded_and_the_rest_continue(stubbed, monkeypatch):
    def broken(query):
        raise PermissionError("Wikipedia refused this client")

    monkeypatch.setattr(connectors, "wikipedia", broken)
    monkeypatch.setattr(connectors, "book_passages", lambda q: [{
        "kind": "book", "via": "ol", "title": "HTTP Guide", "url": "https://archive.org/details/x",
        "text": "HTTP guide passage about GET and POST.", "evidence": True}])
    result = run_research(TITLE, OBJECTIVE, PLAN)
    assert any(d["connector"] == "wikipedia" and "refused" in d["error"] for d in result["diagnostics"])
    assert any("failed" in w for w in result["warnings"])
    assert len(result["sources"]) >= 2


def test_too_few_sources_fails_with_the_underlying_reason(stubbed, monkeypatch):
    def broken(query):
        raise ConnectionError("network down")

    monkeypatch.setattr(connectors, "wikipedia", broken)
    monkeypatch.setattr(connectors, "mdn", broken)
    with pytest.raises(ResearchError, match="network down"):
        run_research(TITLE, OBJECTIVE, PLAN)


def test_missing_keys_produce_warnings_not_errors(stubbed):
    warnings = " ".join(run_research(TITLE, OBJECTIVE, PLAN)["warnings"])
    assert "web search is off" in warnings and "Video search is off" in warnings
