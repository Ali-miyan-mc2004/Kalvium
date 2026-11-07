"""Instructor actions: editing, applying QC recommendations, restoring versions."""
from __future__ import annotations

import copy

from . import db
from .research import enrich
from .research.connectors import oembed


class ReviewError(Exception):
    """An instructor action cannot go ahead. The message explains why."""


def save_edit(lesson_id: int, sections: list[dict], base_version: int, note: str) -> int:
    latest = db.latest_version(lesson_id)
    if not latest:
        raise ReviewError("There is no lesson to edit yet.")
    if not sections:
        raise ReviewError("A lesson needs at least one section.")
    ids = [s["id"] for s in sections]
    if len(set(ids)) != len(ids):
        raise ReviewError("Section ids must be unique.")
    content = copy.deepcopy(latest["content"])
    content["sections"] = sections
    return db.save_version(lesson_id, content, note or "Instructor edit", "instructor", base_version)


def _find_issue(report: dict, issue_id: str) -> dict:
    for item in report["issues"]:
        if item["id"] == issue_id:
            return item
    raise ReviewError("That recommendation no longer exists.")


def apply_issue(lesson_id: int, report_name: str, issue_id: str) -> int:
    """Apply a recommendation as a new version, so it can be undone from history."""
    version = 0
    with db.edit_artifact(lesson_id, report_name) as report:
        if report is None:
            raise ReviewError("That QC report does not exist.")
        item = _find_issue(report, issue_id)
        if item["status"] == "applied":
            raise ReviewError("This recommendation has already been applied.")
        if not item.get("applicable"):
            raise ReviewError("This recommendation needs a manual edit.")

        latest = db.latest_version(lesson_id)
        content = copy.deepcopy(latest["content"])
        section = next((s for s in content["sections"] if s["id"] == item["section_id"]), None)
        if section is None:
            raise ReviewError("The section this recommendation targets no longer exists.")

        find, replace = item["find"], item["replace"]
        # The instructor may have edited this text since the report was made.
        # Never guess at a fuzzy match: changing the wrong words is worse.
        stale = bool(find) and section["body"].count(find) != 1
        if stale:
            item["status"] = "stale"
        else:
            if find:
                section["body"] = section["body"].replace(find, replace)
            else:
                section["body"] = f"{section['body'].rstrip()}\n\n{replace.strip()}"
            version = db.save_version(
                lesson_id, content, f"Applied QC recommendation {issue_id}", "instructor", latest["version"])
            item["status"] = "applied"
            item["applied_in_version"] = version

    if stale:
        raise ReviewError("The text this fix targets has changed since the report ran. "
                          "Make the edit by hand, or rerun this QC layer.")
    return version


def set_issue_status(lesson_id: int, report_name: str, issue_id: str, status: str) -> None:
    with db.edit_artifact(lesson_id, report_name) as report:
        if report is None:
            raise ReviewError("That QC report does not exist.")
        _find_issue(report, issue_id)["status"] = status


def restore_version(lesson_id: int, version: int) -> int:
    """Restoring appends a copy, so the versions after it are kept as well."""
    target = db.get_version(lesson_id, version)
    if not target:
        raise ReviewError(f"Version {version} does not exist.")
    return db.save_version(lesson_id, target["content"], f"Restored version {version}", "instructor")


def add_video(lesson_id: int, url: str) -> dict:
    """Add an instructor-chosen video, confirmed with YouTube's oEmbed endpoint."""
    try:
        details = oembed(url)
    except ValueError as exc:
        raise ReviewError(str(exc)) from exc
    except Exception as exc:
        raise ReviewError(f"Could not check that video: {type(exc).__name__}") from exc

    with db.edit_artifact(lesson_id, "research") as research:
        if research is None:
            raise ReviewError("Run research before adding videos.")
        if any(s["url"] == details["url"] for s in research["sources"]):
            raise ReviewError("That video is already in the sources.")
        next_id = 1 + max((int(s["id"][1:]) for s in research["sources"]), default=0)
        source = {**enrich(details), "id": f"S{next_id}", "via": "instructor", "relevance": None}
        research["sources"].append(source)

    latest = db.latest_version(lesson_id)
    if latest:
        content = copy.deepcopy(latest["content"])
        content.setdefault("videos", []).append(
            {"source_id": source["id"], "why": "Added by the instructor.", "when_to_watch": "Optional"})
        db.save_version(lesson_id, content, f"Added video {source['id']}", "instructor")
    return source
