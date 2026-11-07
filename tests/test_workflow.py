"""The whole workflow through the HTTP API, with a fake LLM and no network."""
from __future__ import annotations

import copy
import io
import json
import zipfile

from app import db, pipeline
from app.research import ResearchError
from app.schemas import ClaimList, FactualReview, InstructionalReview, LessonDraft
from tests.conftest import wait_idle
from tests.fakes import RESEARCH, draft


def state(client, lesson_id):
    return client.get(f"/api/lessons/{lesson_id}").json()


def step(data, name):
    return next(s for s in data["steps"] if s["name"] == name)


def edit_payload(data, text="Instructor wrote this."):
    sections = copy.deepcopy(data["content"]["sections"])
    sections[1]["body"] = text
    return {"base_version": data["version"], "sections": sections}


# ---- input validation ---------------------------------------------------
def test_blank_inputs_are_rejected(client):
    for body in ({"title": "   ", "objective": "Explain GET."}, {"title": "HTTP", "objective": ""}, {"title": "HTTP"}):
        response = client.post("/api/lessons", json=body)
        assert response.status_code == 422
        assert isinstance(response.json()["detail"], str)
    assert client.get("/api/lessons").json() == []


# ---- happy path -----------------------------------------------------------
def test_full_pipeline(client, lesson):
    data = state(client, lesson)
    assert [s["status"] for s in data["steps"]] == ["done"] * 4
    assert data["version"] == 1
    assert data["time_budget"]["within_limit"]
    assert [v["source_id"] for v in data["content"]["videos"]] == ["S4"], "invented video IDs are removed"
    assert "text" not in data["sources"][0], "source text stays on the server"

    factual, instructional = data["reports"]["qc_factual"], data["reports"]["qc_instructional"]
    assert factual["layer"] != instructional["layer"]
    assert factual["claims_total"] == 2 and factual["verdict_counts"]["supported"] == 1
    assert instructional["average_score"] == 3.5
    assert all(i["status"] == "open" for i in factual["issues"] + instructional["issues"])


def test_qc_layers_are_independent(client, fake, lesson):
    """Layer 2 must never be shown the sources or layer 1's findings."""
    seen = {}
    original = fake.complete

    def spy(schema, system, user, **kwargs):
        seen[schema.__name__] = user
        return original(schema, system, user, **kwargs)

    fake.complete = spy
    pipeline._run_step(lesson, "qc_instructional")
    assert "One claim needs evidence" not in seen["InstructionalReview"]
    assert "developer.mozilla.org" not in seen["InstructionalReview"]


def test_over_length_draft_gets_one_automatic_correction(client, fake):
    fake.drafts = [draft(words_per_section=2000), draft()]
    lesson_id = client.post("/api/lessons", json={"title": "HTTP", "objective": "Explain GET."}).json()["id"]
    wait_idle(lesson_id)
    assert fake.calls.count("LessonDraft") == 2
    assert state(client, lesson_id)["time_budget"]["within_limit"]


def test_thin_draft_gets_one_automatic_correction(client, fake):
    fake.drafts = [draft(words_per_section=40), draft()]
    lesson_id = client.post("/api/lessons", json={"title": "HTTP", "objective": "Explain GET."}).json()["id"]
    wait_idle(lesson_id)
    assert fake.calls.count("LessonDraft") == 2
    assert state(client, lesson_id)["time_budget"]["too_short"] is False


def test_fabricated_quotes_lower_the_support_rate(client, fake):
    fake.fabricate_quote = True
    lesson_id = client.post("/api/lessons", json={"title": "HTTP", "objective": "Explain GET."}).json()["id"]
    wait_idle(lesson_id)
    report = state(client, lesson_id)["reports"]["qc_factual"]
    assert report["claims_supported"] == 0
    assert report["verdict_counts"]["unverified"] == 1


# ---- failure and retry ------------------------------------------------------
def test_research_failure_stops_the_chain_and_retry_recovers(client, fake, monkeypatch):
    def fail(*_):
        raise ResearchError("Only 0 usable sources found.")

    monkeypatch.setattr(pipeline, "run_research", fail)
    lesson_id = client.post("/api/lessons", json={"title": "HTTP", "objective": "Explain GET."}).json()["id"]
    wait_idle(lesson_id)
    data = state(client, lesson_id)
    assert step(data, "research")["status"] == "failed"
    assert "usable sources" in step(data, "research")["error"]
    assert step(data, "generate")["status"] == "pending"

    monkeypatch.setattr(pipeline, "run_research", lambda *a: copy.deepcopy(RESEARCH))
    assert client.post(f"/api/lessons/{lesson_id}/steps/research/retry", json={}).status_code == 200
    wait_idle(lesson_id)
    data = state(client, lesson_id)
    assert [s["status"] for s in data["steps"]] == ["done"] * 4
    assert step(data, "research")["error"] is None
    assert step(data, "research")["attempts"] == 2


def test_generation_failure_keeps_research_and_retry_reuses_it(client, fake, monkeypatch):
    fake.fail_on = {LessonDraft}
    lesson_id = client.post("/api/lessons", json={"title": "HTTP", "objective": "Explain GET."}).json()["id"]
    wait_idle(lesson_id)
    data = state(client, lesson_id)
    assert step(data, "generate")["status"] == "failed"
    assert data["sources"], "research survives the failure"

    fake.fail_on = set()

    def must_not_run(*_):
        raise AssertionError("retrying generation must not repeat research")

    monkeypatch.setattr(pipeline, "run_research", must_not_run)
    client.post(f"/api/lessons/{lesson_id}/steps/generate/retry", json={})
    wait_idle(lesson_id)
    assert state(client, lesson_id)["version"] == 1


def test_planning_failure_falls_back_to_simple_queries(client, fake):
    from app.schemas import QueryPlan
    fake.fail_on = {QueryPlan}
    lesson_id = client.post("/api/lessons", json={"title": "HTTP", "objective": "Explain GET."}).json()["id"]
    wait_idle(lesson_id)
    data = state(client, lesson_id)
    assert step(data, "research")["status"] == "done"
    assert any("query planning failed" in w for w in data["research"]["warnings"])


def test_one_qc_layer_failing_does_not_block_the_other(client, fake):
    fake.fail_on = {FactualReview}
    lesson_id = client.post("/api/lessons", json={"title": "HTTP", "objective": "Explain GET."}).json()["id"]
    wait_idle(lesson_id)
    data = state(client, lesson_id)
    assert step(data, "qc_factual")["status"] == "failed"
    assert step(data, "qc_instructional")["status"] == "done"
    assert data["reports"]["qc_instructional"] is not None

    fake.fail_on = set()
    client.post(f"/api/lessons/{lesson_id}/steps/qc_factual/retry", json={})
    wait_idle(lesson_id)
    assert state(client, lesson_id)["reports"]["qc_factual"] is not None


def test_failed_qc_leaves_instructor_edits_untouched(client, fake, lesson):
    data = state(client, lesson)
    client.put(f"/api/lessons/{lesson}/content", json=edit_payload(data))
    fake.fail_on = {ClaimList, InstructionalReview}
    client.post(f"/api/lessons/{lesson}/steps/qc_factual/retry", json={})
    wait_idle(lesson)
    data = state(client, lesson)
    assert step(data, "qc_factual")["status"] == "failed"
    assert data["content"]["sections"][1]["body"] == "Instructor wrote this."


def test_regenerating_over_edits_needs_confirmation_and_keeps_them(client, lesson):
    data = state(client, lesson)
    client.put(f"/api/lessons/{lesson}/content", json=edit_payload(data))
    refused = client.post(f"/api/lessons/{lesson}/steps/generate/retry", json={})
    assert refused.status_code == 409
    assert state(client, lesson)["version"] == 2

    assert client.post(f"/api/lessons/{lesson}/steps/generate/retry", json={"confirm": True}).status_code == 200
    wait_idle(lesson)
    assert state(client, lesson)["version"] == 3
    assert db.get_version(lesson, 2)["content"]["sections"][1]["body"] == "Instructor wrote this."


def test_interrupted_step_can_be_retried_after_restart(client, lesson):
    db.set_step(lesson, "qc_factual", "running")
    db.init()  # what startup does
    failed = step(state(client, lesson), "qc_factual")
    assert failed["status"] == "failed" and "Interrupted" in failed["error"]


def test_two_runs_cannot_overlap(client, lesson):
    with pipeline._active_lock:
        pipeline._active.add(lesson)
    try:
        assert client.post(f"/api/lessons/{lesson}/steps/qc_factual/retry", json={}).status_code == 409
    finally:
        with pipeline._active_lock:
            pipeline._active.discard(lesson)


# ---- review -------------------------------------------------------------
def test_edit_creates_a_version_and_marks_reports_stale(client, lesson):
    data = state(client, lesson)
    assert client.put(f"/api/lessons/{lesson}/content", json=edit_payload(data)).json()["version"] == 2
    data = state(client, lesson)
    assert data["content"]["sections"][1]["body"] == "Instructor wrote this."
    assert data["reports"]["qc_factual"]["stale"] is True


def test_stale_browser_tab_cannot_overwrite_newer_work(client, lesson):
    data = state(client, lesson)
    first = edit_payload(data, "First tab.")
    second = edit_payload(data, "Second tab.")
    assert client.put(f"/api/lessons/{lesson}/content", json=first).status_code == 200
    conflict = client.put(f"/api/lessons/{lesson}/content", json=second)
    assert conflict.status_code == 409 and conflict.json()["latest"] == 2
    assert state(client, lesson)["content"]["sections"][1]["body"] == "First tab."


def test_applying_a_fix_replaces_the_text(client, lesson):
    report = state(client, lesson)["reports"]["qc_factual"]
    fix = next(i for i in report["issues"] if i["origin"] == "reviewer")
    assert fix["applicable"]
    url = f"/api/lessons/{lesson}/reports/qc_factual/issues/{fix['id']}/apply"
    assert client.post(url).json()["version"] == 2
    data = state(client, lesson)
    assert "representation of a resource" in data["content"]["sections"][1]["body"]
    assert next(i for i in data["reports"]["qc_factual"]["issues"] if i["id"] == fix["id"])["status"] == "applied"
    assert client.post(url).status_code == 409, "a fix cannot be applied twice"


def test_additive_fix_appends_to_the_section(client, lesson):
    report = state(client, lesson)["reports"]["qc_instructional"]
    fix = next(i for i in report["issues"] if i["origin"] == "reviewer")
    client.post(f"/api/lessons/{lesson}/reports/qc_instructional/issues/{fix['id']}/apply")
    assert state(client, lesson)["content"]["sections"][3]["body"].endswith("A 503 means the server is unavailable.")


def test_fix_refuses_text_the_instructor_has_changed(client, lesson):
    data = state(client, lesson)
    fix = next(i for i in data["reports"]["qc_factual"]["issues"] if i["origin"] == "reviewer")
    client.put(f"/api/lessons/{lesson}/content", json=edit_payload(data, "Rewritten entirely."))
    response = client.post(f"/api/lessons/{lesson}/reports/qc_factual/issues/{fix['id']}/apply")
    assert response.status_code == 409 and "changed" in response.json()["detail"]
    after = state(client, lesson)
    assert after["version"] == 2 and after["content"]["sections"][1]["body"] == "Rewritten entirely."
    assert next(i for i in after["reports"]["qc_factual"]["issues"] if i["id"] == fix["id"])["status"] == "stale"


def test_dismiss_and_reopen(client, lesson):
    issue = state(client, lesson)["reports"]["qc_instructional"]["issues"][0]
    url = f"/api/lessons/{lesson}/reports/qc_instructional/issues/{issue['id']}/status"
    client.post(url, json={"status": "dismissed"})
    assert state(client, lesson)["reports"]["qc_instructional"]["issues"][0]["status"] == "dismissed"
    client.post(url, json={"status": "open"})
    assert state(client, lesson)["reports"]["qc_instructional"]["issues"][0]["status"] == "open"
    assert client.post(url, json={"status": "applied"}).status_code == 422


def test_restore_appends_rather_than_rewinds(client, lesson):
    data = state(client, lesson)
    original = data["content"]["sections"][1]["body"]
    client.put(f"/api/lessons/{lesson}/content", json=edit_payload(data))
    assert client.post(f"/api/lessons/{lesson}/versions/1/restore").json()["version"] == 3
    assert state(client, lesson)["content"]["sections"][1]["body"] == original
    assert db.get_version(lesson, 2)["content"]["sections"][1]["body"] == "Instructor wrote this."


# ---- preview and export ------------------------------------------------------
def test_preview_escapes_html_and_blocks_script_links(client, lesson):
    data = state(client, lesson)
    payload = edit_payload(data, '<script>alert(1)</script> [click](javascript:alert(1)) A fact [S1].')
    client.put(f"/api/lessons/{lesson}/content", json=payload)
    html = client.get(f"/api/lessons/{lesson}/preview").json()["html"]
    assert "<script>" not in html and 'href="javascript:' not in html
    assert 'href="#ref-S1"' in html and 'id="ref-S1"' in html


def test_export_bundle(client, lesson):
    response = client.get(f"/api/lessons/{lesson}/export")
    assert response.status_code == 200
    assert "http-requests-v1.zip" in response.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert set(archive.namelist()) == {
        "lesson.md", "lesson.html", "references.md", "manifest.json",
        "qc_factual_report.md", "qc_factual_report.json",
        "qc_instructional_report.md", "qc_instructional_report.json",
    }
    lesson_md = archive.read("lesson.md").decode()
    assert "## References" in lesson_md and "developer.mozilla.org/status" in lesson_md
    assert "Supplementary videos" in lesson_md and "HTTP in 5 minutes" in lesson_md
    assert "HTTP: The Definitive Guide" in lesson_md, "uncited books appear as further reading"
    assert "Claim verification" in archive.read("qc_factual_report.md").decode()
    assert "Objective coverage" in archive.read("qc_instructional_report.md").decode()


def test_export_flags_reports_older_than_the_lesson(client, lesson):
    data = state(client, lesson)
    client.put(f"/api/lessons/{lesson}/content", json=edit_payload(data))
    archive = zipfile.ZipFile(io.BytesIO(client.get(f"/api/lessons/{lesson}/export").content))
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["version"] == 2
    assert manifest["reports"]["qc_factual"]["current"] is False
    assert "predates" in archive.read("qc_factual_report.md").decode()


def test_export_before_any_content_is_refused(client, fake, monkeypatch):
    fake.fail_on = {LessonDraft}
    lesson_id = client.post("/api/lessons", json={"title": "HTTP", "objective": "Explain GET."}).json()["id"]
    wait_idle(lesson_id)
    assert client.get(f"/api/lessons/{lesson_id}/export").status_code == 409


def test_unknown_lesson_is_404(client):
    assert client.get("/api/lessons/999").status_code == 404
