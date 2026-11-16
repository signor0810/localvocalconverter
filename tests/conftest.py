"""Shared test setup.

Most pipeline tests use a fake LLM that answers with a canned string ("refined") unrelated
to the input; the faithfulness guard would rightly reject those. The guard is therefore
off by default in tests and switched on with `@pytest.mark.guard`.
"""
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "guard: run with the faithfulness guard enabled")


@pytest.fixture(autouse=True)
def _guard_off_unless_marked(request, monkeypatch):
    if request.node.get_closest_marker("guard") is None:
        from local_typeless.pipeline import Pipeline

        monkeypatch.setattr(Pipeline, "_guard", lambda self, raw, out, mode, warnings=None: None)
