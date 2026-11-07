"""QC layer 1: research and factual accuracy.

The layer does not take the lesson's word for anything:
  1. Code checks citations, links and source quality.
  2. A model extracts every checkable claim.
  3. A separate judge rates each claim using only retrieved passages, and code
     confirms each evidence quote really occurs in the passage.
  4. Claims that fail get a second, independent lookup outside the lesson's
     own source pool, so a gap in the original research is not mistaken for
     a wrong claim.
  5. A reviewer turns the results into issues with concrete fixes.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from .. import llm, prompts
from ..research import connectors
from ..research import enrich
from ..research.corpus import Index, Passage, build_index, normalize
from ..schemas import ClaimList, FactualReview, VerdictList
from . import checks

JUDGE_BATCH = 20  # large enough to respect free-tier request quotas
PASSAGES_PER_CLAIM = 4
MAX_LOOKUPS = 8
UNRESOLVED = ("unsupported", "contradicted", "partially_supported", "unverified")


def lesson_text(content: dict) -> str:
    return "\n\n".join(
        f"<section id=\"{s['id']}\" kind=\"{s['kind']}\" title=\"{s['title']}\">\n{s['body']}\n</section>"
        for s in content["sections"])


def extract_claims(content: dict) -> list[dict]:
    result = llm.complete(ClaimList, prompts.CLAIMS_SYSTEM, lesson_text(content), role="qc", effort="medium")
    ids = {s["id"] for s in content["sections"]}
    return [c.model_dump() for c in result.claims if c.section_id in ids]


def retrieve(claim: dict, index: Index) -> list[Passage]:
    """Best passages overall, plus the best passage from each source it cites."""
    passages = index.search(claim["claim"], k=PASSAGES_PER_CLAIM)
    for source_id in dict.fromkeys(checks.cited_ids(claim["quote"])):
        passages.extend(index.search(claim["claim"], k=1, source_id=source_id))
    unique: dict[tuple[str, str], Passage] = {(p.source_id, p.text): p for p in passages}
    return list(unique.values())


def judge(claims: list[dict], index: Index) -> list[dict]:
    """Attach a verdict to each claim, in batches run in parallel."""
    batches = [claims[i:i + JUDGE_BATCH] for i in range(0, len(claims), JUDGE_BATCH)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        return [row for batch in pool.map(lambda b: _judge_batch(b, index), batches) for row in batch]


def _judge_batch(batch: list[dict], index: Index) -> list[dict]:
    retrieved = [retrieve(claim, index) for claim in batch]
    blocks = []
    for number, (claim, passages) in enumerate(zip(batch, retrieved)):
        rendered = "\n".join(f'<passage source="{p.source_id}">{p.text}</passage>' for p in passages)
        blocks.append(f"Claim {number}: {claim['claim']}\n{rendered or 'No passages were found.'}")
    result = llm.complete(VerdictList, prompts.JUDGE_SYSTEM, "\n\n".join(blocks), role="qc", effort="high")
    by_index = {v.claim_index: v for v in result.verdicts}

    rows = []
    for number, (claim, passages) in enumerate(zip(batch, retrieved)):
        verdict = by_index.get(number)
        if verdict is None:
            rows.append({**claim, "verdict": "unverified", "evidence_source": "", "evidence_quote": "",
                         "explanation": "The judge returned no verdict for this claim."})
            continue
        row = {**claim, **verdict.model_dump(exclude={"claim_index"})}
        rows.append(_confirm_quote(row, passages))
    return rows


def _confirm_quote(row: dict, passages: list[Passage]) -> dict:
    """A verdict resting on a quote that is not in the passage counts for nothing."""
    if row["verdict"] == "unsupported":
        return row
    quote = normalize(row["evidence_quote"])
    genuine = bool(quote) and any(
        p.source_id == row["evidence_source"] and quote in normalize(p.text) for p in passages)
    if not genuine:
        row = {**row, "verdict": "unverified",
               "explanation": "The evidence quote could not be found in the cited passage, so "
                              f"this verdict was discarded. Judge's note: {row['explanation']}"}
    return row


def independent_lookup(rows: list[dict], sources: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Look outside the lesson's sources for claims the first pass could not confirm."""
    pending = [r for r in rows if r["verdict"] in UNRESOLVED][:MAX_LOOKUPS]
    if not pending:
        return rows, [], []

    known = {s["url"].rstrip("/").lower() for s in sources}
    next_id = 1 + max(int(s["id"][1:]) for s in sources)
    fresh: list[dict] = []
    notes: list[str] = []
    use_web = connectors.web_search_available()

    def lookup(row: dict) -> list[dict]:
        found: list[dict] = []
        for name, fn in (("wikipedia", lambda q: connectors.wikipedia(q, limit=1)),
                         ("web", lambda q: connectors.web(q, limit=3) if use_web else [])):
            try:
                found.extend(fn(row["claim"][:150]))
            except Exception as exc:
                notes.append(f"Independent {name} lookup failed: {type(exc).__name__}")
        return found

    with ThreadPoolExecutor(max_workers=4) as pool:
        for items in pool.map(lookup, pending):
            for item in items:
                key = item["url"].rstrip("/").lower()
                if key in known or not item.get("text"):
                    continue
                known.add(key)
                fresh.append({**enrich(item), "id": f"S{next_id}", "relevance": None, "via": "qc_lookup"})
                next_id += 1

    if not fresh:
        return rows, [], sorted(set(notes)) or ["The independent lookup found no new sources."]

    rechecked = judge(pending, build_index(fresh))
    updated = {id(old): new for old, new in zip(pending, rechecked)}
    merged = []
    for row in rows:
        second = updated.get(id(row))
        if second is None:
            merged.append(row)
        elif second["verdict"] in ("supported", "contradicted"):
            merged.append({**second, "found_by": "independent lookup"})
        else:
            merged.append({**row, "second_opinion": second["verdict"]})
    return merged, fresh, sorted(set(notes))


def review(content: dict, rows: list[dict], audit: list[dict], automated: list[dict]) -> FactualReview:
    table = [{k: r.get(k) for k in ("section_id", "quote", "claim", "verdict", "evidence_source",
                                    "evidence_quote", "explanation")}
             for r in rows if r["verdict"] != "supported"]
    slim_audit = [{k: a[k] for k in ("id", "title", "domain", "tier", "year", "citations")} for a in audit]
    user = (f"Lesson:\n{lesson_text(content)}\n\n"
            f"Claims not fully supported:\n{json.dumps(table, indent=1)}\n\n"
            f"Source audit:\n{json.dumps(slim_audit, indent=1)}\n\n"
            f"Deterministic findings already reported:\n"
            f"{json.dumps([i['description'] for i in automated], indent=1)}")
    return llm.complete(FactualReview, prompts.FACTUAL_REVIEW_SYSTEM, user, role="qc", effort="high")


def run(content: dict, sources: list[dict], version: int) -> tuple[dict, list[dict]]:
    """Return (report, new sources found by the independent lookup)."""
    claims = extract_claims(content)
    rows = judge(claims, build_index(sources)) if claims else []
    rows, fresh, notes = independent_lookup(rows, sources)
    all_sources = sources + fresh

    audit, link_issues = checks.audit_sources(content, all_sources)
    automated = (checks.citation_issues(content, all_sources)
                 + checks.source_issues(content, all_sources) + link_issues)
    result = review(content, rows, audit, automated)

    tally: dict[str, int] = {}
    for row in rows:
        tally[row["verdict"]] = tally.get(row["verdict"], 0) + 1
    supported = tally.get("supported", 0)
    report = {
        "layer": "qc_factual", "title": "QC Layer 1: Research and Factual Accuracy",
        "lesson_version": version, "summary": result.summary,
        "claims_total": len(rows), "claims_supported": supported, "verdict_counts": tally,
        "support_rate": round(100 * supported / len(rows)) if rows else None,
        "claims": rows, "source_audit": audit, "new_sources": [s["id"] for s in fresh],
        "notes": notes,
        "issues": automated + [{**i.model_dump(), "origin": "reviewer"} for i in result.issues],
    }
    return report, fresh
