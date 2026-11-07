"""Structured shapes exchanged with the LLM and the HTTP API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SectionKind = Literal["hook", "concept", "example", "activity", "check", "summary"]
Severity = Literal["high", "medium", "low"]
Verdict = Literal["supported", "partially_supported", "unsupported", "contradicted"]


# ---- research ----------------------------------------------------------
class QueryPlan(BaseModel):
    subtopics: list[str]
    wikipedia_queries: list[str]
    web_queries: list[str]
    book_queries: list[str]
    video_queries: list[str]


# ---- lesson ------------------------------------------------------------
class Section(BaseModel):
    id: str
    kind: SectionKind
    title: str
    minutes: int
    body: str


class VideoPick(BaseModel):
    source_id: str
    why: str
    when_to_watch: str


class LessonDraft(BaseModel):
    subobjectives: list[str]
    prerequisites: list[str]
    sections: list[Section]
    videos: list[VideoPick]


# ---- QC layer 1: research and factual accuracy --------------------------
class Claim(BaseModel):
    section_id: str
    quote: str
    claim: str


class ClaimList(BaseModel):
    claims: list[Claim]


class ClaimVerdict(BaseModel):
    claim_index: int
    verdict: Verdict
    evidence_source: str
    evidence_quote: str
    explanation: str


class VerdictList(BaseModel):
    verdicts: list[ClaimVerdict]


class ProposedIssue(BaseModel):
    severity: Severity
    category: str
    section_id: str
    description: str
    suggestion: str
    find: str
    replace: str


class FactualReview(BaseModel):
    summary: str
    issues: list[ProposedIssue]


# ---- QC layer 2: instructional quality ----------------------------------
class ObjectiveCoverage(BaseModel):
    subobjective: str
    coverage: Literal["strong", "weak", "missing"]
    where: str
    assessed_by_check: bool
    note: str


class RubricScore(BaseModel):
    criterion: Literal[
        "objective_alignment", "clarity", "structure", "relevance",
        "examples", "practical_application",
    ]
    score: int
    rationale: str
    weakness: str


class InstructionalReview(BaseModel):
    summary: str
    objective_coverage: list[ObjectiveCoverage]
    rubric: list[RubricScore]
    issues: list[ProposedIssue]


# ---- HTTP request bodies ------------------------------------------------
class LessonCreate(BaseModel):
    title: str = Field(max_length=200)
    objective: str = Field(max_length=1000)

    @field_validator("title", "objective")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("must contain at least 3 non-space characters")
        return value


class ContentUpdate(BaseModel):
    base_version: int
    sections: list[Section]
    note: str = "Instructor edit"


class VideoAdd(BaseModel):
    url: str
