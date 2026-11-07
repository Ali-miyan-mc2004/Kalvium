"""Starts the app with the fake LLM, to check the interface without a key.

For development only. Every lesson it produces is placeholder text, so it must
never be used for the demo or the submission.

    python -m tests.ui_server
"""
from __future__ import annotations

import copy
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["LESSONFORGE_DB"] = str(Path(tempfile.mkdtemp()) / "ui-test.db")
os.environ["LLM_PROVIDER"] = "FAKE (placeholder text, not a real lesson)"  # shown in the status bar

import uvicorn  # noqa: E402

from app import llm, pipeline  # noqa: E402
from app.main import app  # noqa: E402
from app.qc import checks, factual  # noqa: E402
from tests.fakes import RESEARCH, FakeLLM  # noqa: E402

llm.set_provider(FakeLLM())
pipeline.run_research = lambda *a: copy.deepcopy(RESEARCH)
checks.check_link = lambda url: "ok"
factual.connectors.wikipedia = lambda *a, **k: []
factual.connectors.web_search_available = lambda: False

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
