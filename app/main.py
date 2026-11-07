"""HTTP API and static front end."""
from __future__ import annotations

import re
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db, exporter, llm, pipeline, review
from .config import ROOT, get_settings
from .qc import checks
from .research import connectors
from .schemas import ContentUpdate, LessonCreate, VideoAdd

REPORTS = ("qc_factual", "qc_instructional")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="LessonForge", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_input(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Turn validation errors into one sentence the form can show."""
    parts = []
    for error in exc.errors():
        field = str(error["loc"][-1]).replace("_", " ").capitalize()
        parts.append(f"{field}: {error['msg'].removeprefix('Value error, ')}")
    return JSONResponse(status_code=422, content={"detail": ". ".join(parts)})


@app.exception_handler(review.ReviewError)
async def review_error(_: Request, exc: review.ReviewError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(db.VersionConflict)
async def version_conflict(_: Request, exc: db.VersionConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "detail": f"This lesson changed while you were editing (now version {exc.latest}). "
                  "Your text is still in the editor. Copy what you need, reload, and reapply it.",
        "latest": exc.latest,
    })


def require_lesson(lesson_id: int) -> dict:
    lesson = db.get_lesson(lesson_id)
    if lesson is None:
        raise HTTPException(404, "Lesson not found.")
    return lesson


def require_report_name(name: str) -> str:
    if name not in REPORTS:
        raise HTTPException(404, "Unknown report.")
    return name


def public_sources(research: dict | None) -> list[dict]:
    """Sources without their full text, which the browser does not need."""
    sources = (research or {}).get("sources", [])
    return [{**{k: v for k, v in s.items() if k != "text"}, "text_chars": len(s.get("text", ""))}
            for s in sources]


@app.get("/api/status")
def status() -> dict:
    ready, message = llm.llm_ready()
    settings = get_settings()
    return {
        "llm_ready": ready, "llm_message": message, "provider": settings.llm_provider,
        "web_search": connectors.web_search_available(),
        "youtube_api": bool(settings.youtube_api_key),
    }


@app.get("/api/lessons")
def lessons() -> list[dict]:
    return db.list_lessons()


@app.post("/api/lessons", status_code=201)
def create_lesson(body: LessonCreate) -> dict:
    lesson_id = db.create_lesson(body.title, body.objective)
    pipeline.start(lesson_id, list(db.STEPS))
    return {"id": lesson_id}


@app.get("/api/lessons/{lesson_id}")
def lesson_state(lesson_id: int) -> dict:
    lesson = require_lesson(lesson_id)
    research = db.get_artifact(lesson_id, "research")
    latest = db.latest_version(lesson_id)
    reports = {name: db.get_artifact(lesson_id, name) for name in REPORTS}
    version = latest["version"] if latest else 0
    for report in reports.values():
        if report:
            report["stale"] = report["lesson_version"] != version
    return {
        "lesson": lesson,
        "steps": db.get_steps(lesson_id),
        "running": pipeline.is_running(lesson_id),
        "version": version,
        "content": latest["content"] if latest else None,
        "time_budget": checks.time_budget(latest["content"]) if latest else None,
        "sources": public_sources(research),
        "research": {k: (research or {}).get(k) for k in
                     ("warnings", "diagnostics", "plan", "rejected_as_irrelevant")},
        "reports": reports,
        "versions": db.list_versions(lesson_id),
    }


@app.post("/api/lessons/{lesson_id}/steps/{name}/retry")
def retry_step(lesson_id: int, name: str, confirm: bool = Body(False, embed=True)) -> dict:
    require_lesson(lesson_id)
    if name not in db.STEPS:
        raise HTTPException(404, "Unknown step.")
    if name in ("research", "generate") and db.has_instructor_edits(lesson_id) and not confirm:
        raise HTTPException(409, "This will generate a new version. Your edits stay in History, "
                                 "but the new draft will not include them.")
    if not pipeline.start(lesson_id, pipeline.chain_from(name)):
        raise HTTPException(409, "A step is already running for this lesson.")
    return {"started": True}


@app.put("/api/lessons/{lesson_id}/content")
def save_content(lesson_id: int, body: ContentUpdate) -> dict:
    require_lesson(lesson_id)
    sections = [s.model_dump() for s in body.sections]
    return {"version": review.save_edit(lesson_id, sections, body.base_version, body.note)}


@app.post("/api/lessons/{lesson_id}/versions/{version}/restore")
def restore(lesson_id: int, version: int) -> dict:
    require_lesson(lesson_id)
    return {"version": review.restore_version(lesson_id, version)}


@app.post("/api/lessons/{lesson_id}/reports/{report}/issues/{issue_id}/apply")
def apply_issue(lesson_id: int, report: str, issue_id: str) -> dict:
    require_lesson(lesson_id)
    return {"version": review.apply_issue(lesson_id, require_report_name(report), issue_id)}


@app.post("/api/lessons/{lesson_id}/reports/{report}/issues/{issue_id}/status")
def issue_status(lesson_id: int, report: str, issue_id: str, status: str = Body(..., embed=True)) -> dict:
    require_lesson(lesson_id)
    if status not in ("open", "dismissed"):
        raise HTTPException(422, "Status must be open or dismissed.")
    review.set_issue_status(lesson_id, require_report_name(report), issue_id, status)
    return {"status": status}


@app.post("/api/lessons/{lesson_id}/videos", status_code=201)
def add_video(lesson_id: int, body: VideoAdd) -> dict:
    require_lesson(lesson_id)
    source = review.add_video(lesson_id, body.url)
    return {"id": source["id"], "title": source["title"]}


def _final(lesson_id: int) -> tuple[dict, dict, list[dict]]:
    lesson = require_lesson(lesson_id)
    latest = db.latest_version(lesson_id)
    if not latest:
        raise HTTPException(409, "There is no lesson content yet.")
    research = db.get_artifact(lesson_id, "research") or {}
    return lesson, latest, research.get("sources", [])


@app.get("/api/lessons/{lesson_id}/preview")
def preview(lesson_id: int) -> dict:
    lesson, latest, sources = _final(lesson_id)
    return {"html": exporter.lesson_fragment(lesson, latest, sources), "version": latest["version"]}


@app.get("/api/lessons/{lesson_id}/export")
def export(lesson_id: int) -> Response:
    lesson, latest, sources = _final(lesson_id)
    reports = {name: db.get_artifact(lesson_id, name) for name in REPORTS}
    slug = re.sub(r"[^a-z0-9]+", "-", lesson["title"].lower()).strip("-") or "lesson"
    return Response(
        exporter.bundle(lesson, latest, sources, reports), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{slug}-v{latest["version"]}.zip"'},
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
