"""airadar command line interface."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import typer
from rich.console import Console
from rich.table import Table

from airadar import collect as collect_mod
from airadar.config import GITHUB_API_VERSION, get_settings
from airadar.db import repo as db
from airadar.gh.client import GitHubClient, GitHubError
from airadar.gh.metrics import (
    HISTORY_PATH,
    WEEKS_PER_PAGE,
    StarHistoryFormatError,
    parse_star_history,
    total_from_history,
)

app = typer.Typer(help="GitHub AI ecosystem radar", no_args_is_help=True)
console = Console()


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )


@app.command()
def doctor(
    repo: str = typer.Option(
        "huggingface/transformers", "--repo", help="Repository to probe, as owner/name"
    ),
) -> None:
    """Verify credentials, rate limits and the stargazers/history endpoint.

    This is the one check that has to pass before anything else in the pipeline is
    worth running: the whole trend model is built on that endpoint.
    """
    asyncio.run(_doctor(repo))


async def _doctor(repo: str) -> None:
    settings = get_settings()
    if not settings.github_token:
        console.print("[red]GH_PAT is not set.[/red] Export a personal access token first.")
        raise typer.Exit(1)

    async with GitHubClient() as client:
        table = Table(title=f"airadar doctor — API version {GITHUB_API_VERSION}")
        table.add_column("check")
        table.add_column("result")

        limits = await client.get("/rate_limit")
        core = limits.data["resources"]["core"]
        search = limits.data["resources"]["search"]
        table.add_row("auth", "[green]ok[/green]")
        table.add_row("core quota", f"{core['remaining']}/{core['limit']} per hour")
        table.add_row("search quota", f"{search['remaining']}/{search['limit']} per minute")
        if core["limit"] < 5000:
            table.add_row(
                "[yellow]warning[/yellow]",
                f"core limit is {core['limit']}/h — this looks like an Actions "
                "GITHUB_TOKEN (1,000/h per repo). Use a PAT instead.",
            )

        meta = await client.get(f"/repos/{repo}")
        created = dt.datetime.fromisoformat(meta.data["created_at"].replace("Z", "+00:00")).date()
        table.add_row("repo", f"{repo} — {meta.data['stargazers_count']:,} stars")
        table.add_row("created", f"{created} ({(dt.date.today() - created).days:,} days ago)")

        # The load-bearing check.
        try:
            history = await client.get(
                HISTORY_PATH.format(full_name=repo),
                params={"per_page": WEEKS_PER_PAGE, "page": 1},
            )
            days = parse_star_history(history.data)
        except (GitHubError, StarHistoryFormatError) as exc:
            table.add_row("stargazers/history", f"[red]FAILED[/red] — {exc}")
            console.print(table)
            console.print(
                "\n[red]The star-history endpoint did not behave as documented.[/red]\n"
                "Fallback: run `airadar collect` daily and build the trend series from "
                "our own snapshots. Forward-looking trends still work; historical "
                "backfill does not."
            )
            raise typer.Exit(2) from exc

        if not days:
            table.add_row("stargazers/history", "[yellow]empty[/yellow] — no star activity")
        else:
            table.add_row(
                "stargazers/history",
                f"[green]ok[/green] — {len(days)} days, "
                f"{days[0].date} → {days[-1].date}, "
                f"{total_from_history(days):,} stars in window",
            )
            table.add_row("etag", history.etag or "(none)")

        table.add_row("api calls spent", str(client.counters.calls))
        console.print(table)


@app.command("init-db")
def init_db() -> None:
    """Create or update the database schema. Safe to re-run."""
    settings = _require_database()
    with db.connect(settings.database_url) as conn:
        db.apply_schema(conn)
    console.print("[green]schema applied[/green]")


@app.command()
def collect(
    limit: int = typer.Option(None, "--limit", help="Stop after this many repos"),
    date: str = typer.Option(None, "--date", help="Override the collection date (YYYY-MM-DD)"),
) -> None:
    """Refresh metadata and recent star history for every repo that is due.

    Two requests per repo. One page of history covers ~210 days, which is enough
    for every bounded window (7/14/28/90d) without a backfill.
    """
    asyncio.run(_run_collect(limit, _parse_date(date)))


async def _run_collect(limit: int | None, date: dt.date | None) -> None:
    settings = _require_database()
    with db.connect(settings.database_url) as conn:
        run_id = db.start_run(conn, "collect")
        async with GitHubClient() as client:
            try:
                report = await collect_mod.collect(conn, client, today=date, limit=limit)
            except Exception as exc:
                db.finish_run(conn, run_id, ok=False, notes=str(exc)[:500], **_spend(client))
                raise
            db.finish_run(conn, run_id, ok=True, notes=report.summary(), **_spend(client))
        console.print(f"[green]collect[/green]: {report.summary()}")
        _print_spend(client)


@app.command()
def backfill(
    repo: str = typer.Option(None, "--repo", help="Backfill a single owner/name"),
    limit: int = typer.Option(None, "--limit", help="Stop after this many repos"),
) -> None:
    """Walk star history back to each repo's creation date.

    Required before a repo can appear on the Fresh Power board: fresh_power
    integrates a repo's whole life, and one page recovers only ~55% of it.
    """
    asyncio.run(_run_backfill(repo, limit))


async def _run_backfill(repo: str | None, limit: int | None) -> None:
    settings = _require_database()
    with db.connect(settings.database_url) as conn:
        run_id = db.start_run(conn, "backfill")
        async with GitHubClient() as client:
            try:
                written = await collect_mod.backfill(conn, client, full_name=repo, limit=limit)
            except Exception as exc:
                db.finish_run(conn, run_id, ok=False, notes=str(exc)[:500], **_spend(client))
                raise
            db.finish_run(conn, run_id, ok=True, notes=f"{written} day-rows", **_spend(client))
        console.print(f"[green]backfill[/green]: {written:,} day-rows written")
        _print_spend(client)


@app.command()
def score(
    date: str = typer.Option(None, "--date", help="Scoring date (YYYY-MM-DD), default today"),
) -> None:
    """Recompute every metric and rebuild all boards. No API calls."""
    settings = _require_database()
    with db.connect(settings.database_url) as conn:
        run_id = db.start_run(conn, "score")
        try:
            scored, rows = collect_mod.score(conn, today=_parse_date(date))
        except Exception as exc:
            db.finish_run(conn, run_id, ok=False, notes=str(exc)[:500])
            raise
        db.finish_run(conn, run_id, ok=True, notes=f"{scored} repos, {rows} board rows")
    console.print(f"[green]score[/green]: {scored:,} repos scored, {rows:,} board rows")


@app.command()
def board(
    name: str = typer.Argument("fresh", help="popular | momentum | breakout | fresh"),
    category: str = typer.Option("_all", "--category"),
    limit: int = typer.Option(20, "--limit"),
    date: str = typer.Option(None, "--date"),
) -> None:
    """Print a leaderboard in the terminal."""
    settings = _require_database()
    on = _parse_date(date) or dt.date.today()
    with db.connect(settings.database_url) as conn:
        rows = db.load_leaderboard(conn, date=on, board=name, category=category, limit=limit)
        previous = db.previous_board_ranks(conn, before=on, board=name, category=category)

    if not rows:
        console.print(f"[yellow]no {name} board for {on}[/yellow] — run `airadar score` first")
        raise typer.Exit(1)

    table = Table(title=f"{name} — {category} — {on}")
    for column in ("#", "Δ7g", "repo", "kategori", "stars", "14g hız", "ivme", "fresh"):
        table.add_column(column)

    for row in rows:
        old = previous.get(row["repo_id"])
        delta = "[dim]yeni[/dim]" if old is None else _delta(old - row["rank"])
        table.add_row(
            str(row["rank"]),
            delta,
            row["full_name"] + (" 🔥" if row["breakout"] else ""),
            row["category"] or "-",
            f"{row['stars']:,}",
            f"{row['velocity_14d'] or 0:,.0f}/g",
            f"{row['acceleration'] or 0:.1f}x",
            f"{row['fresh_power'] or 0:,.0f}",
        )
    console.print(table)


def _delta(value: int) -> str:
    if value > 0:
        return f"[green]+{value}[/green]"
    if value < 0:
        return f"[red]{value}[/red]"
    return "[dim]0[/dim]"


def _parse_date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _require_database():
    settings = get_settings()
    if not settings.database_url:
        console.print("[red]DATABASE_URL is not set.[/red]")
        raise typer.Exit(1)
    return settings


def _spend(client: GitHubClient) -> dict:
    return {"api_calls": client.counters.calls, "api_304s": client.counters.not_modified}


def _print_spend(client: GitHubClient) -> None:
    counters = client.counters
    console.print(
        f"[dim]API: {counters.calls:,} istek, {counters.not_modified:,} 304 (bedava), "
        f"{counters.retries} retry, {counters.seconds_waiting:.0f}s bekleme[/dim]"
    )


if __name__ == "__main__":
    app()
