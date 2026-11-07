"""The four-step workflow: research, generate, QC layer 1, QC layer 2.

Each step saves its output before the next starts, so a failure costs only that
step and a retry resumes from there. Generation only ever appends a new version.
"""
from __future__ import annotations

import logging
import re
import threading
import traceback

from . import db, llm, prompts
from .config import LESSON_MAX_MINUTES
from .qc import checks, factual, instructional
from .research import ResearchError, fallback_plan, run_research
from .research.corpus import build_index
from .schemas import LessonDraft, QueryPlan

log = logging.getLogger("lessonforge")
QC_STEPS = ("qc_factual", "qc_instructional")
PASSAGES_PER_SUBTOPIC = 8
MAX_PASSAGES = 40

_active: set[int] = set()
_active_lock = threading.Lock()


class StepError(Exception):
    """A step failed in a way the instructor should read."""


def is_running(lesson_id: int) -> bool:
    with _active_lock:
        return lesson_id in _active


def start(lesson_id: int, steps: list[str]) -> bool:
    """Run steps in the background. Returns False if this lesson is already busy."""
    with _active_lock:
        if lesson_id in _active:
            return False
        _active.add(lesson_id)
    threading.Thread(target=_run_chain, args=(lesson_id, steps), daemon=True).start()
    return True


def _run_chain(lesson_id: int, steps: list[str]) -> None:
    try:
        for name in steps:
            ok = _run_step(lesson_id, name)
            # The QC layers are independent: one failing must not block the other.
            if not ok and name not in QC_STEPS:
                break
    finally:
        with _active_lock:
            _active.discard(lesson_id)


def _run_step(lesson_id: int, name: str) -> bool:
    db.set_step(lesson_id, name, "running", bump=True)
    try:
        detail = STEP_FUNCTIONS[name](lesson_id)
    except (StepError, ResearchError, llm.LLMError) as exc:
        db.set_step(lesson_id, name, "failed", error=str(exc))
        return False
    except Exception as exc:  # keep the app alive and show the failure
        log.error("Step %s failed:\n%s", name, traceback.format_exc())
        db.set_step(lesson_id, name, "failed", error=f"Unexpected error: {type(exc).__name__}: {exc}")
        return False
    db.set_step(lesson_id, name, "done", detail=detail)
    return True


def _lesson(lesson_id: int) -> dict:
    lesson = db.get_lesson(lesson_id)
    if lesson is None:
        raise StepError("Lesson not found.")
    return lesson


# ---- step 1: research ---------------------------------------------------
def step_research(lesson_id: int) -> str:
    lesson = _lesson(lesson_id)
    title, objective = lesson["title"], lesson["objective"]
    notes: list[str] = []
    try:
        plan = llm.complete(QueryPlan, prompts.PLAN_SYSTEM, prompts.plan_user(title, objective), effort="low")
    except llm.LLMError as exc:
        plan = fallback_plan(title, objective)
        notes.append(f"Used simple search queries because query planning failed: {exc}")

    result = run_research(title, objective, plan)
    result["warnings"] = notes + result["warnings"]

    # Keep sources the instructor added by hand across a research retry.
    previous = db.get_artifact(lesson_id, "research") or {}
    manual = [s for s in previous.get("sources", []) if s.get("via") == "instructor"]
    if manual:
        urls = {s["url"] for s in result["sources"]}
        next_id = 1 + len(result["sources"])
        for source in manual:
            if source["url"] not in urls:
                result["sources"].append({**source, "id": f"S{next_id}"})
                next_id += 1

    db.put_artifact(lesson_id, "research", result)
    counts = {kind: sum(1 for s in result["sources"] if s["kind"] == kind) for kind in ("web", "book", "video")}
    return f"{counts['web']} websites, {counts['book']} books, {counts['video']} videos"


# ---- step 2: generate ---------------------------------------------------
def _research(lesson_id: int) -> dict:
    research = db.get_artifact(lesson_id, "research")
    if not research:
        raise StepError("No research saved yet. Run the research step first.")
    return research


def build_context(research: dict) -> tuple[str, str, str]:
    sources = research["sources"]
    index = build_index(sources)
    chosen: dict[tuple[str, str], object] = {}
    for subtopic in research["plan"]["subtopics"]:
        for passage in index.search(subtopic, k=PASSAGES_PER_SUBTOPIC):
            chosen.setdefault((passage.source_id, passage.text), passage)
    by_id = {s["id"]: s for s in sources}
    blocks = [
        f'<source id="{p.source_id}" title="{by_id[p.source_id]["title"]}">\n{p.text}\n</source>'
        for p in list(chosen.values())[:MAX_PASSAGES]
    ]
    videos = "\n".join(
        f"{s['id']}: {s['title']} ({s.get('channel', 'unknown channel')}"
        + (f", {s['duration_s'] // 60} min" if s.get("duration_s") else "") + ")"
        for s in sources if s["kind"] == "video")
    reading = "\n".join(f"{s['title']} by {s.get('author') or 'unknown author'}"
                        for s in sources if s["kind"] == "book" and not s.get("evidence"))
    return "\n\n".join(blocks), videos, reading


def normalize_draft(draft: LessonDraft, sources: list[dict]) -> dict:
    """Remove anything the model could have got structurally wrong."""
    video_ids = {s["id"] for s in sources if s["kind"] == "video"}
    content = draft.model_dump()
    content["videos"] = [v for v in content["videos"] if v["source_id"] in video_ids]
    for number, section in enumerate(content["sections"], start=1):
        section["id"] = f"s{number}"
        section["minutes"] = max(1, section["minutes"])
        section["body"] = re.sub(r"^# .*\n+", "", section["body"].strip())
    return content


def step_generate(lesson_id: int) -> str:
    lesson = _lesson(lesson_id)
    research = _research(lesson_id)
    passages, videos, reading = build_context(research)
    if not passages:
        raise StepError("The research contains no readable passages. Retry research.")

    feedback = ""
    content: dict = {}
    for _ in range(2):  # one automatic correction if the draft misses the time budget
        user = prompts.generate_user(lesson["title"], lesson["objective"], research["plan"]["subtopics"],
                                     passages, videos, reading, feedback)
        draft = llm.complete(LessonDraft, prompts.GENERATE_SYSTEM, user, effort="high")
        if not draft.sections:
            raise StepError("The model returned a lesson with no sections. Retry this step.")
        content = normalize_draft(draft, research["sources"])
        budget = checks.time_budget(content)
        if budget["within_limit"] and not budget["too_short"]:
            break
        if budget["too_short"]:
            feedback = (f"The draft plans {budget['planned_minutes']} minutes, but its text only supports "
                        f"{budget['total_minutes']}. Teach in more depth: explain why, contrast similar "
                        "cases, cover common mistakes, and develop the worked examples. Do not pad.")
        else:
            feedback = (f"The draft takes {budget['total_minutes']} minutes at 180 words per minute. "
                        f"Cut it to fit within {LESSON_MAX_MINUTES} minutes.")

    version = db.save_version(lesson_id, content, "Generated from research", "ai")
    budget = checks.time_budget(content)
    return f"Version {version}: {len(content['sections'])} sections, about {budget['total_minutes']} minutes"


# ---- steps 3 and 4: QC --------------------------------------------------
def _latest(lesson_id: int) -> dict:
    latest = db.latest_version(lesson_id)
    if not latest:
        raise StepError("There is no lesson to review yet. Run generation first.")
    return latest


def prepare_issues(report: dict, content: dict) -> dict:
    """Number the issues and work out which suggested fixes can be applied directly."""
    bodies = {s["id"]: s["body"] for s in content["sections"]}
    for number, item in enumerate(report["issues"], start=1):
        item["id"] = f"{report['layer']}-{number}"
        item["status"] = "open"
        body = bodies.get(item["section_id"])
        find, replace = item["find"], item["replace"]
        if body is None or (not find and not replace):
            item["applicable"] = False
        elif find:
            item["applicable"] = body.count(find) == 1
        else:
            item["applicable"] = True
    return report


def _count(report: dict) -> str:
    total = len(report["issues"])
    return f"{total} issue{'' if total == 1 else 's'}"


def step_qc_factual(lesson_id: int) -> str:
    latest = _latest(lesson_id)
    research = _research(lesson_id)
    report, fresh = factual.run(latest["content"], research["sources"], latest["version"])
    report = prepare_issues(report, latest["content"])
    if fresh:
        with db.edit_artifact(lesson_id, "research") as stored:
            stored["sources"].extend(fresh)
    db.put_artifact(lesson_id, "qc_factual", report)
    rate = report["support_rate"]
    return f"{rate if rate is not None else 'n/a'}% of {report['claims_total']} claims supported, {_count(report)}"


def step_qc_instructional(lesson_id: int) -> str:
    lesson = _lesson(lesson_id)
    latest = _latest(lesson_id)
    report = instructional.run(lesson["title"], lesson["objective"], latest["content"], latest["version"])
    report = prepare_issues(report, latest["content"])
    db.put_artifact(lesson_id, "qc_instructional", report)
    return f"Average {report['average_score']}/5, {_count(report)}"


STEP_FUNCTIONS = {
    "research": step_research,
    "generate": step_generate,
    "qc_factual": step_qc_factual,
    "qc_instructional": step_qc_instructional,
}


def chain_from(name: str) -> list[str]:
    """Steps to run on a retry. QC reruns stand alone; earlier steps flow onward."""
    if name in QC_STEPS:
        return [name]
    return db.STEPS[db.STEPS.index(name):]
