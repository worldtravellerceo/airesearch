import os

import pytest

os.environ.setdefault("GH_PAT", "test-token")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from airadar.db import repo as db  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "airadar.db"


@pytest.fixture
def conn(db_path):
    """A fresh, schema-applied database per test.

    SQLite needs no server, so these tests always run — there is no "skipped
    because no database was reachable" hole for a regression to hide in.
    """
    with db.connect(db_path) as connection:
        db.apply_schema(connection)
        yield connection


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """Replace the client's sleep with a recorder so backoff is testable instantly."""
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    return calls, fake_sleep
