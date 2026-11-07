from __future__ import annotations

import copy
import time

import pytest
from fastapi.testclient import TestClient

from app import db, llm, pipeline
from app.main import app
from app.qc import checks, factual
from tests.fakes import RESEARCH, FakeLLM


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Isolated database, fake LLM, and no network access."""
    monkeypatch.setenv("LESSONFORGE_DB", str(tmp_path / "test.db"))
    db.init()
    provider = FakeLLM()
    llm.set_provider(provider)
    monkeypatch.setattr(pipeline, "run_research", lambda *a: copy.deepcopy(RESEARCH))
    monkeypatch.setattr(checks, "check_link", lambda url: "ok")
    monkeypatch.setattr(factual.connectors, "wikipedia", lambda *a, **k: [])
    monkeypatch.setattr(factual.connectors, "web_search_available", lambda: False)
    yield provider
    llm.set_provider(None)


@pytest.fixture
def client(fake):
    with TestClient(app) as test_client:
        yield test_client


def wait_idle(lesson_id: int, timeout: float = 10) -> None:
    deadline = time.time() + timeout
    while pipeline.is_running(lesson_id):
        assert time.time() < deadline, "pipeline did not finish"
        time.sleep(0.02)


@pytest.fixture
def lesson(client):
    """A lesson taken all the way through the pipeline."""
    created = client.post("/api/lessons", json={"title": "HTTP Requests", "objective": "Explain GET and POST."})
    lesson_id = created.json()["id"]
    wait_idle(lesson_id)
    return lesson_id
