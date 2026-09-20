import os

import pytest

os.environ.setdefault("GH_PAT", "test-token")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/airadar_test")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")


@pytest.fixture
def recorded_sleeps(monkeypatch):
    """Replace the client's sleep with a recorder so backoff is testable instantly."""
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    return calls, fake_sleep
