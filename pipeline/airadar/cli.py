"""airadar command line interface."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from airadar import classify_run, export_site
from airadar import collect as collect_mod
from airadar import discover as discover_mod
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
    with db.connect(settings.db_path) as conn:
        db.apply_schema(conn)
    console.print("[green]schema applied[/green]")


@app.command()
def discover(
    channels: str = typer.Option(
        "all",
        "--channels",
        help="Comma-separated: topics,keywords,snowball,awesome,ecosystems,huggingface,resolve",
    ),
    resolve_limit: int = typer.Option(
        None, "--resolve-limit", help="Cap how many pending names to look up this run"
    ),
    max_queries: int = typer.Option(
        None, "--max-queries", help="Stop after this many search requests"
    ),
) -> None:
    """Sweep every discovery channel and persist what it finds.

    Channels are individually selectable so a long sweep can be split across
    several jobs — a full topic sweep is thousands of search requests at 30 per
    minute, which is more than one Actions run should hold.
    """
    asyncio.run(_run_discover(channels, resolve_limit, max_queries))


_CHANNELS = ("topics", "keywords", "snowball", "awesome", "ecosystems", "huggingface", "resolve")


async def _run_discover(
    channels: str, resolve_limit: int | None, max_queries: int | None = None
) -> None:
    settings = _require_database()
    selected = _parse_channels(channels)

    with db.connect(settings.db_path) as conn:
        run_id = db.start_run(conn, "discover")
        async with GitHubClient() as client:
            try:
                report = await discover_mod.discover(
                    conn,
                    client,
                    topics=selected["topics"],
                    keywords=selected["keywords"],
                    snowball=selected["snowball"],
                    awesome=selected["awesome"],
                    ecosystems=selected["ecosystems"],
                    huggingface=selected["huggingface"],
                    resolve=selected["resolve"],
                    resolve_limit=resolve_limit,
                    query_budget=max_queries,
                )
            except Exception as exc:
                db.finish_run(conn, run_id, ok=False, notes=str(exc)[:500], **_spend(client))
                raise
            db.finish_run(conn, run_id, ok=True, notes=report.summary()[:500], **_spend(client))

        console.print(f"[green]discover[/green]: {report.summary()}")
        console.print(f"[dim]search: {report.search.summary()}[/dim]")
        _print_spend(client)
        _print_overview(discover_mod.discovery_overview(conn))


def _parse_channels(value: str) -> dict[str, bool]:
    if value.strip().lower() == "all":
        return dict.fromkeys(_CHANNELS, True)
    wanted = {c.strip().lower() for c in value.split(",") if c.strip()}
    unknown = wanted - set(_CHANNELS)
    if unknown:
        console.print(
            f"[red]unknown channel(s): {', '.join(sorted(unknown))}[/red]\n"
            f"known: {', '.join(_CHANNELS)}"
        )
        raise typer.Exit(1)
    return {channel: channel in wanted for channel in _CHANNELS}


def _print_overview(overview: dict) -> None:
    table = Table(title=f"izlenen evren — {overview['as_of']}")
    table.add_column("kanal")
    table.add_column("repo", justify="right")
    for channel, count in overview["by_channel"].items():
        table.add_row(channel, f"{count:,}")
    table.add_row("[bold]toplam[/bold]", f"[bold]{overview['total']:,}[/bold]")
    if overview["pending"]:
        table.add_row("[dim]bekleyen[/dim]", f"[dim]{overview['pending']:,}[/dim]")
    console.print(table)


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
    with db.connect(settings.db_path) as conn:
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
    with db.connect(settings.db_path) as conn:
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
def classify(
    limit: int = typer.Option(None, "--limit", help="Only consider this many repos"),
    max_llm: int = typer.Option(
        None, "--max-llm", help="Cap how many repos are sent to the model this run"
    ),
    no_llm: bool = typer.Option(False, "--no-llm", help="Rule engine only, spend nothing"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what a run would cost without spending anything"
    ),
    export_pending: str = typer.Option(
        None,
        "--export-pending",
        help="Write the borderline repos to a file for someone else to judge",
    ),
    import_verdicts: str = typer.Option(
        None, "--import-verdicts", help="Read judged repos back in"
    ),
) -> None:
    """Decide which repos are AI-related and what kind, rules first.

    Most repos are settled for free. Only the ambiguous band costs money, and
    results are cached against the hash of their inputs, so a weekly run pays
    only for what actually changed.
    """
    asyncio.run(_run_classify(limit, max_llm, no_llm, dry_run, export_pending, import_verdicts))


async def _run_classify(
    limit: int | None,
    max_llm: int | None,
    no_llm: bool,
    dry_run: bool,
    export_pending: str | None = None,
    import_verdicts: str | None = None,
) -> None:
    settings = _require_database()

    # Handing the borderline cases off is the free alternative to the Batch API:
    # the same judgement, made on a subscription rather than billed per token.
    if import_verdicts:
        with db.connect(settings.db_path) as conn:
            report = classify_run.import_verdicts(conn, Path(import_verdicts))
        console.print(
            f"[green]classify[/green]: {report.imported:,} karar içeri alındı, "
            f"{len(report.unmatched):,} atlandı · toplam AI: {report.ai_repos:,}"
        )
        return

    if export_pending:
        with db.connect(settings.db_path) as conn:
            async with GitHubClient() as client:
                report = await classify_run.export_pending(
                    conn, client, Path(export_pending), limit=max_llm
                )
        console.print(
            f"[green]classify[/green]: {report.exported:,} sınırdaki repo "
            f"{export_pending} dosyasına yazıldı ({report.escalated:,} toplam)"
        )
        _print_spend(client)
        return

    if not no_llm and not dry_run and not settings.anthropic_api_key:
        console.print(
            "[red]ANTHROPIC_API_KEY is not set.[/red] "
            "Use --no-llm for the free rule engine, or --dry-run to see the cost."
        )
        raise typer.Exit(1)

    with db.connect(settings.db_path) as conn:
        run_id = db.start_run(conn, "classify")
        async with GitHubClient() as client:
            try:
                report = await classify_run.classify_all(
                    conn,
                    client,
                    use_llm=not no_llm,
                    limit=limit,
                    max_llm_repos=max_llm,
                    dry_run=dry_run,
                )
            except Exception as exc:
                db.finish_run(conn, run_id, ok=False, notes=str(exc)[:500], **_spend(client))
                raise
            db.finish_run(
                conn,
                run_id,
                ok=True,
                notes=report.summary()[:500],
                llm_in_tok=report.llm_input_tokens,
                llm_out_tok=report.llm_output_tokens,
                llm_cost_usd=report.llm_cost_usd,
                **_spend(client),
            )

    table = Table(title="classify" + (" (dry run)" if dry_run else ""))
    table.add_column("")
    table.add_column("", justify="right")
    table.add_row("değerlendirilen", f"{report.considered:,}")
    table.add_row("önbellekten (değişmemiş)", f"{report.cached:,}")
    table.add_row("kuralla karara bağlanan", f"{report.settled_by_rules:,}")
    table.add_row("LLM'e giden", f"{report.escalated:,}")
    if dry_run:
        table.add_row(
            "[bold]tahmini maliyet[/bold]", f"[bold]${report.estimated_cost_usd:.2f}[/bold]"
        )
    else:
        table.add_row("LLM ile sınıflanan", f"{report.classified_by_llm:,}")
        if report.unmatched:
            table.add_row("[yellow]eşleşmeyen[/yellow]", f"{len(report.unmatched):,}")
        table.add_row("AI olarak işaretli", f"{report.ai_repos:,}")
        table.add_row(
            "[bold]gerçek maliyet[/bold]",
            f"[bold]${report.llm_cost_usd:.2f}[/bold] "
            f"[dim](tahmin ${report.estimated_cost_usd:.2f})[/dim]",
        )
    console.print(table)
    if report.unmatched and not dry_run:
        console.print(
            "[dim]Eşleşmeyen repolar bir sonraki çalıştırmada yeniden denenecek "
            "(önbelleğe yazılmadılar).[/dim]"
        )


@app.command()
def score(
    date: str = typer.Option(None, "--date", help="Scoring date (YYYY-MM-DD), default today"),
) -> None:
    """Recompute every metric and rebuild all boards. No API calls."""
    settings = _require_database()
    with db.connect(settings.db_path) as conn:
        run_id = db.start_run(conn, "score")
        try:
            scored, rows = collect_mod.score(conn, today=_parse_date(date))
        except Exception as exc:
            db.finish_run(conn, run_id, ok=False, notes=str(exc)[:500])
            raise
        db.finish_run(conn, run_id, ok=True, notes=f"{scored} repos, {rows} board rows")
    console.print(f"[green]score[/green]: {scored:,} repos scored, {rows:,} board rows")


@app.command("export-site")
def export_site_cmd(
    out: str = typer.Option("web/public/data", "--out", help="Where the site reads its JSON from"),
    date: str = typer.Option(None, "--date", help="Export this scoring date"),
) -> None:
    """Write the static JSON the dashboard is built from."""
    settings = _require_database()
    with db.connect(settings.db_path) as conn:
        report = export_site.export(conn, Path(out), date=_parse_date(date))
    console.print(f"[green]export-site[/green]: {report.summary()}")


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
    with db.connect(settings.db_path) as conn:
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
    """The database is a file, so there is nothing to configure — but a missing
    one usually means the `data` branch was not checked out, and saying so is
    more useful than an empty board."""
    settings = get_settings()
    path = Path(settings.db_path)
    if not path.exists():
        console.print(
            f"[yellow]{path} yok.[/yellow] `airadar init-db` ile oluşturun, "
            "ya da CI'da `data` dalının çekildiğinden emin olun."
        )
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
