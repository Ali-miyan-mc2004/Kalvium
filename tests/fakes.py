"""Test doubles. FakeLLM returns canned objects: it proves the plumbing works and
says nothing about lesson quality, which only a real model run can show.
"""
from __future__ import annotations

from app import llm
from app.schemas import (
    Claim, ClaimList, ClaimVerdict, FactualReview, InstructionalReview, LessonDraft,
    ObjectiveCoverage, ProposedIssue, QueryPlan, RubricScore, Section, VerdictList, VideoPick,
)

SOURCES = [
    {"id": "S1", "kind": "web", "via": "mdn", "title": "HTTP status codes (MDN)",
     "url": "https://developer.mozilla.org/status", "domain": "developer.mozilla.org", "tier": 1,
     "tier_reason": "Official documentation", "relevance": 0.8, "evidence": True,
     "text": "Successful responses use the 200 to 299 range. Client error responses use 400 to 499. "
             "Server error responses use 500 to 599."},
    {"id": "S2", "kind": "web", "via": "mdn", "title": "HTTP request methods (MDN)",
     "url": "https://developer.mozilla.org/methods", "domain": "developer.mozilla.org", "tier": 1,
     "tier_reason": "Official documentation", "relevance": 0.7, "evidence": True,
     "text": "The GET method requests a representation of the specified resource. "
             "The POST method submits an entity to the specified resource."},
    {"id": "S3", "kind": "book", "via": "openlibrary", "title": "HTTP: The Definitive Guide",
     "url": "https://openlibrary.org/works/OL1", "domain": "openlibrary.org", "tier": 2,
     "tier_reason": "Published book", "relevance": 0.5, "evidence": False, "text": "",
     "author": "David Gourley", "year": 2002},
    {"id": "S4", "kind": "video", "via": "youtube_api", "title": "HTTP in 5 minutes",
     "url": "https://www.youtube.com/watch?v=abcdefghijk", "domain": "youtube.com", "tier": 3,
     "tier_reason": "Video", "relevance": 0.6, "evidence": False, "text": "",
     "channel": "Test Channel", "duration_s": 300},
]

RESEARCH = {
    "plan": QueryPlan(subtopics=["GET and POST", "status categories"], wikipedia_queries=["HTTP"],
                      web_queries=["http"], book_queries=["http"], video_queries=["http"]).model_dump(),
    "sources": SOURCES, "diagnostics": [{"connector": "mdn", "query": "http", "found": 2, "error": None}],
    "warnings": [], "rejected_as_irrelevant": 0,
}


def draft(words_per_section: int | None = None) -> LessonDraft:
    """A lesson whose text backs its planned minutes, unless a length is forced."""
    plan = [("hook", 3), ("concept", 6), ("concept", 6), ("example", 6), ("example", 6),
            ("activity", 9), ("check", 6), ("summary", 2)]
    sections = []
    for number, (kind, minutes) in enumerate(plan, start=1):
        words = words_per_section if words_per_section is not None else minutes * 95
        filler = " ".join(["word"] * words)
        body = f"GET requests a resource [S2]. Client errors use 400 to 499 [S1]. {filler}"
        if kind == "check":
            body += " Q1? Q2? Q3? Q4? Q5?"
        sections.append(Section(id=f"s{number}", kind=kind, title=f"{kind} {number}", minutes=minutes, body=body))
    return LessonDraft(subobjectives=["Explain GET", "Distinguish status categories"], prerequisites=["None"],
                       sections=sections,
                       videos=[VideoPick(source_id="S4", why="Short recap", when_to_watch="After section 2"),
                               VideoPick(source_id="S99", why="Made up", when_to_watch="Never")])


class FakeLLM:
    def __init__(self) -> None:
        self.fail_on: set[type] = set()
        self.calls: list[str] = []
        self.drafts: list[LessonDraft] = []
        self.fabricate_quote = False

    def complete(self, schema, system, user, *, role, effort):
        self.calls.append(schema.__name__)
        if schema in self.fail_on:
            raise llm.LLMError(f"Simulated failure in {schema.__name__}")
        if schema is QueryPlan:
            return QueryPlan(**RESEARCH["plan"])
        if schema is LessonDraft:
            return self.drafts.pop(0) if self.drafts else draft()
        if schema is ClaimList:
            return ClaimList(claims=[
                Claim(section_id="s2", quote="GET requests a resource [S2].", claim="GET requests a resource."),
                Claim(section_id="s2", quote="Client errors use 400 to 499 [S1].", claim="Client errors use 400 to 499."),
            ])
        if schema is VerdictList:
            quote = "this text is not in any passage" if self.fabricate_quote else "Client error responses use 400 to 499"
            return VerdictList(verdicts=[
                ClaimVerdict(claim_index=0, verdict="unsupported", evidence_source="", evidence_quote="", explanation="Not found."),
                ClaimVerdict(claim_index=1, verdict="supported", evidence_source="S1", evidence_quote=quote, explanation="Stated directly."),
            ])
        if schema is FactualReview:
            return FactualReview(summary="One claim needs evidence.", issues=[ProposedIssue(
                severity="medium", category="unsupported_claim", section_id="s2", description="Unverified wording.",
                suggestion="Use the source's wording.", find="GET requests a resource [S2].",
                replace="GET requests a representation of a resource [S2].")])
        if schema is InstructionalReview:
            return InstructionalReview(
                summary="Solid structure.",
                objective_coverage=[ObjectiveCoverage(subobjective="Explain GET", coverage="strong", where="s2",
                                                      assessed_by_check=True, note="")],
                rubric=[RubricScore(criterion="clarity", score=4, rationale="Clear.", weakness="Dense."),
                        RubricScore(criterion="structure", score=3, rationale="Fine.", weakness="Abrupt.")],
                issues=[ProposedIssue(severity="low", category="missing_example", section_id="s4",
                                      description="Add a 5xx case.", suggestion="Add one.", find="",
                                      replace="A 503 means the server is unavailable.")])
        raise AssertionError(f"Unexpected schema {schema}")

    def web_search(self, query, domains=None):
        return []
