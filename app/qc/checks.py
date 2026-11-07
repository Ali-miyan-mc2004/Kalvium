"""Deterministic checks. Anything code can verify is not left to a model."""
from __future__ import annotations

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx

from ..config import LESSON_MAX_MINUTES, LESSON_MIN_MINUTES, READING_WPM, STUDY_FACTOR
from ..research import connectors

CITATION = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")
OLD_SOURCE_YEARS = 15


def cited_ids(text: str) -> list[str]:
    found: list[str] = []
    for group in CITATION.findall(text):
        found.extend(part.strip() for part in group.split(","))
    return found


def all_citations(content: dict) -> Counter:
    counts: Counter = Counter()
    for section in content["sections"]:
        counts.update(cited_ids(section["body"]))
    return counts


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def time_budget(content: dict) -> dict:
    """Time each section can honestly claim.

    No section takes less than its reading time, so a low planned figure cannot
    hide a long section. Sections the learner reads are also capped at
    STUDY_FACTOR times their reading time, so a high planned figure cannot make
    a thin lesson look full. Activities and checks keep their planned time,
    because the work happens away from the text.
    """
    rows = []
    for section in content["sections"]:
        reading = word_count(section["body"]) / READING_WPM
        effective = max(float(section["minutes"]), reading)
        if section["kind"] not in ("activity", "check"):
            effective = min(effective, max(reading * STUDY_FACTOR, 1.0))
        rows.append({
            "section_id": section["id"], "title": section["title"], "kind": section["kind"],
            "declared": section["minutes"], "reading": round(reading, 1),
            "effective": round(effective, 1),
        })
    total = round(sum(r["effective"] for r in rows), 1)
    return {"sections": rows, "total_minutes": total, "planned_minutes": sum(r["declared"] for r in rows),
            "limit": LESSON_MAX_MINUTES,
            "floor": LESSON_MIN_MINUTES, "within_limit": total <= LESSON_MAX_MINUTES,
            "too_short": total < LESSON_MIN_MINUTES}


def issue(category: str, severity: str, description: str, suggestion: str,
          section_id: str = "") -> dict:
    return {"severity": severity, "category": category, "section_id": section_id,
            "description": description, "suggestion": suggestion, "find": "", "replace": "",
            "origin": "automated check"}


# ---- layer 1: citations, links, source quality --------------------------
def citation_issues(content: dict, sources: list[dict]) -> list[dict]:
    by_id = {s["id"]: s for s in sources}
    found: list[dict] = []
    for section in content["sections"]:
        ids = cited_ids(section["body"])
        for unknown in sorted({i for i in ids if i not in by_id}):
            found.append(issue(
                "invalid_citation", "high",
                f"Section '{section['title']}' cites {unknown}, which is not in the research sources.",
                "Replace the citation with a real source, or remove the claim.", section["id"]))
        for sid in sorted({i for i in ids if i in by_id and not by_id[i].get("evidence")}):
            found.append(issue(
                "uncheckable_citation", "medium",
                f"Section '{section['title']}' cites {sid}, but we only hold metadata for it, "
                "so nothing can confirm that it says this.",
                "Cite a source whose text was retrieved, or check this one by hand.", section["id"]))
        if section["kind"] == "concept" and not ids:
            found.append(issue(
                "uncited_section", "medium",
                f"Concept section '{section['title']}' has no citations.",
                "Add citations for its factual statements.", section["id"]))
    return found


def source_issues(content: dict, sources: list[dict]) -> list[dict]:
    counts = all_citations(content)
    by_id = {s["id"]: s for s in sources}
    used = [by_id[i] for i in counts if i in by_id]
    found: list[dict] = []
    total = sum(counts[s["id"]] for s in used)

    for source in used:
        if source["tier"] >= 4:
            found.append(issue(
                "weak_source", "medium",
                f"{source['id']} ({source['domain']}) is an unreviewed publishing platform.",
                "Replace it with official documentation or a reference work."))
        year = source.get("year")
        if year and datetime.now(timezone.utc).year - int(year) > OLD_SOURCE_YEARS:
            found.append(issue(
                "dated_source", "low",
                f"{source['id']} '{source['title']}' dates from {year}.",
                "Confirm the cited facts are still current."))

    if total >= 6:
        top_id, top = counts.most_common(1)[0]
        if top / total > 0.5:
            found.append(issue(
                "source_concentration", "low",
                f"{top_id} carries {round(100 * top / total)}% of all citations.",
                "Cross-check the key facts against a second source."))
    if len({s["domain"] for s in used}) == 1 and used:
        found.append(issue(
            "single_domain", "low",
            f"Every citation comes from {used[0]['domain']}.",
            "Add an independent source so errors in one publisher are not repeated."))
    return found


def check_link(url: str) -> str:
    """Return ok, dead or unknown. A refusal to bots does not mean a page is gone."""
    try:
        with connectors.client() as http:
            response = http.get(url, timeout=12)
    except httpx.HTTPError:
        return "unknown"
    if response.status_code < 400:
        return "ok"
    return "dead" if response.status_code in (404, 410) else "unknown"


def audit_sources(content: dict, sources: list[dict]) -> tuple[list[dict], list[dict]]:
    counts = all_citations(content)
    picked = {v["source_id"] for v in content.get("videos", [])}
    used = [s for s in sources if s["id"] in counts or s["id"] in picked]
    with ThreadPoolExecutor(max_workers=6) as pool:
        statuses = list(pool.map(lambda s: check_link(s["url"]), used))
    status_by_id = {s["id"]: st for s, st in zip(used, statuses)}

    audit = [{
        "id": s["id"], "title": s["title"], "url": s["url"], "kind": s["kind"],
        "domain": s.get("domain", ""), "tier": s["tier"], "tier_reason": s["tier_reason"],
        "year": s.get("year"), "relevance": s.get("relevance"), "citations": counts.get(s["id"], 0),
        "text_retrieved": bool(s.get("evidence")), "link": status_by_id.get(s["id"], "not checked"),
    } for s in sources]

    dead = [issue("dead_link", "medium", f"{s['id']} no longer resolves: {s['url']}",
                  "Replace the source or remove what depends on it.")
            for s in used if status_by_id[s["id"]] == "dead"]
    return audit, dead


# ---- layer 2: time, structure ------------------------------------------
def structure_checks(content: dict) -> list[dict]:
    kinds = Counter(s["kind"] for s in content["sections"])
    check_body = "\n".join(s["body"] for s in content["sections"] if s["kind"] == "check")
    questions = len(re.findall(r"\?", check_body))
    return [
        {"check": "Opens with a hook", "passed": bool(content["sections"])
            and content["sections"][0]["kind"] == "hook"},
        {"check": "At least two concept sections", "passed": kinds["concept"] >= 2},
        {"check": "At least two worked examples", "passed": kinds["example"] >= 2},
        {"check": "At least one practical activity", "passed": kinds["activity"] >= 1},
        {"check": "Knowledge check present", "passed": kinds["check"] >= 1},
        {"check": "At least five knowledge-check questions", "passed": questions >= 5},
        {"check": "Ends with a summary", "passed": bool(content["sections"])
            and content["sections"][-1]["kind"] == "summary"},
    ]


def instructional_issues(content: dict, budget: dict, structure: list[dict]) -> list[dict]:
    found: list[dict] = []
    if not budget["within_limit"]:
        found.append(issue(
            "time_budget", "high",
            f"The lesson takes about {budget['total_minutes']} minutes, over the "
            f"{budget['limit']}-minute limit.",
            "Trim the longest sections until the total fits."))
    elif budget["too_short"]:
        found.append(issue(
            "time_budget", "medium",
            f"The content supports about {budget['total_minutes']} minutes although "
            f"{budget['planned_minutes']} are planned, so it may be too thin for a 45-minute slot.",
            "Deepen the explanations, or add practice or another worked example."))
    for row in budget["sections"]:
        if row["reading"] > row["declared"] + 1:
            found.append(issue(
                "pacing", "low",
                f"'{row['title']}' is planned at {row['declared']} min but takes about "
                f"{row['reading']} min to read.",
                "Shorten it or raise its planned minutes.", row["section_id"]))
    for item in structure:
        if not item["passed"]:
            found.append(issue("structure", "medium", f"Failed check: {item['check']}.",
                               "Add or reorder sections so the lesson meets this."))
    return found
