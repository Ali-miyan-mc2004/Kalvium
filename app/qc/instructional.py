"""QC layer 2: instructional quality.

This reviewer deliberately never sees the sources or layer 1's findings, so it
judges the lesson as a learner meets it and cannot be anchored by the research.
"""
from __future__ import annotations

import json

from .. import llm, prompts
from ..schemas import InstructionalReview
from . import checks
from .factual import lesson_text


def calibrate(score: int, weakness: str) -> int:
    """Keep scores on the scale, and reserve 5 for a criterion with no stated weakness."""
    score = max(1, min(5, score))
    return score if weakness.strip().lower().rstrip(".") in ("", "none") else min(score, 4)


def run(title: str, objective: str, content: dict, version: int) -> dict:
    budget = checks.time_budget(content)
    structure = checks.structure_checks(content)
    automated = checks.instructional_issues(content, budget, structure)

    user = (f"Lesson title: {title}\nLearning objective: {objective}\n\n"
            f"Lesson:\n{lesson_text(content)}\n\n"
            f"Measured time: {budget['total_minutes']} of {budget['limit']} minutes.\n"
            f"Deterministic findings already reported:\n"
            f"{json.dumps([i['description'] for i in automated], indent=1)}")
    result = llm.complete(InstructionalReview, prompts.INSTRUCTIONAL_REVIEW_SYSTEM, user,
                          role="qc", effort="high")

    rubric = [{**r.model_dump(), "score": calibrate(r.score, r.weakness)} for r in result.rubric]
    scores = [r["score"] for r in rubric]
    return {
        "layer": "qc_instructional", "title": "QC Layer 2: Instructional Quality",
        "lesson_version": version, "summary": result.summary,
        "average_score": round(sum(scores) / len(scores), 1) if scores else None,
        "objective_coverage": [c.model_dump() for c in result.objective_coverage],
        "rubric": rubric,
        "time_budget": budget, "structure_checks": structure,
        "issues": automated + [{**i.model_dump(), "origin": "reviewer"} for i in result.issues],
    }
