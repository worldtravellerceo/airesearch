"""The one place this project spends money.

Apify runs a scraper on its own machines and bills per result. That is the only
way the data behind this universe is reachable at all at our budget: the G2 API
is an enterprise partnership, the Crunchbase API is Enterprise-only — Pro at $49
a month does not include it — and Dealroom is EUR 12,600 a year.

Two things follow from spending real money, and they are what this module is:

**The cap is enforced by Apify, not by us.** Every run is started with
`maxTotalChargeUsd`, so the ceiling holds even if this code has a bug, loops, or
is handed a list ten times longer than intended. A cap that lives only in our
own arithmetic is not a cap.

**Every run is written down.** `apify_run` records the actor, the item count and
what it cost, before the next run is allowed to start. A month's spend is a
query, not an estimate.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import sqlite3
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

API_ROOT = "https://api.apify.com/v2"

# Actor IDs are `username~name`. Pinned as constants because a typo here does
# not fail — it starts somebody else's actor and bills us for it.
SIMILARWEB_ACTOR = "memo23~similarweb-scraper"
CRUNCHBASE_ACTOR = "memo23~crunchbase-scraper"
G2_ACTOR = "automation_craft~g2-reviews-scraper"

# How long to let a run go before giving up on it. Apify keeps billing a run
# that is still producing results, so this is a limit on our patience, not on
# the spend — that is `maxTotalChargeUsd`.
DEFAULT_RUN_TIMEOUT_SECONDS = 3600
POLL_SECONDS = 10.0
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT", "ABORTING"}


class ApifyError(RuntimeError):
    """A run that cannot be completed or paid for."""


class BudgetExceeded(ApifyError):
    """The month's cap would be breached by this run, so it was not started."""


@dataclass
class ActorRun:
    run_id: str
    actor: str
    status: str
    cost_usd: float
    items: list[dict[str, Any]]

    @property
    def ok(self) -> bool:
        return self.status == "SUCCEEDED"


def spend_this_month(conn: sqlite3.Connection, *, today: dt.date | None = None) -> float:
    """What Apify has already been paid this calendar month."""
    today = today or dt.date.today()
    first = today.replace(day=1)
    row = conn.execute(
        "SELECT COALESCE(sum(cost_usd), 0) AS spent FROM apify_run WHERE started_at >= ?",
        (dt.datetime.combine(first, dt.time.min, tzinfo=dt.UTC),),
    ).fetchone()
    return float(row["spent"])


class ApifyClient:
    """Start an actor, wait for it, read its dataset, record the bill."""

    def __init__(
        self,
        token: str,
        *,
        monthly_cap_usd: float = 25.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        if not token:
            raise ApifyError("No Apify token. Set APIFY_TOKEN.")
        self._monthly_cap = monthly_cap_usd
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=API_ROOT,
            transport=transport,
            timeout=httpx.Timeout(120.0, connect=15.0),
            headers={"Authorization": f"Bearer {token}"},
        )

    async def __aenter__(self) -> ApifyClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def budget_for(self, conn: sqlite3.Connection, *, today: dt.date | None = None) -> float:
        """What is left of this month's cap."""
        return max(0.0, self._monthly_cap - spend_this_month(conn, today=today))

    async def run_actor(
        self,
        conn: sqlite3.Connection,
        actor: str,
        payload: dict[str, Any],
        *,
        max_charge_usd: float,
        timeout_seconds: int = DEFAULT_RUN_TIMEOUT_SECONDS,
        notes: str = "",
    ) -> ActorRun:
        """Run `actor` on `payload`, never spending more than `max_charge_usd`.

        The ledger row is written before the run starts and updated when it
        finishes, so a run that is killed mid-flight — an Actions timeout, a
        cancelled job — still leaves a trace that something was spent.
        """
        remaining = self.budget_for(conn)
        if max_charge_usd > remaining:
            raise BudgetExceeded(
                f"{actor}: asked for ${max_charge_usd:.2f} with ${remaining:.2f} left "
                f"of this month's ${self._monthly_cap:.2f} cap"
            )

        started = dt.datetime.now(dt.UTC)
        cursor = conn.execute(
            "INSERT INTO apify_run (actor, started_at, status, notes) VALUES (?, ?, ?, ?)",
            (actor, started, "STARTING", notes),
        )
        ledger_id = cursor.lastrowid
        conn.commit()

        response = await self._client.post(
            f"/acts/{actor}/runs",
            params={
                # Apify's own ceiling. This is the one that holds when our code
                # is wrong.
                "maxTotalChargeUsd": f"{max_charge_usd:.2f}",
                "timeout": timeout_seconds,
            },
            json=payload,
        )
        if response.status_code >= 300:
            conn.execute(
                "UPDATE apify_run SET status = ?, finished_at = ?, notes = ? WHERE id = ?",
                ("START-FAILED", dt.datetime.now(dt.UTC), response.text[:500], ledger_id),
            )
            conn.commit()
            raise ApifyError(
                f"{actor} would not start ({response.status_code}): {response.text[:300]}"
            )

        run = response.json()["data"]
        run_id = run["id"]
        conn.execute(
            "UPDATE apify_run SET run_id = ?, status = ? WHERE id = ?",
            (run_id, run.get("status"), ledger_id),
        )
        conn.commit()
        log.info("apify: %s started as %s, cap $%.2f", actor, run_id, max_charge_usd)

        run = await self._await_run(run_id, timeout_seconds)
        cost = float(run.get("usageTotalUsd") or 0.0)
        items: list[dict[str, Any]] = []
        if run.get("defaultDatasetId"):
            items = await self._dataset_items(run["defaultDatasetId"])

        conn.execute(
            "UPDATE apify_run SET status = ?, finished_at = ?, items = ?, cost_usd = ? "
            "WHERE id = ?",
            (run.get("status"), dt.datetime.now(dt.UTC), len(items), cost, ledger_id),
        )
        conn.commit()
        log.info(
            "apify: %s finished %s — %d items, $%.4f",
            actor,
            run.get("status"),
            len(items),
            cost,
        )
        return ActorRun(run_id, actor, run.get("status", "UNKNOWN"), cost, items)

    async def _await_run(self, run_id: str, timeout_seconds: int) -> dict[str, Any]:
        """Poll until the run reaches a terminal status.

        `waitForFinish` blocks server-side for at most 60 seconds, so a long run
        is a sequence of those rather than a busy loop.
        """
        deadline = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=timeout_seconds + 120)
        while dt.datetime.now(dt.UTC) < deadline:
            response = await self._client.get(f"/actor-runs/{run_id}", params={"waitForFinish": 60})
            response.raise_for_status()
            run = response.json()["data"]
            if run.get("status") in TERMINAL_STATUSES:
                return run
            await self._sleep(POLL_SECONDS)
        raise ApifyError(f"run {run_id} did not finish within {timeout_seconds}s")

    async def _dataset_items(self, dataset_id: str) -> list[dict[str, Any]]:
        """Read the whole dataset. Reading costs nothing; only producing does."""
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = await self._client.get(
                f"/datasets/{dataset_id}/items",
                params={"offset": offset, "limit": 1000, "clean": "true"},
            )
            response.raise_for_status()
            page = response.json()
            if not page:
                return items
            items.extend(page)
            offset += len(page)
            if len(page) < 1000:
                return items
