"""Generic batched-chunk runner: the one mechanism every LLM annotation stage runs through.

A host project describes a stage (extract facts, extract claims, ...) as a `ChunkTaskSpec` - its prompt
builder, its batch-shaped response schema and how to persist one chunk's result - and this module supplies
everything else:

- `run_batch`: bundles 1 or N chunks into ONE router call, does the `gen_tasks` bookkeeping, maps the answer
  back to chunks by `chunk_index`, and retries once any chunk the model silently dropped. There is exactly one
  code path for a single chunk and for many: a single chunk is just a batch of one, with the same batch-shaped
  prompt and schema.
- `run_backlog`: selects the pending chunks (diversified across documents, blacklisted documents excluded),
  slices them into batches, runs them through a thread pool sharing one router, backs off when every
  deployment is exhausted, stops after too many consecutive fully-failed batches without losing progress, and
  prints a timestamped progress table.

Concurrency note: no database transaction is ever held open across an LLM call. `router.complete()` writes its
own ledger row through an independent session on the same SQLite file, and on file-based SQLite two
overlapping write transactions from one process deadlock rather than queue. Task bookkeeping (before) and
result persistence (after) therefore each get their own short, fully-committed session.
"""

import logging
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from llmrouter_free import AllDeploymentsExhausted, LLMRouter, json_schema_response_format, json_validator
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from corpusforge.annotate.tasks import get_or_create_task, mark_done, mark_failed
from corpusforge.db.session import get_session
from corpusforge.models import Chunk, Document, GenTask

logger = logging.getLogger(__name__)

PersistResult = Callable[[Session, Chunk, Any, str], dict[str, Any]]


@dataclass(frozen=True)
class ChunkTaskSpec:
    """Everything domain-specific about one chunk-level LLM stage.

    - `task_type`: `gen_tasks.task_type`; each chunk's idempotency key is `"<task_type>:<chunk_id>"`.
    - `build_messages(texts)`: chat messages for a batch of excerpts (always batch-shaped, even for one text).
    - `response_schema`: pydantic model of the whole batch response. It must expose `results`, a list of
      entries each carrying `chunk_index` (the excerpt's position in the batch).
    - `persist_result(session, chunk, chunk_result, model)`: replace any earlier rows for `chunk` with rows
      built from `chunk_result` and return a small JSON-able payload summarising them (stored on the task).
    - `output_tokens_per_chunk`: output budget per excerpt; a batch of N gets `max(this, this * N)`.
    """

    task_type: str
    build_messages: Callable[[list[str]], list[dict[str, str]]]
    response_schema: type[BaseModel]
    persist_result: PersistResult
    output_tokens_per_chunk: int
    route: str = "extract"
    temperature: float | None = 0  # schema-constrained extraction is most consistent at 0


@dataclass
class BacklogResult:
    outcomes: Counter = field(default_factory=Counter)
    api_calls: int = 0
    stopped: bool = False  # True when the run gave up after too many consecutive fully-failed batches
    pending: int = 0  # chunks selected at the start of the run


def run_batch(
    engine: Engine, router: LLMRouter, spec: ChunkTaskSpec, chunk_ids: list[str], *, force: bool = False,
    _retry: bool = True, console: Console | None = None,
) -> dict[str, str]:
    """Resumable annotation of 1 or more chunks in a single LLM call.

    Returns `{chunk_id: "done" | "skipped" | "failed"}`, one entry per input id. `force=True` also bypasses the
    router's response cache - otherwise re-running the identical prompt would just replay the cached answer.
    """
    task_keys = {chunk_id: f"{spec.task_type}:{chunk_id}" for chunk_id in chunk_ids}
    pending: list[str] = []
    outcomes: dict[str, str] = {}
    with get_session(engine) as s:
        for chunk_id in chunk_ids:
            task = get_or_create_task(s, spec.task_type, task_keys[chunk_id])
            if task.status == "done" and not force:
                outcomes[chunk_id] = "skipped"
                continue
            task.attempts += 1
            pending.append(chunk_id)
    if not pending:
        return outcomes

    try:
        with get_session(engine) as s:
            chunks = [s.get(Chunk, chunk_id) for chunk_id in pending]
            payloads = _call_and_persist(s, router, spec, chunks, use_cache=not force, console=console)
    except AllDeploymentsExhausted as exc:
        with get_session(engine) as s:
            for chunk_id in pending:
                mark_failed(_task(s, task_keys[chunk_id]), str(exc))
        outcomes.update({chunk_id: "failed" for chunk_id in pending})
        return outcomes

    missing = [chunk_id for chunk_id in pending if chunk_id not in payloads]
    with get_session(engine) as s:
        for chunk_id, payload in payloads.items():
            mark_done(_task(s, task_keys[chunk_id]), payload)
            outcomes[chunk_id] = "done"

    if missing and _retry:
        # Retry, once, any chunk the LLM silently dropped from the batch response - the same mechanism applied
        # to just the missing ids. `_retry=False` on the recursive call caps it at one extra attempt per chunk,
        # so a chunk the model keeps refusing to return still terminates as "failed".
        outcomes.update(run_batch(engine, router, spec, missing, force=force, _retry=False, console=console))
    elif missing:
        outcomes.update({chunk_id: "failed" for chunk_id in missing})
    return outcomes


def _task(session: Session, key: str) -> GenTask:
    return session.scalars(select(GenTask).where(GenTask.key == key)).one()


def _call_and_persist(
    session: Session, router: LLMRouter, spec: ChunkTaskSpec, chunks: list[Chunk], *, use_cache: bool,
    console: Console | None,
) -> dict[str, dict[str, Any]]:
    """One router call for all `chunks`, persisting each returned chunk result via the spec. A `chunk_index`
    missing from the response is simply absent from the returned `{chunk_id: payload}` map."""
    result = router.complete(
        spec.route, spec.build_messages([c.text for c in chunks]),
        validate=json_validator(spec.response_schema),
        response_format=json_schema_response_format(spec.response_schema),
        max_tokens=max(spec.output_tokens_per_chunk, spec.output_tokens_per_chunk * len(chunks)),
        use_cache=use_cache, temperature=spec.temperature,
    )
    logger.info(
        f"{spec.task_type} call via {result.deployment} ({len(chunks)} chunks): "
        f"tokens_in={result.tokens_in}, tokens_out={result.tokens_out}",
        extra={"context": {"deployment": result.deployment, "chunks": len(chunks), "tokens_in": result.tokens_in,
                            "tokens_out": result.tokens_out, "cached": result.cached}},
    )
    (console or Console()).print(
        f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {spec.task_type} via {result.deployment}: "
        f"tokens_in={result.tokens_in}, tokens_out={result.tokens_out} ({len(chunks)} chunks)"
        + (" [cached]" if result.cached else "")
    )

    payloads: dict[str, dict[str, Any]] = {}
    for chunk_result in result.parsed.results:
        if not (0 <= chunk_result.chunk_index < len(chunks)):
            continue
        chunk = chunks[chunk_result.chunk_index]
        payloads[chunk.chunk_id] = spec.persist_result(session, chunk, chunk_result, result.model)
    return payloads


# --- backlog selection -----------------------------------------------------------------------------


def diversify_by_doc(chunk_ids: list[str]) -> list[str]:
    """Round-robin `chunk_ids` (in "<doc_id>#..." form) across their documents, preserving each document's own
    chunk order. Without this, a query ordered by (doc_id, order) and then truncated to `limit` lets one large
    document (e.g. a long local PDF) consume an entire run's budget by itself - confirmed live 2026-09-16: a
    single 706-chunk local textbook ate all 2000 slots of a run before any of ~500 other documents got a chunk."""
    by_doc: dict[str, deque[str]] = defaultdict(deque)
    doc_order: list[str] = []
    for chunk_id in chunk_ids:
        doc_id = chunk_id.split("#", 1)[0]
        if doc_id not in by_doc:
            doc_order.append(doc_id)
        by_doc[doc_id].append(chunk_id)

    result: list[str] = []
    while doc_order:
        for doc_id in list(doc_order):
            result.append(by_doc[doc_id].popleft())
            if not by_doc[doc_id]:
                doc_order.remove(doc_id)
    return result


def load_chunk_ids_filter(path: Path | None) -> set[str] | None:
    """Chunk ids from a one-per-line file, or None if no file was given. Used to pin every pipeline stage to the
    exact same fixed batch of chunks, instead of each stage independently diversifying/truncating its own
    pending pool - which can otherwise pick different chunks at each stage and never converge on a batch that
    has gone through every stage."""
    if path is None:
        return None
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def pending_chunk_ids(
    engine: Engine, task_type: str, limit: int, force: bool = False, restrict_to: set[str] | None = None
) -> list[str]:
    """Chunks of parsed, non-blacklisted documents that have no `done` task of `task_type`, diversified across
    documents and truncated to `limit`. `force` selects every chunk regardless of task state."""
    with get_session(engine) as s:
        done_keys = set() if force else set(
            s.scalars(select(GenTask.key).where(GenTask.task_type == task_type, GenTask.status == "done"))
        )
        chunk_ids = s.scalars(
            select(Chunk.chunk_id)
            .join(Document, Chunk.doc_id == Document.doc_id)
            .where(Document.status == "chunked", ~Document.blacklisted)
            .order_by(Chunk.doc_id, Chunk.order)
        ).all()
    if restrict_to is not None:
        chunk_ids = [c for c in chunk_ids if c in restrict_to]
    pending = [c for c in chunk_ids if force or f"{task_type}:{c}" not in done_keys]
    return diversify_by_doc(pending)[:limit]


# --- the backlog loop ------------------------------------------------------------------------------


def _ts() -> str:
    """Local wall-clock timestamp, readable against `date`/a clock while watching the terminal live."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_backlog(
    engine: Engine, router: LLMRouter, spec: ChunkTaskSpec, *, limit: int = 50, force: bool = False,
    batch_size: int = 5, restrict_to: set[str] | None = None, concurrency: int = 1, report_every: int = 100,
    retry_wait_seconds: int = 300, max_consecutive_failures: int = 10, label: str | None = None,
    console: Console | None = None, clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> BacklogResult:
    """Annotate up to `limit` pending chunks in batches of `batch_size` and report what happened.

    A batch in which EVERY chunk failed almost certainly means every deployment on the route is down or
    exhausted right now (rather than a per-chunk problem), so it is re-queued `retry_wait_seconds` later; after
    `max_consecutive_failures` such batches in a row the run stops (`result.stopped`) without losing anything -
    tasks marked `done` stay done, failed ones are pending on the next run, so re-running the same command
    resumes where it stopped. `concurrency` batches run at once from worker threads sharing one router.
    """
    console = console or Console()
    label = label or spec.task_type
    pending = pending_chunk_ids(engine, spec.task_type, limit, force, restrict_to)
    result = BacklogResult(pending=len(pending))
    total_batches = (len(pending) + batch_size - 1) // batch_size
    console.print(f"[{_ts()}] {label}: {len(pending)} pending chunks, batch_size={batch_size} "
                  f"({total_batches} API calls planned, before any retries)")

    started = clock()
    consecutive_full_failures = 0

    def render_progress() -> Table:
        elapsed = clock() - started
        table = Table(f"{label} annotation progress", "value", title=f"[{_ts()}] after {result.api_calls} API calls")
        table.add_row("elapsed", f"{elapsed / 60:.1f} min")
        table.add_row("api calls / min", f"{result.api_calls / (elapsed / 60):.2f}" if elapsed > 0 else "n/a")
        table.add_row("chunks done", str(result.outcomes["done"]))
        table.add_row("chunks skipped", str(result.outcomes["skipped"]))
        table.add_row("chunks failed", str(result.outcomes["failed"]))
        table.add_row("chunks remaining", str(len(pending) - sum(result.outcomes.values())))
        return table

    # Each work item is one batch plus the earliest time it may be (re)submitted - a fresh batch is ready
    # immediately (0.0); a fully-failed batch is pushed back by `retry_wait_seconds`.
    work: list[dict[str, Any]] = [
        {"batch": pending[i:i + batch_size], "not_before": 0.0} for i in range(0, len(pending), batch_size)
    ]

    def handle(batch: list[str], batch_outcomes: dict[str, str]) -> None:
        """Process one completed batch. Only ever called from this (the main) thread, so it needs no lock."""
        nonlocal consecutive_full_failures
        result.api_calls += 1
        if batch_outcomes and all(v == "failed" for v in batch_outcomes.values()):
            consecutive_full_failures += 1
            logger.warning(
                f"{label} batch fully failed ({consecutive_full_failures}/{max_consecutive_failures} consecutive)"
                f" - every deployment on route '{spec.route}' appears exhausted; "
                f"waiting {retry_wait_seconds}s before retrying the same batch",
                extra={"context": {"batch": batch, "consecutive_full_failures": consecutive_full_failures}},
            )
            resume_at = (datetime.now() + timedelta(seconds=retry_wait_seconds)).strftime("%Y-%m-%d %H:%M:%S")
            console.print(f"[yellow][{_ts()}] all deployments exhausted (failure {consecutive_full_failures}/"
                          f"{max_consecutive_failures}) - waiting {retry_wait_seconds}s, retrying at "
                          f"{resume_at}[/yellow]")
            if consecutive_full_failures >= max_consecutive_failures:
                console.print(render_progress())
                console.print(f"[red][{_ts()}] {max_consecutive_failures} consecutive fully-failed batches - "
                              f"stopping. Nothing already marked done was lost; re-run this exact command "
                              f"later to resume from where it stopped.[/red]")
                result.stopped = True
                return
            work.append({"batch": batch, "not_before": clock() + retry_wait_seconds})
            return
        consecutive_full_failures = 0
        for chunk_id in batch:
            result.outcomes[batch_outcomes.get(chunk_id, "failed")] += 1
        logger.info(
            f"{label} batch {result.api_calls}/{total_batches}: {dict(Counter(batch_outcomes.values()))}",
            extra={"context": {"batch": batch, "outcomes": batch_outcomes}},
        )
        if result.api_calls % report_every == 0:
            console.print(render_progress())

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        in_flight: dict[Any, list[str]] = {}
        while (work or in_flight) and not result.stopped:
            now = clock()
            while work and len(in_flight) < concurrency:
                ready = next((i for i, w in enumerate(work) if w["not_before"] <= now), None)
                if ready is None:
                    break
                item = work.pop(ready)
                in_flight[pool.submit(run_batch, engine, router, spec, item["batch"], force=force,
                                      console=console)] = item["batch"]
            if not in_flight:
                # Nothing submittable and nothing running: every remaining item is still waiting out a retry
                # backoff - sleep until the earliest one is ready rather than busy-polling.
                sleep(max(0.1, min(w["not_before"] for w in work) - now))
                continue
            done, _ = futures_wait(in_flight.keys(), timeout=5, return_when=FIRST_COMPLETED)
            for future in done:
                handle(in_flight.pop(future), future.result())
                if result.stopped:
                    break
        # `stopped` only stops submitting NEW work: batches already in flight finish naturally as the `with`
        # block exits (their task rows are written by the worker itself), rather than being cancelled mid-call
        # and leaving a task row half-attempted.
    console.print(render_progress())
    if not result.stopped:
        console.print(dict(result.outcomes))
    return result
