"""Measure the QC layers by planting known problems in a real lesson.

Uses a live model. It takes a saved lesson about HTTP and works on copies, so
the lesson itself is not changed.

    python -m evals.seeded_errors <lesson id>

Layer 1 gets false statements added to the lesson, and each one counts as caught
if the layer does not mark it supported. Layer 2 reviews the original lesson and
a damaged copy with no practice and no 5xx teaching, and should score the damaged
copy lower and report the damage.
"""
from __future__ import annotations

import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.qc import checks, factual, instructional  # noqa: E402

# (false statement, what it gets wrong)
FALSE_STATEMENTS = [
    ("The `POST` method is idempotent, so repeating an identical `POST` never has any extra effect [S3].",
     "POST is not idempotent"),
    ("A `404 Not Found` response belongs to the 5xx class of server errors [S1].",
     "404 is a 4xx client error"),
    ("The `GET` method is unsafe because every `GET` request changes the state of the server [S2].",
     "GET is a safe method"),
    ("The status code `201 Created` means that the request failed validation [S1].",
     "201 is a success code"),
    ("The 2xx class of status codes means that the server crashed while handling the request [S1].",
     "2xx means success"),
    ("Status codes in the 4xx class always mean that the server itself is broken [S1].",
     "4xx means a client error"),
    ("`503 Service Unavailable` is used when the client sends malformed syntax [S1].",
     "503 is a server condition; malformed syntax is 400"),
    ("HTTP was invented by Google engineers in 2015 [S1].",
     "HTTP long predates 2015 and was not created by Google"),
]


def seed_facts(content: dict) -> dict:
    """Add the false statements to the concept sections, in turn."""
    seeded = copy.deepcopy(content)
    concepts = [s for s in seeded["sections"] if s["kind"] == "concept"]
    for number, (statement, _) in enumerate(FALSE_STATEMENTS):
        target = concepts[number % len(concepts)]
        target["body"] = f"{target['body'].rstrip()}\n\n{statement}"
    return seeded


def damage_teaching(content: dict) -> dict:
    """Remove all practice and all teaching about the 5xx class."""
    damaged = copy.deepcopy(content)
    damaged["sections"] = [s for s in damaged["sections"] if s["kind"] not in ("activity", "check")]
    for section in damaged["sections"]:
        section["body"] = "\n".join(line for line in section["body"].splitlines()
                                    if "5xx" not in line and "50" not in line)
    return damaged


def matching_claim(rows: list[dict], statement: str) -> dict | None:
    """Find the extracted claim that came from a seeded statement."""
    plain = checks.CITATION.sub("", statement).strip().rstrip(".")
    for row in rows:
        if plain in row["quote"] or checks.CITATION.sub("", row["quote"]).strip().rstrip(".") in plain:
            return row
    return None


def evaluate_facts(content: dict, sources: list[dict], version: int) -> tuple[list[str], int]:
    report, _ = factual.run(seed_facts(content), sources, version)
    rows = report["claims"]
    caught = 0
    lines = ["| Planted false statement | Result | Verdict |", "|---|---|---|"]
    for statement, reason in FALSE_STATEMENTS:
        row = matching_claim(rows, statement)
        verdict = row["verdict"] if row else "not extracted"
        hit = row is not None and verdict != "supported"
        caught += hit
        lines.append(f"| {checks.CITATION.sub('', statement).strip()} ({reason}) | "
                     f"{'caught' if hit else '**missed**'} | {verdict} |")
    return lines, caught


def evaluate_teaching(title: str, objective: str, content: dict, version: int) -> tuple[list[str], dict, dict]:
    original = instructional.run(title, objective, content, version)
    damaged = instructional.run(title, objective, damage_teaching(content), version)
    lines = ["| Lesson | Average score | Issues | High severity | Sub-objectives weak or missing |", "|---|---|---|---|---|"]
    for label, report in (("Original", original), ("Damaged: no practice, no 5xx", damaged)):
        high = sum(1 for i in report["issues"] if i["severity"] == "high")
        gaps = sum(1 for c in report["objective_coverage"] if c["coverage"] != "strong")
        lines.append(f"| {label} | {report['average_score']} | {len(report['issues'])} | {high} | {gaps} |")
    return lines, original, damaged


def main() -> None:
    lesson_id = int(sys.argv[1])
    lesson = db.get_lesson(lesson_id)
    latest = db.latest_version(lesson_id)
    sources = db.get_artifact(lesson_id, "research")["sources"]

    fact_lines, caught = evaluate_facts(latest["content"], sources, latest["version"])
    teach_lines, original, damaged = evaluate_teaching(
        lesson["title"], lesson["objective"], latest["content"], latest["version"])

    output = "\n".join([
        "# QC evaluation results", "",
        f"Run on {datetime.now(timezone.utc).date()} against \"{lesson['title']}\", version {latest['version']}.",
        "Generated by `evals/seeded_errors.py`, which plants known problems and records what each layer finds.", "",
        "## QC Layer 1: planted factual errors", "",
        f"**Caught {caught} of {len(FALSE_STATEMENTS)}.**", "", *fact_lines, "",
        "## QC Layer 2: damaged lesson", "", *teach_lines, "",
        f"Score drop on the damaged lesson: {round(original['average_score'] - damaged['average_score'], 1)}.", "",
        "### What Layer 2 said about the damaged lesson", "", damaged["summary"], "",
    ])
    path = ROOT / "evals" / "results.md"
    path.write_text(output, encoding="utf8")
    print(output.encode("ascii", "replace").decode())


if __name__ == "__main__":
    main()
