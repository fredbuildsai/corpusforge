"""The `corpusforge` command line: discovery -> screening -> fetch -> parse, plus DB/log housekeeping.

The pipeline commands are plain functions registered through `register_pipeline_commands`, so a host project's
own Typer app (e.g. `bg`) can expose exactly the same commands next to its domain-specific ones instead of
re-implementing them. `logs_app` is a ready-made sub-app for the same reason.
"""

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from corpusforge import models
from corpusforge.db.session import get_session, migrate
from corpusforge.logs import LOGGER_ROOTS, configure_logging
from corpusforge.settings import TEMPLATES_DIR, get_settings, load_config

app = typer.Typer(help="corpusforge: open-access literature -> licensed, chunked training corpora",
                  no_args_is_help=True)
db_app = typer.Typer(help="Database commands", no_args_is_help=True)
logs_app = typer.Typer(help="Query the structured application log (data/logs.db)", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(logs_app, name="logs")
console = Console()
fetch_logger = logging.getLogger("corpusforge.fetch")

_PIPELINE_COMMANDS: list[tuple[str | None, Callable[..., Any]]] = []


def _command(name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a function as a shareable pipeline command (registered on any Typer app by name)."""
    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        _PIPELINE_COMMANDS.append((name, fn))
        return fn
    return decorator


def register_pipeline_commands(target: typer.Typer) -> None:
    """Add discover, screen, fetch, images, add-local, fetch-failures, pipeline-failures, blacklist,
    blacklist-list, blacklist-remove and parse to `target`."""
    for name, fn in _PIPELINE_COMMANDS:
        target.command(name)(fn)


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", help="Log at DEBUG instead of INFO")) -> None:
    """Runs before every command: attaches the structured DB log handler (see `corpusforge logs`)."""
    configure_logging(logging.DEBUG if verbose else logging.INFO, roots=LOGGER_ROOTS)


@app.command()
def init(
    force: bool = typer.Option(
        False, help="Overwrite existing configs/.env with the bundled defaults - existing "
        "customizations are lost. Off by default: init is idempotent and never touches what's already there.",
    ),
) -> None:
    """Scaffold a project directory: config YAML files, .env, data directories and the database.

    Every config in `configs/` (sources, generation, llm_routes) is generated from a bundled template if
    missing, so `corpusforge init` in an empty folder gets you running without hand-authoring any YAML. Safe
    to re-run any time: existing files are reported and left alone unless `--force` is passed.
    """
    import shutil

    from corpusforge.settings import PROJECT_ROOT

    settings = get_settings()
    settings.configs_dir.mkdir(parents=True, exist_ok=True)
    for template in sorted(TEMPLATES_DIR.glob("*.yaml")):
        target = settings.configs_dir / template.name
        existed = target.exists()
        if existed and not force:
            console.print(f"configs/{template.name}: already exists, left alone")
            continue
        shutil.copy2(template, target)
        console.print(f"configs/{template.name}: {'overwritten from template' if existed else 'created'}")

    env_example, env_file = TEMPLATES_DIR / "env.example", PROJECT_ROOT / ".env"
    env_existed = env_file.exists()
    if env_existed and not force:
        console.print(".env: already exists, left alone")
    else:
        shutil.copy2(env_example, env_file)
        console.print(f".env: {'overwritten' if env_existed else 'created'} from the bundled template - "
                      "set CF_CONTACT_EMAIL and your provider API keys.")

    for directory in (settings.data_dir, settings.raw_dir, settings.export_dir, settings.images_dir,
                      settings.data_dir / "review"):
        directory.mkdir(parents=True, exist_ok=True)
    console.print("data directories ready")

    migrate()
    console.print(f"database ready: {settings.database_url}")
    console.print("\n[green]init complete[/green] - edit configs/sources.yaml for your domain, "
                  "then `corpusforge discover --help`.")


@db_app.command("init")
def db_init() -> None:
    """Apply database migrations up to the latest revision (idempotent)."""
    migrate()
    console.print(f"Database ready: {get_settings().database_url}")


@app.command()
def stats() -> None:
    """Row counts per table and document breakdowns by status, source and license."""
    with get_session() as s:
        counts = Table("table", "rows")
        for model in (models.Document, models.File, models.Chunk, models.GenTask, models.Release):
            counts.add_row(model.__tablename__, str(s.scalar(select(func.count()).select_from(model))))
        console.print(counts)
        for column in (models.Document.status, models.Document.source, models.Document.license):
            breakdown = Table(f"documents.{column.key}", "rows")
            for value, n in s.execute(select(column, func.count()).group_by(column).order_by(func.count().desc())):
                breakdown.add_row(str(value), str(n))
            console.print(breakdown)


@_command()
def discover(
    source: str = typer.Option("all", help="openalex | chemrxiv | arxiv | all"),
    limit: int = typer.Option(500, help="Maximum records per source"),
    from_date: str = typer.Option("2020-01-01", help="arXiv OAI-PMH harvest start date (YYYY-MM-DD)"),
    batch_size: int = typer.Option(200, help="Records per database commit (progress survives interruption)"),
    fields: str = typer.Option(
        "default", "--fields", help="Fields to request from sources that support field selection "
        "(openalex, chemrxiv): 'default' for the curated minimal set, 'all' for the full unrestricted "
        "record (kept on the stored document's raw_metadata), or a comma-separated custom list. "
        "No effect on arxiv - its OAI-PMH feed has no field-selection concept, every record is fixed."
    ),
) -> None:
    """Discover candidate papers and store them with cross-source deduplication."""
    from collections import Counter
    from itertools import islice

    from corpusforge.sources.arxiv import ArxivOaiSource
    from corpusforge.sources.base import BlockedByBotProtection, PoliteClient
    from corpusforge.sources.crossref import DEFAULT_FIELDS as CHEMRXIV_DEFAULT_FIELDS
    from corpusforge.sources.crossref import CrossrefChemRxivSource
    from corpusforge.sources.openalex import DEFAULT_FIELDS as OPENALEX_DEFAULT_FIELDS
    from corpusforge.sources.openalex import OpenAlexSource
    from corpusforge.sources.store import upsert_records

    if fields == "default":
        custom_fields: list[str] | None = None
    elif fields == "all":
        custom_fields = []  # distinguished from "default" below via `use_default`
    else:
        custom_fields = [f.strip() for f in fields.split(",") if f.strip()]
    use_default = fields == "default"

    def resolve_fields(default: list[str]) -> list[str] | None:
        if use_default:
            return default
        return custom_fields or None  # "all" (empty list) or a real custom list both fall through correctly

    migrate()
    email = get_settings().contact_email
    cfg = load_config("sources")
    source_cfg = cfg["sources"]
    builders = {
        "openalex": lambda: (
            OpenAlexSource(PoliteClient(contact_email=email, min_interval=0.2), contact_email=email,
                          fields=resolve_fields(OPENALEX_DEFAULT_FIELDS)),
            source_cfg["openalex"]["terms"],
        ),
        "chemrxiv": lambda: (
            CrossrefChemRxivSource(PoliteClient(contact_email=email, min_interval=0.5), contact_email=email,
                                   fields=resolve_fields(CHEMRXIV_DEFAULT_FIELDS)),
            source_cfg["chemrxiv"]["terms"],
        ),
        "arxiv": lambda: (
            ArxivOaiSource(
                PoliteClient(contact_email=email, min_interval=3.0),  # arXiv asks for >= 3 s between requests
                sets=source_cfg["arxiv"]["oai_sets"], categories=source_cfg["arxiv"]["categories"],
                from_date=from_date,
            ),
            cfg["scope"]["must"][0],
        ),
    }
    names = list(builders) if source == "all" else [source]
    if unknown := set(names) - builders.keys():
        raise typer.BadParameter(f"Unknown source(s): {sorted(unknown)}")

    for name in names:
        if not source_cfg.get(name, {}).get("enabled", False):
            console.print(f"{name}: disabled in sources.yaml, skipped")
            continue
        adapter, terms = builders[name]()
        totals: Counter[str] = Counter()
        records = adapter.discover(terms, limit)
        try:
            while batch := list(islice(records, batch_size)):
                with get_session() as s:
                    totals += upsert_records(s, batch, allow=cfg["license_allow"], flag=cfg["license_flag"])
                console.print(f"  {name}: {sum(totals.values())} records stored so far")
        except BlockedByBotProtection as exc:
            console.print(f"[yellow]{name}: {exc}. Stopped this source (bot protection is not bypassed).[/yellow]")
        finally:
            adapter.client.close()
        console.print(f"[bold]{name}[/bold]: {dict(totals)}")


@_command()
def screen(
    rescreen: bool = typer.Option(False, help="Also re-evaluate documents already accepted or rejected"),
) -> None:
    """Apply the license gate and keyword relevance scoring to discovered documents."""
    from corpusforge.screen.screening import screen_documents

    migrate()
    with get_session() as s:
        counts = screen_documents(s, load_config("sources"), rescreen=rescreen)
    console.print(dict(counts))


@_command()
def fetch(
    limit: int = typer.Option(50, help="Maximum accepted documents to fetch in this run"),
) -> None:
    """Download full text: Europe PMC XML, else the licensed PDF, else CORE, else an Unpaywall repository mirror."""
    import os
    from collections import Counter

    from corpusforge.fetch import fetch_document
    from corpusforge.sources.base import PoliteClient

    migrate()
    settings = get_settings()
    cfg = load_config("sources")
    core_api_key = os.environ.get("CORE_API_KEY", "")
    with get_session() as s:
        doc_ids = s.scalars(
            select(models.Document.doc_id)
            .where(models.Document.status == "accepted", ~models.Document.files.any())
            .order_by(models.Document.relevance.desc())
            .limit(limit)
        ).all()
    client = PoliteClient(contact_email=settings.contact_email, min_interval=1.0)
    outcomes: Counter[str] = Counter()
    try:
        for doc_id in doc_ids:
            with get_session() as s:  # one transaction per document so progress survives interruption
                doc = s.get(models.Document, doc_id)
                outcome = fetch_document(s, doc, client, settings.raw_dir, allow=cfg["license_allow"],
                                         flag=cfg["license_flag"], contact_email=settings.contact_email,
                                         core_api_key=core_api_key)
            outcomes[outcome] += 1
            console.print(f"  {doc_id}: {outcome}")
            level = logging.INFO if outcome in ("xml", "pdf", "pdf_core", "pdf_unpaywall") else logging.WARNING
            fetch_logger.log(level, "fetch %s: %s", doc_id, outcome,
                             extra={"context": {"doc_id": doc_id, "outcome": outcome}})
    finally:
        client.close()
    console.print(dict(outcomes))


@_command()
def images(
    limit: int = typer.Option(1000, help="Maximum documents to process in this run"),
) -> None:
    """Resolve <graphic>/<inline-graphic> hrefs in stored XML full text and download the actual figures.

    Only documents fetched as JATS XML (Europe PMC route) carry resolvable figure references; images come
    from the official PMC Open Access S3 bucket (see images.py for why). Saved under data/images/<source>/
    <external_id>/ and linked to the article via a File(kind="image") row, same as the XML/PDF files.
    """
    from collections import Counter

    from sqlalchemy.orm import aliased

    from corpusforge.images import download_images_for_document
    from corpusforge.sources.base import PoliteClient

    migrate()
    settings = get_settings()
    ImageFile = aliased(models.File)
    with get_session() as s:
        rows = s.execute(
            select(models.File.doc_id, models.File.path)
            .where(models.File.kind == "xml", ~models.File.doc_id.in_(
                select(ImageFile.doc_id).where(ImageFile.kind == "image")
            ))
            .limit(limit)
        ).all()
    client = PoliteClient(contact_email=settings.contact_email, min_interval=0.2)
    counts: Counter[str] = Counter()
    try:
        for doc_id, xml_path in rows:
            with get_session() as s:
                doc = s.get(models.Document, doc_id)
                new_files = download_images_for_document(s, doc, Path(xml_path), client, settings.images_dir)
            if new_files:
                counts["documents_with_images"] += 1
                counts["images_downloaded"] += len(new_files)
                console.print(f"  {doc_id}: {len(new_files)} image(s) -> {new_files[0].path.rsplit('/', 1)[0]}")
            else:
                counts["no_images_found"] += 1
    finally:
        client.close()
    console.print(dict(counts))
    console.print(f"[bold]Images saved under {settings.images_dir}[/bold]")


@_command("add-local")
def add_local(
    paths: list[Path] = typer.Argument(..., help="Local PDF file(s) to add to the corpus"),
    license: str = typer.Option(
        "all-rights-reserved",
        help="License to record. Leave the default unless you actually hold the rights to release this "
        "file's content publicly — the default keeps it out of the open/CC-only track (the default track "
        "for dataset export) while still being usable for your own local fine-tuning.",
    ),
    doc_id: str | None = typer.Option(
        None,
        help="Attach this single file to an EXISTING document (e.g. one from `corpusforge fetch-failures`) instead of "
        "adding it as a new one - preserves that document's original DOI/license/title. Only valid with "
        "exactly one path.",
    ),
) -> None:
    """Add local PDF file(s) directly into the corpus as already-fetched documents, ready for `corpusforge parse`."""
    from corpusforge.screen.license import ALLOWED, evaluate_license
    from corpusforge.sources.local import add_local_pdf, attach_local_pdf

    migrate()
    settings = get_settings()
    cfg = load_config("sources")

    if doc_id is not None:
        if len(paths) != 1:
            console.print("[red]--doc-id only accepts a single path.[/red]")
            raise typer.Exit(1)
        try:
            with get_session() as s:
                doc = attach_local_pdf(s, paths[0], settings.raw_dir, doc_id)
        except KeyError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        console.print(f'{doc.doc_id}: "{doc.title}" — attached, status={doc.status} (license unchanged: {doc.license})')
        return

    for path in paths:
        with get_session() as s:
            doc = add_local_pdf(s, path, settings.raw_dir, license=license)
        decision = evaluate_license(doc.license, cfg["license_allow"], cfg["license_flag"])
        track = "open/CC-only" if decision == ALLOWED else "all-sources only, excluded from public export"
        console.print(f'{doc.doc_id}: "{doc.title}" — license={doc.license} ({track})')


@_command("fetch-failures")
def fetch_failures_cmd(
    output: Path = typer.Option(
        Path("data/review/fetch_failures.csv"), help="Where to write the CSV for manual review"
    ),
) -> None:
    """List accepted documents `corpusforge fetch` never got full text for, with a link and the reason, for manual retrieval.

    Every automated, legitimate channel (Europe PMC XML, the licensed PDF, an Unpaywall mirror) has already
    been tried - see the reason column. This never attempts to bypass bot protection or paywalls; it exists
    so you can retrieve a paper by hand (library access, contacting the author, etc.) and add it back with
    `corpusforge add-local --doc-id <doc_id> <file>`, which reattaches it to this same document (keeping its DOI and
    license) instead of creating a disconnected duplicate.
    """
    import csv

    from corpusforge.fetch import best_effort_url

    with get_session() as s:
        docs = s.scalars(
            select(models.Document)
            .where(models.Document.status == "accepted", ~models.Document.files.any())
            .order_by(models.Document.relevance.desc())
        ).all()

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["doc_id", "title", "doi", "year", "source", "reason", "url"])
        for doc in docs:
            writer.writerow([doc.doc_id, doc.title, doc.doi or "", doc.year or "", doc.source,
                             doc.status_reason or "not_yet_attempted", best_effort_url(doc)])

    console.print(f"{len(docs)} documents need manual retrieval -> {output}")
    for doc in docs[:20]:
        console.print(f"  {doc.doc_id}: {doc.status_reason or 'not_yet_attempted'} — {best_effort_url(doc)}")
    if len(docs) > 20:
        console.print(f"  ... and {len(docs) - 20} more, see {output}")


@_command("pipeline-failures")
def pipeline_failures_cmd(
    output: Path = typer.Option(
        Path("data/review/pipeline_failures.csv"), help="Where to write the CSV for review"
    ),
) -> None:
    """List documents with at least one failed annotate/generate/judge task, across every stage.

    `corpusforge fetch-failures` covers documents that never got full text; this covers everything downstream of
    that - a chunk that kept failing extraction, a Q&A that failed judging, etc. Most failures here are
    transient (a rate-limited or timed-out LLM call) and will simply succeed on the next re-run of the stage
    (the task queue is resumable) - this command is for spotting a document
    that fails *every* time, which usually means something about that specific chunk (garbled text, an
    unusual structure) rather than bad luck.
    """
    import csv

    from corpusforge.pipeline_failures import failed_documents

    with get_session() as s:
        results = failed_documents(s)

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["doc_id", "title", "failure_count", "stages", "sample_error"])
        for r in results:
            sample_error = r["failures"][0]["error"][:300] if r["failures"] else ""
            writer.writerow([r["doc_id"], r["title"], r["failure_count"], ",".join(r["stages"]), sample_error])

    console.print(f"{len(results)} documents have at least one failed task -> {output}")
    for r in results[:20]:
        console.print(f"  {r['doc_id']} ({r['failure_count']} failures, stages: {', '.join(r['stages'])}): {r['title'][:70]}")
    if len(results) > 20:
        console.print(f"  ... and {len(results) - 20} more, see {output}")


@_command("blacklist")
def blacklist_cmd(
    threshold: int = typer.Option(20, help="Blacklist any document with at least this many failed tasks"),
) -> None:
    """Blacklist documents that keep failing every time (see `corpusforge pipeline-failures`), so future
    annotation runs stop retrying their chunks - it does not touch or delete anything already generated.
    """
    from corpusforge.pipeline_failures import blacklist_repeat_failures

    with get_session() as s:
        newly = blacklist_repeat_failures(s, threshold=threshold)

    if not newly:
        console.print(f"no documents reached the failure threshold ({threshold}) - nothing blacklisted")
        return
    console.print(f"blacklisted {len(newly)} document(s):")
    for r in newly:
        console.print(f"  {r['doc_id']} ({r['failure_count']} failures, stages: {', '.join(r['stages'])}): {r['title'][:70]}")


@_command("blacklist-list")
def blacklist_list_cmd() -> None:
    """Show every currently blacklisted document and why."""
    with get_session() as s:
        docs = s.scalars(select(models.Document).where(models.Document.blacklisted).order_by(models.Document.doc_id)).all()
    if not docs:
        console.print("no documents are blacklisted")
        return
    for doc in docs:
        console.print(f"  {doc.doc_id}: {doc.blacklist_reason} — {doc.title[:70]}")


@_command("blacklist-remove")
def blacklist_remove_cmd(doc_id: str = typer.Argument(..., help="Document id to un-blacklist")) -> None:
    """Remove a document from the blacklist (e.g. after fixing whatever made it keep failing)."""
    with get_session() as s:
        doc = s.get(models.Document, doc_id)
        if doc is None:
            console.print(f"[red]no such document: {doc_id}[/red]")
            raise typer.Exit(1)
        was_blacklisted = doc.blacklisted
        doc.blacklisted, doc.blacklist_reason = False, None
    console.print(f"{doc_id}: removed from blacklist" if was_blacklisted else f"{doc_id}: was not blacklisted")


@_command()
def parse(
    limit: int = typer.Option(100, help="Maximum fetched documents to parse in this run"),
    reparse: bool = typer.Option(False, help="Also re-chunk documents that were already chunked"),
    from_existing_chunks: bool = typer.Option(
        False, "--from-existing-chunks",
        help="Re-chunk at the current configs/generation.yaml chunking settings using each "
        "document's EXISTING chunks as source text, instead of re-parsing its original PDF/XML "
        "(skips Docling entirely - use this after only `target_tokens`/`max_tokens` changed, not "
        "after a genuine re-extraction is needed). Implies --reparse; only affects already-chunked "
        "documents, since there's no existing chunk text to reconstruct from otherwise.",
    ),
) -> None:
    """Parse fetched full text (JATS XML, else PDF via Docling) into section-aware, quality-flagged chunks."""
    from corpusforge.parse.chunk import load_token_counter
    from corpusforge.parse.pdf_docling import build_converter, parse_pdf
    from corpusforge.parse.pipeline import parse_and_chunk, rechunk_from_existing_chunks

    migrate()
    chunking = load_config("generation")["chunking"]
    count_tokens = load_token_counter()
    statuses = ["chunked"] if from_existing_chunks else (["fetched", "chunked"] if reparse else ["fetched"])
    with get_session() as s:
        doc_ids = s.scalars(
            select(models.Document.doc_id)
            .where(models.Document.status.in_(statuses), models.Document.files.any())
            .limit(limit)
        ).all()

    converter = None

    def pdf_parser(path):  # the Docling converter loads layout models, so build it only if a PDF needs it
        nonlocal converter
        converter = converter or build_converter()
        return parse_pdf(path, converter)

    total = 0
    for doc_id in doc_ids:
        with get_session() as s:
            doc = s.get(models.Document, doc_id)
            if from_existing_chunks:
                n = rechunk_from_existing_chunks(s, doc, count_tokens, chunking)
            else:
                n = parse_and_chunk(s, doc, count_tokens, chunking, pdf_parser=pdf_parser)
        total += n
        console.print(f"  {doc_id}: {n} chunks")
    console.print(f"Parsed {len(doc_ids)} documents into {total} chunks")




def _log_table(entries) -> Table:  # noqa: ANN001 - list[LogEntry], kept loose to avoid importing the type here
    table = Table("id", "ts", "level", "component", "message", "context")
    for e in entries:
        table.add_row(str(e.id), e.ts.strftime("%Y-%m-%d %H:%M:%S"), e.level, e.component,
                      e.message[:120], str(e.context) if e.context else "")
    return table


@logs_app.command("tail")
def logs_tail(
    n: int = typer.Option(50, "-n", help="Number of most recent entries to show"),
    level: str | None = typer.Option(None, help="Filter by level, e.g. WARNING"),
    component: str | None = typer.Option(None, help="Filter by logger name substring, e.g. fetch or router"),
    follow: bool = typer.Option(False, "-f", "--follow", help="Keep polling for new entries (like tail -f)"),
) -> None:
    """Show the most recent log entries, oldest first."""
    import time as _time

    from corpusforge.logs import get_log_session, query_logs

    with get_log_session() as s:
        entries = list(reversed(query_logs(s, level=level, component=component, limit=n)))
    console.print(_log_table(entries))
    if not follow:
        return
    last_id = entries[-1].id if entries else 0
    try:
        while True:
            _time.sleep(1.0)
            with get_log_session() as s:
                new = query_logs(s, level=level, component=component, after_id=last_id, limit=1000)
            if new:
                console.print(_log_table(new))
                last_id = new[-1].id
    except KeyboardInterrupt:
        pass


@logs_app.command("query")
def logs_query(
    level: str | None = typer.Option(None, help="Filter by level, e.g. ERROR"),
    component: str | None = typer.Option(None, help="Filter by logger name substring"),
    contains: str | None = typer.Option(None, help="Filter by a substring in the message"),
    limit: int = typer.Option(100, help="Maximum rows to return"),
) -> None:
    """Search the log, most recent first."""
    from corpusforge.logs import get_log_session, query_logs

    with get_log_session() as s:
        entries = query_logs(s, level=level, component=component, contains=contains, limit=limit)
    console.print(_log_table(entries))
    console.print(f"{len(entries)} entries")


@logs_app.command("stats")
def logs_stats() -> None:
    """Row counts by level and by component."""
    from corpusforge.logs import get_log_session, log_stats

    with get_log_session() as s:
        stats = log_stats(s)
    console.print("by level:", stats["by_level"])
    console.print("by component:", stats["by_component"])


@logs_app.command("clear")
def logs_clear(
    older_than_days: int | None = typer.Option(None, help="Only delete entries older than this many days"),
) -> None:
    """Delete log entries (all of them, unless --older-than-days is given)."""
    from datetime import datetime, timedelta, timezone

    from corpusforge.logs import clear_logs, get_log_session

    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days) if older_than_days else None
    with get_log_session() as s:
        n = clear_logs(s, older_than=cutoff)
    console.print(f"deleted {n} log entries")


register_pipeline_commands(app)


if __name__ == "__main__":
    app()
