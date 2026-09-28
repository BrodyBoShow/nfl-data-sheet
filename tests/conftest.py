import pytest

import pipeline.core.base
import pipeline.orchestration.auditor


def _no_live_database(*args, **kwargs):
    raise RuntimeError(
        "a test tried to open a live database connection; tests use fixtures and fakes only"
    )


@pytest.fixture(autouse=True)
def _block_live_database(monkeypatch):
    """Every job's run() opens its connection through base._execute, and the auditor
    through its own import. A test whose guard fails (e.g. a CLI flag check that should
    have returned early) would otherwise run the real job against the database in .env.
    That happened once, 2026-09-28, during P7 step 5's break demonstrations. Found by
    breaking run.py's --delete check: main(["efficiency", "--delete"]) ran Efficiency live.
    """
    monkeypatch.setattr(pipeline.core.base, "get_connection", _no_live_database)
    monkeypatch.setattr(pipeline.orchestration.auditor, "get_connection", _no_live_database)
