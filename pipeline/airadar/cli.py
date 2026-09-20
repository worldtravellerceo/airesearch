"""airadar command line interface."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import typer
from rich.console import Console
from rich.table import Table

from airadar.config import GITHUB_API_VERSION, get_settings
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


if __name__ == "__main__":
    app()
