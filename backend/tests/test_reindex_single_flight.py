"""Every chat request builds its own orchestrator and so its own HybridRetriever. The "one rebuild
per project per process" guard has to span them: simultaneous requests that all see a stale index
must not each start a rebuild that wipes and re-fills the same project."""
import threading
import time

from app.rag import retriever as retriever_mod
from app.rag.retriever import HybridRetriever


class _Parts:
    """Just enough for a retriever that never embeds, searches or reranks."""
    semantic = False
    identity = "test"


def _make():
    return HybridRetriever(embedding_service=_Parts(), vector_store=object(), keyword_store=object(), reranker=object())


def _wait(pred, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_a_rebuild_is_single_flight_across_retriever_instances(monkeypatch):
    import app.rag.indexer as indexer_mod

    calls, started, release = [], threading.Event(), threading.Event()

    class FakeIndexer:
        def __init__(self, **_):
            pass

        def index_project(self, project_id):
            calls.append(project_id)
            started.set()
            release.wait(5)

    monkeypatch.setattr(indexer_mod, "WorkspaceIndexer", FakeIndexer)
    monkeypatch.setattr(retriever_mod, "_REINDEXING", set())

    a, b, c = _make(), _make(), _make()
    a._reindex_in_background("p1")
    assert started.wait(2)
    b._reindex_in_background("p1")  # a different request, a different retriever, the same project
    c._reindex_in_background("p1")
    time.sleep(0.1)
    assert calls == ["p1"], f"expected one rebuild, got {calls}"

    a._reindex_in_background("p2")  # another project is independent
    assert _wait(lambda: calls.count("p2") == 1)

    release.set()
    assert _wait(lambda: not retriever_mod._REINDEXING)
    b._reindex_in_background("p1")  # once finished, a later stale index can rebuild again
    assert _wait(lambda: calls.count("p1") == 2)


def test_a_failed_rebuild_releases_the_project(monkeypatch):
    import app.rag.indexer as indexer_mod

    class Boom:
        def __init__(self, **_):
            pass

        def index_project(self, project_id):
            raise RuntimeError("embedding sidecar down")

    monkeypatch.setattr(indexer_mod, "WorkspaceIndexer", Boom)
    monkeypatch.setattr(retriever_mod, "_REINDEXING", set())
    _make()._reindex_in_background("p9")
    assert _wait(lambda: not retriever_mod._REINDEXING)
