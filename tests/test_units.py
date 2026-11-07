"""Pure logic: no database, no network."""
from __future__ import annotations

from app import llm
from app.qc import checks, factual, instructional
from app.research import _dedupe
from app.research.connectors import _duration_seconds, _is_prose, extract_text, is_public_url, youtube_id
from app.research.corpus import Index, Passage, chunk, rate_source, relevance
from app.schemas import LessonDraft
from tests.fakes import SOURCES, draft


def content(**overrides):
    data = draft().model_dump()
    data.update(overrides)
    return data


def test_strict_schema_closes_every_object_and_keeps_fields_named_title():
    schema = llm.strict_schema(LessonDraft)
    section = schema["properties"]["sections"]["items"]
    assert schema["additionalProperties"] is False
    assert section["additionalProperties"] is False
    assert "title" in section["properties"], "a field called title is not the schema keyword"
    assert set(section["required"]) == {"id", "kind", "title", "minutes", "body"}
    assert "$defs" not in schema and "$ref" not in str(schema)


def test_source_tiers():
    assert rate_source("https://www.rfc-editor.org/rfc/rfc9110", "web")[0] == 1
    assert rate_source("https://cs.university.edu/notes", "web")[0] == 1
    assert rate_source("https://en.wikipedia.org/wiki/HTTP", "web")[0] == 2
    assert rate_source("https://someone.medium.com/post", "web")[0] == 4
    assert rate_source("https://example.com/blog", "web")[0] == 3
    # A lookalike domain must not inherit a trusted domain's tier.
    assert rate_source("https://fakew3.org/spec", "web")[0] == 3


def test_relevance_folds_plurals():
    assert relevance("HTTP request methods", "HTTP Requests") == 1.0
    assert relevance("cooking pasta", "HTTP Requests") == 0.0


def test_bm25_ranks_the_matching_passage_first():
    index = Index([Passage("S1", "cats sleep all day"), Passage("S2", "http status codes in the 4xx range"),
                   Passage("S3", "dogs enjoy long walks")])
    assert index.search("4xx status codes", k=1)[0].source_id == "S2"
    assert index.search("4xx status codes", source_id="S1") == []


def test_chunk_splits_long_text_without_losing_it():
    text = "\n\n".join(f"paragraph {i} " + "x" * 500 for i in range(10))
    passages = chunk("S1", text)
    assert len(passages) > 1
    assert all(len(p.text) <= 1300 for p in passages)
    assert "paragraph 9" in passages[-1].text


def test_dedupe_keeps_the_record_with_text():
    merged = _dedupe([{"url": "https://a.org/x/", "text": ""}, {"url": "https://a.org/x", "text": "body"}])
    assert len(merged) == 1 and merged[0]["text"] == "body"


def test_prose_filter_rejects_index_pages():
    assert not _is_prose("HTTP_ACCEPTED - HTTP status code (202) field, 269 HTTP_BAD_GATEWAY - status")
    assert _is_prose("An HTTP status code is a number sent in the header information of a response to a client")


def test_extract_text_drops_page_furniture():
    title, text = extract_text("<html><title>T</title><nav>menu</nav><script>x()</script><main><p>Real content</p></main></html>")
    assert title == "T" and "Real content" in text and "menu" not in text and "x()" not in text


def test_private_addresses_are_not_fetched():
    assert not is_public_url("http://127.0.0.1:8000/admin")
    assert not is_public_url("file:///etc/passwd")


def test_youtube_helpers():
    assert youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3") == "dQw4w9WgXcQ"
    assert youtube_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube_id("https://example.com/video") is None
    assert _duration_seconds("PT1H2M3S") == 3723


def test_citations_handle_groups():
    assert checks.cited_ids("Fact [S1]. Another [S2, S3].") == ["S1", "S2", "S3"]


def test_time_budget_counts_reading_time_when_minutes_are_understated():
    data = {"sections": [{"id": "s1", "kind": "concept", "title": "Long", "minutes": 1, "body": "word " * 1800}]}
    budget = checks.time_budget(data)
    assert budget["total_minutes"] == 10
    assert budget["too_short"] is True


def test_planned_minutes_must_be_backed_by_text():
    data = {"sections": [{"id": "s1", "kind": "concept", "title": "Thin", "minutes": 30, "body": "word " * 180}]}
    budget = checks.time_budget(data)
    assert budget["planned_minutes"] == 30
    assert budget["total_minutes"] == 2
    assert budget["too_short"] is True


def test_activities_keep_their_planned_time():
    data = {"sections": [{"id": "s1", "kind": "activity", "title": "Do it", "minutes": 9, "body": "Short task."}]}
    assert checks.time_budget(data)["total_minutes"] == 9


def test_a_score_of_five_needs_no_stated_weakness():
    assert instructional.calibrate(5, "Too many status codes.") == 4
    assert instructional.calibrate(5, "None.") == 5
    assert instructional.calibrate(3, "Abrupt ending.") == 3
    assert instructional.calibrate(9, "") == 5 and instructional.calibrate(0, "") == 1


def test_time_budget_flags_a_lesson_over_45_minutes():
    data = {"sections": [{"id": "s1", "kind": "activity", "title": "Huge", "minutes": 50, "body": "x"}]}
    budget = checks.time_budget(data)
    assert not budget["within_limit"]
    found = checks.instructional_issues(data, budget, [])
    assert found[0]["category"] == "time_budget" and found[0]["severity"] == "high"


def test_invented_citation_is_a_high_severity_issue():
    data = content()
    data["sections"][1]["body"] = "A fact [S42]."
    found = checks.citation_issues(data, SOURCES)
    assert any(i["category"] == "invalid_citation" and i["severity"] == "high" for i in found)


def test_citing_a_metadata_only_source_is_flagged():
    data = content()
    data["sections"][1]["body"] = "A claim about the book [S3]."
    assert any(i["category"] == "uncheckable_citation" for i in checks.citation_issues(data, SOURCES))


def test_structure_checks_catch_a_missing_activity():
    data = content()
    data["sections"] = [s for s in data["sections"] if s["kind"] != "activity"]
    failed = [c["check"] for c in checks.structure_checks(data) if not c["passed"]]
    assert failed == ["At least one practical activity"]


def test_fabricated_evidence_quote_voids_the_verdict():
    passages = [Passage("S1", "Client error responses use 400 to 499.")]
    row = {"verdict": "supported", "evidence_source": "S1", "evidence_quote": "made up words", "explanation": "x"}
    assert factual._confirm_quote(row, passages)["verdict"] == "unverified"


def test_quote_from_the_wrong_source_voids_the_verdict():
    passages = [Passage("S1", "Client error responses use 400 to 499.")]
    row = {"verdict": "supported", "evidence_source": "S2",
           "evidence_quote": "Client error responses", "explanation": "x"}
    assert factual._confirm_quote(row, passages)["verdict"] == "unverified"


def test_genuine_quote_survives_formatting_differences():
    passages = [Passage("S1", "Client error\nresponses  use 400 to 499.")]
    row = {"verdict": "supported", "evidence_source": "S1",
           "evidence_quote": "client error responses use 400", "explanation": "x"}
    assert factual._confirm_quote(row, passages)["verdict"] == "supported"
