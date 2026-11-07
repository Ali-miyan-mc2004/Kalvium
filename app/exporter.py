"""Rendering and export: Markdown, safe HTML, and the final ZIP bundle."""
from __future__ import annotations

import html
import io
import json
import re
import zipfile

from markdown_it import MarkdownIt

from .qc import checks

# html=False escapes raw HTML, so model output or pasted text cannot inject markup.
_markdown = MarkdownIt("commonmark", {"html": False}).enable("table")

PAGE_STYLE = """body{font:17px/1.6 Georgia,serif;max-width:46rem;margin:2rem auto;padding:0 1rem;color:#1f2328}
h1,h2,h3{font-family:system-ui,sans-serif;line-height:1.25}code{background:#f0f1f3;padding:.1em .3em;border-radius:3px}
pre{background:#f0f1f3;padding:1rem;overflow:auto}pre code{background:none}table{border-collapse:collapse}
td,th{border:1px solid #ccd;padding:.3rem .6rem}.meta{color:#57606a;font-family:system-ui,sans-serif;font-size:.9rem}
sup a{text-decoration:none}"""


def references(content: dict, sources: list[dict]) -> dict:
    """Built from what the lesson cites, so a reference can never be made up."""
    counts = checks.all_citations(content)
    by_id = {s["id"]: s for s in sources}
    cited = sorted((by_id[i] for i in counts if i in by_id), key=lambda s: int(s["id"][1:]))
    videos = [{**by_id[v["source_id"]], **v} for v in content.get("videos", []) if v["source_id"] in by_id]
    reading = [s for s in sources if s["kind"] == "book" and s["id"] not in counts and not s.get("evidence")]
    return {"cited": cited, "videos": videos, "further_reading": reading}


def _citation_line(source: dict) -> str:
    parts = [source["title"]]
    if source.get("author"):
        parts.append(source["author"])
    if source.get("year"):
        parts.append(str(source["year"]))
    return ". ".join(parts)


def _minutes(seconds: int | None) -> str:
    return f" ({max(1, round(seconds / 60))} min)" if seconds else ""


def references_markdown(content: dict, sources: list[dict]) -> str:
    refs = references(content, sources)
    lines = ["## References", ""]
    lines += [f"- **[{s['id']}]** {_citation_line(s)}. <{s['url']}>" for s in refs["cited"]]
    if not refs["cited"]:
        lines.append("_No sources are cited._")
    if refs["further_reading"]:
        lines += ["", "### Further reading", ""]
        lines += [f"- {_citation_line(s)}. <{s['url']}>" for s in refs["further_reading"]]
    return "\n".join(lines)


def videos_markdown(content: dict, sources: list[dict]) -> str:
    videos = references(content, sources)["videos"]
    if not videos:
        return ""
    lines = ["## Supplementary videos", "",
             "_Optional extras. They are not counted in the 45-minute lesson time._", ""]
    for video in videos:
        channel = f", {video['channel']}" if video.get("channel") else ""
        lines.append(f"- [{video['title']}]({video['url']}){_minutes(video.get('duration_s'))}{channel}. "
                     f"{video['why']} Watch: {video['when_to_watch']}")
    return "\n".join(lines)


def lesson_markdown(lesson: dict, version: dict, sources: list[dict]) -> str:
    content = version["content"]
    budget = checks.time_budget(content)
    lines = [f"# {lesson['title']}", "",
             f"**Learning objective:** {lesson['objective']}", "",
             f"**Estimated time:** {budget['total_minutes']} minutes (limit {budget['limit']})", ""]
    if content.get("prerequisites"):
        lines += ["**Prerequisites:** " + "; ".join(content["prerequisites"]), ""]
    if content.get("subobjectives"):
        lines += ["**By the end of this lesson you will be able to:**", ""]
        lines += [f"- {item}" for item in content["subobjectives"]] + [""]
    for section in content["sections"]:
        lines += [f"## {section['title']} ({section['minutes']} min)", "", section["body"], ""]
    videos = videos_markdown(content, sources)
    if videos:
        lines += [videos, ""]
    lines.append(references_markdown(content, sources))
    return "\n".join(lines)


def _link_citations(rendered: str) -> str:
    def link(match: re.Match) -> str:
        ids = [i.strip() for i in match.group(1).split(",")]
        return "<sup>[" + ", ".join(f'<a href="#ref-{i}">{i}</a>' for i in ids) + "]</sup>"
    return checks.CITATION.sub(link, rendered)


def lesson_fragment(lesson: dict, version: dict, sources: list[dict]) -> str:
    rendered = _markdown.render(lesson_markdown(lesson, version, sources))
    rendered = _link_citations(rendered)
    # Give each reference entry an anchor for the citation links above.
    return re.sub(r"<li><strong><sup>\[<a href=\"#ref-(S\d+)\">", r'<li id="ref-\1"><strong><sup>[<a href="#ref-\1">', rendered)


def lesson_page(lesson: dict, version: dict, sources: list[dict]) -> str:
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{html.escape(lesson['title'])}</title><style>{PAGE_STYLE}</style></head><body>"
            f"{lesson_fragment(lesson, version, sources)}"
            f"<p class=\"meta\">Exported from LessonForge, version {version['version']}.</p></body></html>")


def _issues_markdown(report: dict) -> list[str]:
    lines = ["## Issues and recommendations", ""]
    if not report["issues"]:
        lines.append("No issues were found.")
    for item in report["issues"]:
        where = f" (section {item['section_id']})" if item["section_id"] else ""
        lines += [f"### [{item['severity'].upper()}] {item['category']}{where}", "",
                  f"- Status: {item['status']}", f"- Found by: {item.get('origin', 'reviewer')}",
                  f"- Problem: {item['description']}", f"- Recommendation: {item['suggestion']}"]
        if item["find"]:
            lines.append(f"- Replace: `{item['find']}`")
        if item["replace"]:
            lines.append(f"- With: {item['replace']}")
        lines.append("")
    return lines


def report_markdown(report: dict, current_version: int) -> str:
    lines = [f"# {report['title']}", "",
             f"Reviewed lesson version {report['lesson_version']}. Exported version {current_version}.", ""]
    if report["lesson_version"] != current_version:
        lines += ["> This report predates the exported version, so some findings may be out of date.", ""]
    lines += [report["summary"], ""]

    if report["layer"] == "qc_factual":
        rate = report["support_rate"]
        lines += [f"**Claims supported:** {report['claims_supported']} of {report['claims_total']}"
                  + (f" ({rate}%)" if rate is not None else ""), ""]
        lines += ["## Claim verification", "", "| Section | Claim | Verdict | Evidence |", "|---|---|---|---|"]
        for row in report["claims"]:
            evidence = f"{row['evidence_source']}: \"{row['evidence_quote']}\"" if row["evidence_quote"] else row["explanation"]
            cells = [row["section_id"], row["claim"], row["verdict"], evidence]
            lines.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
        lines += ["", "## Source audit", "", "| ID | Source | Tier | Cited | Text retrieved | Link |", "|---|---|---|---|---|---|"]
        for row in report["source_audit"]:
            title = row["title"].replace("|", "\\|")
            lines.append(f"| {row['id']} | [{title}]({row['url']}) | {row['tier']} | {row['citations']} | "
                         f"{'yes' if row['text_retrieved'] else 'no'} | {row['link']} |")
        lines.append("")
    else:
        budget = report["time_budget"]
        lines += [f"**Average rubric score:** {report['average_score']} / 5", "",
                  f"**Measured time:** {budget['total_minutes']} of {budget['limit']} minutes", "",
                  "## Objective coverage", "", "| Sub-objective | Coverage | Where | Assessed | Note |", "|---|---|---|---|---|"]
        for row in report["objective_coverage"]:
            lines.append(f"| {row['subobjective']} | {row['coverage']} | {row['where']} | "
                         f"{'yes' if row['assessed_by_check'] else 'no'} | {row['note']} |")
        lines += ["", "## Rubric", "", "| Criterion | Score | Rationale | Weakness |", "|---|---|---|---|"]
        lines += [f"| {r['criterion']} | {r['score']} | {r['rationale']} | {r['weakness']} |" for r in report["rubric"]]
        lines += ["", "## Structure checks", ""]
        lines += [f"- {'Pass' if c['passed'] else 'Fail'}: {c['check']}" for c in report["structure_checks"]]
        lines.append("")
    return "\n".join(lines + _issues_markdown(report))


def bundle(lesson: dict, version: dict, sources: list[dict], reports: dict[str, dict | None]) -> bytes:
    buffer = io.BytesIO()
    number = version["version"]
    manifest = {"lesson": lesson["title"], "objective": lesson["objective"], "version": number, "reports": {}}
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("lesson.md", lesson_markdown(lesson, version, sources))
        archive.writestr("lesson.html", lesson_page(lesson, version, sources))
        archive.writestr("references.md", references_markdown(version["content"], sources))
        for name, report in reports.items():
            if report is None:
                manifest["reports"][name] = "not run"
                continue
            archive.writestr(f"{name}_report.md", report_markdown(report, number))
            archive.writestr(f"{name}_report.json", json.dumps(report, indent=2))
            manifest["reports"][name] = {
                "reviewed_version": report["lesson_version"],
                "current": report["lesson_version"] == number,
                "open_issues": sum(1 for i in report["issues"] if i["status"] == "open"),
            }
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buffer.getvalue()
