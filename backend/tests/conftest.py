import os
import tempfile

# Point the whole test session at a throwaway database *before* app.main is imported: the
# SQLModel engine is created at import time from settings, and without this every test run
# from the repo root wrote sessions, artifacts and projects into the developer's real
# backend/data/jarvis_memory.db.
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="jarvis-tests-"), "jarvis_memory.db")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + _TEST_DB.replace("\\", "/"))
os.environ.setdefault("MEMORY_DB_PATH", _TEST_DB)
# Same for project workspaces: WORKSPACE_PATH defaults to "./workspace", which from the repo root
# is D:\JARVIS\workspace -- and anything that builds a default VectorStoreService (ProjectIndexer
# without an explicit store, for one) created project/index folders there on every run.
os.environ.setdefault("WORKSPACE_PATH", os.path.join(os.path.dirname(_TEST_DB), "workspace"))
# Tests never spawn the embedding sidecar (a llama-server on :8003): the hashed engine is
# deterministic and needs no model. Sidecar-specific tests construct one explicitly.
os.environ.setdefault("RAG_EMBEDDING_SIDECAR_ENABLED", "false")
# Forgotten memories go to a bin file under repo-root data/; keep test runs out of the real one.
os.environ.setdefault("JARVIS_SCREENSHOT_DIR", os.path.join(os.path.dirname(_TEST_DB), "screenshots"))
os.environ.setdefault("RAG_TRACE_PATH", os.path.join(os.path.dirname(_TEST_DB), "rag_trace.jsonl"))
os.environ.setdefault("JARVIS_MEMORY_BIN_PATH", os.path.join(os.path.dirname(_TEST_DB), "memory_bin.json"))
# The in-app compare's draw is random: a test turn must never start one (tests that need it set the
# rate), and a vote never lands in the user's real preference file.
os.environ.setdefault("COMPARE_RATE", "0")
# Freeform's file tools work on the user's home folder; a test turn must never touch the real one.
os.environ.setdefault("FREEFORM_FILES_ROOT", os.path.join(os.path.dirname(_TEST_DB), "freeform-home"))
os.makedirs(os.environ["FREEFORM_FILES_ROOT"], exist_ok=True)
os.environ.setdefault("JARVIS_PREFERENCE_VOTES_PATH", os.path.join(os.path.dirname(_TEST_DB), "votes.jsonl"))
# The user's notes folder and Google sign-in: a test must never write the real tasks.md, nor find
# the real Google token and sync against the user's account.
os.environ.setdefault("JARVIS_NOTES_DIR", os.path.join(os.path.dirname(_TEST_DB), "notes"))
os.environ.setdefault("JARVIS_GOOGLE_DIR", os.path.join(os.path.dirname(_TEST_DB), "google"))

import pytest
from unittest.mock import patch
from app.main import governor


@pytest.fixture(autouse=True)
def ensure_test_healthy_governor(request):
    """
    Ensure the governor allows test HTTP requests through without getting throttled
    by background GPU/CPU spikes on the host developer machine during test runs,
    except for governor-specific unit tests in test_governor.py.
    """
    if "test_governor" in request.node.nodeid:
        yield
        return

    with patch("app.main.governor.wait_until_healthy", return_value=(True, None)), \
         patch("app.main.governor.is_throttled", return_value=(False, None)):
        yield
