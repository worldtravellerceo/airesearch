"""airadar command line interface."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from airadar import audit, classify_run, export_site
from airadar import collect as collect_mod
from airadar import discover as discover_mod
from airadar.companies import apify
from airadar.companies import domains as company_domains
from airadar.companies import enrich as company_enrich
from airadar.companies import news as company_news
from airadar.companies import traffic as company_traffic_mod
from airadar.config import GITHUB_API_VERSION, get_settings
from airadar.db import company as company_db
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

        # Per lane, because the ceiling a run works against is the sum. One
        # token is 5,000 an hour; three are 15,000, and that is the difference
        # between a backfill that fits in the job's timeout and one that does not.
        lanes = await client.check_lanes()
        table.add_row("auth", f"[green]ok[/green] — {client.lanes} token(s)")
        for lane in lanes:
            if not lane["ok"]:
                table.add_row(lane["lane"], "[red]rejected (401), dropped[/red]")
                continue
            if lane.get("unknown"):
                table.add_row(lane["lane"], "[yellow]quota unreadable[/yellow]")
                continue
            table.add_row(
                lane["lane"],
                f"core {lane['core']}/{lane['core_limit']} per hour, "
                f"search {lane['search']}/{lane['search_limit']} per minute",
            )
        # The sum is only the ceiling if the lanes are separate allowances.
        # GitHub applies the limit to the account, so three tokens belonging to
        # one user are three views of one bucket — and adding them up would
        # predict a run three times faster than it can possibly be.
        shared = any(lane.get("shared_quota") for lane in lanes)
        live = [lane for lane in lanes if lane["ok"]]
        limits = [lane.get("core_limit", 0) for lane in live]
        if shared or len(live) < 2:
            table.add_row("ceiling", f"{max(limits or [0]):,} core requests per hour")
            if shared:
                table.add_row(
                    "[yellow]note[/yellow]",
                    "the tokens share one allowance — GitHub applies the rate "
                    "limit per account, not per token, so extra tokens from the "
                    "same user add nothing. Only tokens from different accounts do.",
                )
        else:
            table.add_row(
                "ceiling",
                f"{sum(limits):,} core requests per hour across {len(live)} separate allowances",
            )
        if any(lane.get("core_limit", 5000) < 5000 for lane in lanes):
            table.add_row(
                "[yellow]warning[/yellow]",
                "a lane is under 5,000/h — that looks like an Actions "
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
        help=(
            "Comma-separated: census,topics,keywords,snowball,"
            "awesome,ecosystems,huggingface,resolve"
        ),
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


_CHANNELS = (
    "census",
    "topics",
    "keywords",
    "snowball",
    "awesome",
    "ecosystems",
    "huggingface",
    "resolve",
)


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
                    census=selected["census"],
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
def coverage(
    min_stars: int = typer.Option(
        3000, "--min-stars", help="Ignore a name match below this many stars"
    ),
) -> None:
    """Check the database against a hand-written list of projects it must hold.

    Counting repos is not coverage: the first real corpus held 64,373 and was
    still missing `karpathy/nanoGPT` and `facebookresearch/faiss`. This is the
    check that would have caught it.
    """
    settings = _require_database()
    with db.connect(settings.db_path) as conn:
        report = audit.audit_coverage(conn, min_stars=min_stars)

    if report.missing:
        table = Table(title=f"eksik ({len(report.missing)})")
        table.add_column("proje")
        for name in report.missing:
            table.add_row(name)
        console.print(table)

    colour = "green" if not report.missing else "red"
    console.print(f"[{colour}]coverage[/{colour}]: {report.summary()}")
    if report.missing:
        raise typer.Exit(1)


@app.command("company-seeds")
def company_seeds(
    limit: int = typer.Option(40, "--limit", help="How many to show"),
    min_stars: int = typer.Option(
        0, "--min-stars", help="Ignore repos below this when building a seed"
    ),
) -> None:
    """List the company domains the repository corpus already points at.

    The corpus holds tens of thousands of owners with an AI repository, and
    their homepages point somewhere. That is a seed list nobody else builds the
    same way: AI companies that ship open source, for nothing.
    """
    settings = _require_database()
    with db.connect(settings.db_path) as conn:
        seeds = company_domains.seeds_from_corpus(conn, min_stars=min_stars)

    table = Table(title=f"şirket tohumları — {len(seeds):,} alan adı")
    table.add_column("yıldız", justify="right")
    table.add_column("alan adı")
    table.add_column("repo", justify="right")
    table.add_column("owner")
    for seed in seeds[:limit]:
        owners = ", ".join(sorted(seed.owners)[:2])
        table.add_row(f"{seed.stars:,}", seed.domain, str(seed.repos), owners)
    console.print(table)

    for cut in (100_000, 10_000, 1_000):
        n = sum(1 for s in seeds if s.stars >= cut)
        console.print(f"  arkasında >= {cut:,} yıldız olan: {n:,}")


@app.command("companies-sync")
def companies_sync(
    min_stars: int = typer.Option(
        None, "--min-stars", help="Ignore repos below this when building a seed"
    ),
) -> None:
    """Rebuild the company table from the repository corpus.

    Free, and the only step that has to happen before anything paid: it is what
    decides which domains are worth spending money on.
    """
    settings = _require_database()
    floor = settings.company_min_stars if min_stars is None else min_stars
    with db.connect(settings.db_path) as conn:
        seeds = company_domains.seeds_from_corpus(conn)
        stored = company_db.upsert_seeds(conn, seeds)
        payable = sum(1 for seed in seeds if seed.stars >= floor)
    console.print(
        f"[green]companies-sync[/green]: {stored:,} şirket kaydedildi, "
        f"{payable:,} tanesi >= {floor:,} yıldız ile ücretli aramaya değer "
        f"(tahmini ${company_enrich.estimate_usd(payable):.2f})"
    )


@app.command("companies-traffic")
def companies_traffic(
    limit: int = typer.Option(1000, "--limit", help="How many companies to look up"),
    min_stars: int = typer.Option(None, "--min-stars", help="Ignore companies below this"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the queue and the price, spend nothing"
    ),
) -> None:
    """Buy Similarweb traffic for the companies that have waited longest.

    This spends money. The month's cap is enforced by Apify itself, not only by
    this process, and every run is written to `apify_run` with what it cost.
    """
    settings = _require_database()
    floor = settings.company_min_stars if min_stars is None else min_stars

    with db.connect(settings.db_path) as conn:
        queue = company_db.companies_to_enrich(conn, limit=limit, min_stars=floor)
        spent = apify.spend_this_month(conn)

    console.print(
        f"kuyrukta {len(queue):,} şirket, tahmini "
        f"${company_enrich.estimate_usd(len(queue)):.2f} — bu ay şu ana dek "
        f"${spent:.2f} / ${settings.apify_monthly_cap_usd:.2f} harcandı"
    )
    if dry_run:
        for domain in queue[:20]:
            console.print(f"  {domain}")
        return

    if not settings.apify_token:
        console.print("[red]APIFY_TOKEN ayarlı değil.[/red]")
        raise typer.Exit(1)

    async def run() -> company_traffic_mod.TrafficReport:
        with db.connect(settings.db_path) as conn:
            async with apify.ApifyClient(
                settings.apify_token, monthly_cap_usd=settings.apify_monthly_cap_usd
            ) as client:
                return await company_enrich.refresh_traffic(
                    conn, client, limit=limit, min_stars=floor
                )

    report = asyncio.run(run())
    console.print(f"[green]companies-traffic[/green]: {report.summary()}")


@app.command("companies-funding")
def companies_funding(
    what: str = typer.Option(
        "rounds", "--what", help="rounds (who just raised) or profiles (totals and M&A)"
    ),
    limit: int = typer.Option(500, "--limit", help="How many rounds or companies"),
    min_stars: int = typer.Option(None, "--min-stars", help="Ignore companies below this"),
    monitor: bool = typer.Option(
        True,
        "--monitor/--no-monitor",
        help="Only rounds not returned by a previous identical run (cheaper)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the price, spend nothing"),
) -> None:
    """Announced rounds, or a company's total raised and who bought whom.

    `rounds` needs no company list: it selects by type, amount and date, so it
    finds companies we have never heard of. `profiles` looks up companies we
    already hold, and stores nothing until the profile's own website confirms
    it is the company we asked about.
    """
    settings = _require_database()
    floor = settings.company_min_stars if min_stars is None else min_stars

    with db.connect(settings.db_path) as conn:
        spent = apify.spend_this_month(conn)
        if what == "rounds":
            price = company_enrich.rounds_estimate_usd(limit)
            queued = limit
        else:
            queued = len(company_db.companies_to_match(conn, limit=limit, min_stars=floor))
            price = company_enrich.rounds_estimate_usd(queued)

    console.print(
        f"{what}: en fazla {queued:,} kayıt, tahmini ${price:.2f} — bu ay "
        f"${spent:.2f} / ${settings.apify_monthly_cap_usd:.2f}"
    )
    if dry_run:
        return
    if not settings.apify_token:
        console.print("[red]APIFY_TOKEN ayarlı değil.[/red]")
        raise typer.Exit(1)

    async def run():
        with db.connect(settings.db_path) as conn:
            async with apify.ApifyClient(
                settings.apify_token, monthly_cap_usd=settings.apify_monthly_cap_usd
            ) as client:
                if what == "rounds":
                    return await company_enrich.refresh_rounds(
                        conn, client, max_rounds=limit, monitor=monitor
                    )
                return await company_enrich.refresh_company_funding(
                    conn, client, limit=limit, min_stars=floor
                )

    report = asyncio.run(run())
    console.print(f"[green]companies-funding[/green]: {report.summary()}")


@app.command("companies-directory")
def companies_directory(
    limit: int = typer.Option(5000, "--limit", help="How many Crunchbase rows to buy"),
    query: str = typer.Option("ai", "--query", help="Substring matched on name or description"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the price, spend nothing"),
) -> None:
    """Buy Crunchbase's company list so we can stop guessing identities.

    The rows carry the company's own website, which is the only field that ties
    a Crunchbase profile to a domain we hold. Without it the slug guess was
    right 19% of the time, and every funding round came back unattributable.
    """
    settings = _require_database()
    with db.connect(settings.db_path) as conn:
        spent = apify.spend_this_month(conn)
    console.print(
        f"dizin: {limit:,} satır, tahmini "
        f"${company_enrich.rounds_estimate_usd(limit):.2f} — bu ay ${spent:.2f} / "
        f"${settings.apify_monthly_cap_usd:.2f}"
    )
    if dry_run:
        return
    if not settings.apify_token:
        console.print("[red]APIFY_TOKEN ayarlı değil.[/red]")
        raise typer.Exit(1)

    async def run() -> dict:
        with db.connect(settings.db_path) as conn:
            async with apify.ApifyClient(
                settings.apify_token, monthly_cap_usd=settings.apify_monthly_cap_usd
            ) as client:
                return await company_enrich.refresh_directory(
                    conn, client, limit=limit, query=query
                )

    report = asyncio.run(run())
    console.print(
        f"[green]companies-directory[/green]: {report['rows']:,} satır "
        f"({report['with_domain']:,} alan adıyla), {report['matched']:,} şirket eşleşti, "
        f"{report['rounds_attached']:,} tur bağlandı, ${report['cost_usd']:.2f}"
    )


@app.command("companies-valuations")
def companies_valuations(
    limit: int = typer.Option(500, "--limit", help="How many articles to read"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the price, spend nothing"),
) -> None:
    """Read press-reported valuations out of Crunchbase News headlines.

    The only place any of these sources states a valuation — and free: the site
    runs on WordPress and its REST API is public. The Apify actor sells the
    same headlines at $0.008 each.

    Every figure is stored with the article that said it, and shown as a press
    report rather than a measurement.
    """
    settings = _require_database()
    console.print(
        f"haberler: en fazla {limit:,} makale, Crunchbase News'in halka açık API'si — ücretsiz"
    )
    if dry_run:
        return

    async def run() -> dict:
        with db.connect(settings.db_path) as conn:
            return await company_news.refresh_valuations(conn, limit=limit)

    report = asyncio.run(run())
    console.print(
        f"[green]companies-valuations[/green]: {report['articles']:,} makale, "
        f"{report['valuations']:,} tanesinde rakam, {report['attached']:,} şirkete bağlandı, "
        f"${report['cost_usd']:.2f}"
    )


@app.command("companies-ratings")
def companies_ratings(
    limit: int = typer.Option(100, "--limit", help="How many companies to ask G2 about"),
    min_stars: int = typer.Option(None, "--min-stars", help="Ignore companies below this"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the price, spend nothing"),
) -> None:
    """Ask G2 what buyers think of a company's product.

    Most of this universe has no G2 page — it indexes software buyers review,
    not model weights — and that answer is stored so the same empty lookup is
    not paid for again. The run is billed per review found, which is what makes
    a broad sweep affordable despite the miss rate.
    """
    settings = _require_database()
    floor = settings.company_min_stars if min_stars is None else min_stars

    with db.connect(settings.db_path) as conn:
        queued = len(company_db.companies_to_rate(conn, limit=limit, min_stars=floor))
        spent = apify.spend_this_month(conn)

    console.print(
        f"G2: {queued:,} şirket, en kötü durumda "
        f"${company_enrich.g2_estimate_usd(queued):.2f} — bu ay ${spent:.2f} / "
        f"${settings.apify_monthly_cap_usd:.2f}"
    )
    if dry_run:
        return
    if not settings.apify_token:
        console.print("[red]APIFY_TOKEN ayarlı değil.[/red]")
        raise typer.Exit(1)

    async def run():
        with db.connect(settings.db_path) as conn:
            async with apify.ApifyClient(
                settings.apify_token, monthly_cap_usd=settings.apify_monthly_cap_usd
            ) as client:
                return await company_enrich.refresh_ratings(
                    conn, client, limit=limit, min_stars=floor
                )

    report = asyncio.run(run())
    console.print(f"[green]companies-ratings[/green]: {report.summary()}")


@app.command("readmes")
def readmes(
    limit: int = typer.Option(None, "--limit", help="How many repos this run"),
    min_stars: int = typer.Option(1000, "--min-stars", help="Ignore repos below this"),
    refetch: bool = typer.Option(False, "--refetch", help="Re-read READMEs already fetched"),
    max_minutes: float = typer.Option(
        None, "--max-minutes", help="Stop cleanly after this long, leaving the rest for next time"
    ),
) -> None:
    """Read the README of every repo the metadata could not place.

    A repo whose name, description and topics say nothing about AI is recorded
    as "not AI" — which is the one reading a zero score does not support.
    Fourteen of the forty highest-star repos created since July are in that
    state, `andrewyng/openworker` and `browser-use/jev-ultrafast` among them.
    Their READMEs say plainly what they are.
    """
    settings = _require_database()

    async def run() -> collect_mod.ReadmeReport:
        with db.connect(settings.db_path) as conn:
            async with GitHubClient() as client:
                return await collect_mod.fetch_readmes(
                    conn,
                    client,
                    limit=limit,
                    min_stars=min_stars,
                    refetch=refetch,
                    max_minutes=max_minutes,
                )

    report = asyncio.run(run())
    console.print(f"[green]readmes[/green]: {report.summary()}")


@app.command("review-queue")
def review_queue(
    out: str = typer.Option("review-queue.json", "--out", help="Where to write the slice"),
    limit: int = typer.Option(1200, "--limit", help="How many repos in this slice"),
    min_stars: int = typer.Option(1000, "--min-stars", help="Ignore repos below this"),
    verdicts: str = typer.Option("verdicts", "--verdicts", help="Directory of past verdicts"),
) -> None:
    """Export the repos the rule engine had no opinion about, biggest first.

    Not the escalation band — these scored zero, meaning no signal was found at
    all, which the engine recorded as "not AI". That is the one reading a zero
    does not support. `anomalyco/opencode` sat here at 208,847 stars.
    """
    settings = _require_database()
    with db.connect(settings.db_path) as conn:
        report = classify_run.export_review_queue(
            conn,
            Path(out),
            limit=limit,
            min_stars=min_stars,
            verdicts_dir=Path(verdicts),
        )
    console.print(
        f"[green]review-queue[/green]: {report.exported:,} repo {out} dosyasına yazıldı "
        f"({report.considered:,} aday)"
    )


@app.command("reset-history")
def reset_history_cmd(
    all_repos: bool = typer.Option(
        False, "--all", help="Reset every backfilled repo, not only the inflated ones"
    ),
) -> None:
    """Clear derived star history so the next backfill rebuilds it.

    For repairing totals that a prune inflated. The weekly buckets are one-way —
    the day rows they came from are gone — so re-fetching is the only repair.
    """
    settings = _require_database()
    with db.connect(settings.db_path) as conn:
        count = db.reset_history(conn, only_inflated=not all_repos)
    console.print(
        f"[green]reset-history[/green]: {count:,} reponun geçmişi temizlendi, "
        "backfill yeniden çıkaracak"
    )


@app.command()
def completeness() -> None:
    """Compare the corpus against GitHub's own count, bucket by bucket.

    The only check that asks something outside the pipeline. `coverage` and
    `census` are both the pipeline grading its own homework; this is ground
    truth, at one search request per star bucket.
    """
    settings = _require_database()
    asyncio.run(_run_completeness(settings))


async def _run_completeness(settings) -> None:
    with db.connect(settings.db_path) as conn:
        async with GitHubClient() as client:
            report = await audit.audit_completeness(conn, client)

    table = Table(title=f"kapsama — GitHub'a karşı (census tabanı {settings.census_min_stars})")
    table.add_column("yıldız")
    table.add_column("GitHub'da", justify="right")
    table.add_column("bizde", justify="right")
    table.add_column("eksik", justify="right")
    table.add_column("oran", justify="right")
    for bucket in report.buckets:
        high = "∞" if bucket.high >= 100_000_000 else f"{bucket.high:,}"
        table.add_row(
            f"{bucket.low:,}–{high}",
            f"{bucket.on_github:,}",
            f"{bucket.in_corpus:,}",
            f"{bucket.missing:,}",
            f"{bucket.rate:.1%}",
        )
    console.print(table)
    colour = "green" if report.rate >= 0.99 else ("yellow" if report.rate >= 0.95 else "red")
    console.print(f"[{colour}]completeness[/{colour}]: {report.summary()}")


@app.command()
def freshness() -> None:
    """Report how much of the tracked universe actually got refreshed.

    The companion to `coverage`, for the same class of failure: a collect run
    that cannot finish does not fail, it just stops part-way down the star
    order and reports the part it managed as though it were the whole.
    """
    settings = _require_database()
    with db.connect(settings.db_path) as conn:
        report = audit.audit_freshness(
            conn,
            tier1_size=settings.tier1_size,
            track_limit=settings.track_limit,
            now=dt.datetime.now(dt.UTC),
        )
    colour = (
        "green" if report.stale == 0 else ("yellow" if report.stale < report.total // 10 else "red")
    )
    console.print(f"[{colour}]freshness[/{colour}]: {report.summary()}")


@app.command()
def stats() -> None:
    """Print what the database contains. Cheap, no API calls."""
    settings = _require_database()
    with db.connect(settings.db_path) as conn:
        report = discover_mod.health(conn)

    table = Table(title="airadar — veri durumu")
    table.add_column("")
    table.add_column("", justify="right")
    table.add_row("izlenen repo", f"{report['tracked']:,}")
    table.add_row("sınıflandırılmış", f"{report['classified']:,}")
    table.add_row("AI olarak işaretli", f"{report['ai_repos']:,}")
    if report["unjudged"] > 0:
        table.add_row(
            "[yellow]kararsız (hiçbir board'da değil)[/yellow]",
            f"[yellow]{report['unjudged']:,}[/yellow]",
        )
    table.add_row("metrikleri toplanmış", f"{report['collected']:,}")
    table.add_row("tam geçmişi çıkarılmış", f"{report['backfilled']:,}")
    table.add_row("günlük veri satırı", f"{report['day_rows']:,}")
    table.add_row("haftalık veri satırı", f"{report['week_rows']:,}")
    table.add_row("bekleyen isim", f"{report['pending']:,}")
    table.add_row("taranmış topic", f"{report['topics_swept']:,}")
    table.add_row("board tarihi", str(report["boards_as_of"] or "—"))
    console.print(table)

    if report["by_channel"]:
        channels = Table(title="keşif kanalı")
        channels.add_column("kanal")
        channels.add_column("repo", justify="right")
        for channel, count_ in report["by_channel"].items():
            channels.add_row(channel, f"{count_:,}")
        console.print(channels)

    if report["by_category"]:
        categories = Table(title="kategori")
        categories.add_column("kategori")
        categories.add_column("repo", justify="right")
        for category, count_ in list(report["by_category"].items())[:20]:
            categories.add_row(category, f"{count_:,}")
        console.print(categories)

    if report["recent_runs"]:
        runs = Table(title="son çalıştırmalar")
        for column in ("komut", "ok", "API", "304", "not"):
            runs.add_column(column)
        for run in report["recent_runs"]:
            runs.add_row(
                run["command"],
                "[green]✓[/green]" if run["ok"] else "[red]✗[/red]",
                f"{run['api_calls']:,}",
                f"{run['api_304s']:,}",
                (run["notes"] or "")[:70],
            )
        console.print(runs)


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
