"""The generic batched-chunk runner, exercised with a toy stage (`Note` rows) and a scripted fake LLM."""

import json
from types import SimpleNamespace

import pytest
from llmrouter_free import LLMRouter
from pydantic import BaseModel
from sqlalchemy import JSON, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from corpusforge.db.session import get_session
from corpusforge.models import Chunk, Document, GenTask
from corpusforge.runner import (
    ChunkTaskSpec,
    diversify_by_doc,
    load_chunk_ids_filter,
    pending_chunk_ids,
    run_backlog,
    run_batch,
)

CONFIG = {
    "deployments": [{"name": "gen", "model": "p/gen", "api_key_env": "KEY_A", "family": "fam1"}],
    "routes": {"extract": ["gen"]},
    "cooldown": {"rate_limit_seconds": 0, "daily_quota_seconds": 0, "error_seconds": 0},
    "max_attempts_per_call": 1, "max_attempts_per_deployment": 1,
}


class _Base(DeclarativeBase):
    pass


class Note(_Base):
    __tablename__ = "notes"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    text: Mapped[str] = mapped_column(String(200))
    tags: Mapped[list] = mapped_column(JSON, default=list)


class ChunkOut(BaseModel):
    chunk_index: int
    notes: list[str]


class BatchOut(BaseModel):
    results: list[ChunkOut]


def persist(session, chunk, chunk_result, model):
    from sqlalchemy import delete

    session.execute(delete(Note).where(Note.id.like(f"{chunk.chunk_id}#note%")))
    session.add_all([Note(id=f"{chunk.chunk_id}#note{i}", text=t, tags=[model]) for i, t in enumerate(chunk_result.notes)])
    return {"notes": len(chunk_result.notes)}


def build_messages(texts):
    body = "\n".join(f"Excerpt {i}: {t}" for i, t in enumerate(texts))
    return [{"role": "system", "content": "Take notes."}, {"role": "user", "content": body}]


SPEC = ChunkTaskSpec(
    task_type="take_notes", build_messages=build_messages, response_schema=BatchOut, persist_result=persist,
    output_tokens_per_chunk=100,
)


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("KEY_A", "x")


@pytest.fixture
def engine(engine):
    _Base.metadata.create_all(engine)
    return engine


def seed(engine, n_docs=1, chunks_per_doc=3, **doc_kwargs):
    with get_session(engine) as s:
        for d in range(n_docs):
            doc_id = f"doc{d}"
            s.add(Document(doc_id=doc_id, source="t", external_id=str(d), title="t", norm_title="t",
                           status=doc_kwargs.get("status", "chunked"), blacklisted=doc_kwargs.get("blacklisted", False)))
            s.flush()
            for c in range(chunks_per_doc):
                s.add(Chunk(chunk_id=f"{doc_id}#c{c}", doc_id=doc_id, order=c, tokens=5, text=f"text {doc_id} {c}"))


def all_chunk_ids(engine):
    with get_session(engine) as s:
        return list(s.scalars(select(Chunk.chunk_id).order_by(Chunk.chunk_id)))


def make_router(engine, respond):
    """`respond(call_number, kwargs)` returns the model's raw text or raises."""
    calls = []

    def completion(**kw):
        calls.append(kw)
        text = respond(len(calls), kw)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        )

    return LLMRouter(CONFIG, engine=engine, completion_fn=completion), calls


def answer_all(call_number, kw):
    """A well-behaved model: one result per excerpt in the prompt, in order."""
    n = kw["messages"][1]["content"].count("Excerpt ")
    return json.dumps({"results": [{"chunk_index": i, "notes": [f"note-{i}"]} for i in range(n)]})


def test_single_chunk_and_many_chunks_take_the_same_path(engine):
    seed(engine, chunks_per_doc=3)
    router, calls = make_router(engine, answer_all)

    one = run_batch(engine, router, SPEC, ["doc0#c0"])
    many = run_batch(engine, router, SPEC, ["doc0#c1", "doc0#c2"])

    assert one == {"doc0#c0": "done"} and many == {"doc0#c1": "done", "doc0#c2": "done"}
    assert len(calls) == 2  # one router call per batch, however many chunks it holds
    for kw in calls:  # both used the batch-shaped prompt ("Excerpt 0:") and the same output-budget rule
        assert "Excerpt 0:" in kw["messages"][1]["content"]
    assert calls[0]["max_tokens"] == 100 and calls[1]["max_tokens"] == 200
    with get_session(engine) as s:
        assert {n.id for n in s.scalars(select(Note))} == {"doc0#c0#note0", "doc0#c1#note0", "doc0#c2#note0"}
        tasks = {t.key: t for t in s.scalars(select(GenTask))}
    assert tasks["take_notes:doc0#c0"].status == "done" and tasks["take_notes:doc0#c0"].payload == {"notes": 1}
    assert tasks["take_notes:doc0#c1"].attempts == 1


def test_done_chunks_are_skipped_unless_forced(engine):
    seed(engine, chunks_per_doc=1)
    router, calls = make_router(engine, answer_all)

    assert run_batch(engine, router, SPEC, ["doc0#c0"]) == {"doc0#c0": "done"}
    assert run_batch(engine, router, SPEC, ["doc0#c0"]) == {"doc0#c0": "skipped"}
    assert len(calls) == 1
    assert run_batch(engine, router, SPEC, ["doc0#c0"], force=True) == {"doc0#c0": "done"}
    assert len(calls) == 2  # force bypasses the response cache too, not just the task check


def test_persisting_again_replaces_earlier_rows_for_the_chunk(engine):
    seed(engine, chunks_per_doc=1)
    router, _ = make_router(engine, lambda n, kw: json.dumps(
        {"results": [{"chunk_index": 0, "notes": ["a", "b"] if n == 1 else ["only"]}]}))

    run_batch(engine, router, SPEC, ["doc0#c0"])
    run_batch(engine, router, SPEC, ["doc0#c0"], force=True)

    with get_session(engine) as s:
        assert [n.text for n in s.scalars(select(Note).order_by(Note.id))] == ["only"]


def test_a_chunk_the_model_drops_is_retried_once_then_done(engine):
    seed(engine, chunks_per_doc=2)

    def drops_second_first_time(n, kw):
        if n == 1:
            return json.dumps({"results": [{"chunk_index": 0, "notes": ["x"]}]})  # chunk 1 omitted
        return answer_all(n, kw)

    router, calls = make_router(engine, drops_second_first_time)
    assert run_batch(engine, router, SPEC, ["doc0#c0", "doc0#c1"]) == {"doc0#c0": "done", "doc0#c1": "done"}
    assert len(calls) == 2
    assert calls[1]["messages"][1]["content"].count("Excerpt ") == 1  # the retry carried only the missing chunk


def test_a_chunk_dropped_twice_is_failed_not_looped(engine):
    seed(engine, chunks_per_doc=2)

    def never_answers_c1(n, kw):
        if n == 1:
            return json.dumps({"results": [{"chunk_index": 0, "notes": ["x"]}]})  # c1 omitted
        return json.dumps({"results": []})  # the single-chunk retry is ignored as well

    router, calls = make_router(engine, never_answers_c1)
    outcome = run_batch(engine, router, SPEC, ["doc0#c0", "doc0#c1"])

    assert outcome == {"doc0#c0": "done", "doc0#c1": "failed"}
    assert len(calls) == 2  # exactly one retry, not a loop


def test_out_of_range_chunk_index_is_ignored(engine):
    seed(engine, chunks_per_doc=1)
    router, _ = make_router(engine, lambda n, kw: json.dumps(
        {"results": [{"chunk_index": 7, "notes": ["ghost"]}, {"chunk_index": 0, "notes": ["real"]}]}))
    assert run_batch(engine, router, SPEC, ["doc0#c0"]) == {"doc0#c0": "done"}
    with get_session(engine) as s:
        assert [n.text for n in s.scalars(select(Note))] == ["real"]


def test_all_deployments_exhausted_marks_every_chunk_failed_and_resumable(engine):
    seed(engine, chunks_per_doc=2)

    def down(n, kw):
        raise RuntimeError("503 service unavailable")

    router, _ = make_router(engine, down)
    outcome = run_batch(engine, router, SPEC, ["doc0#c0", "doc0#c1"])
    assert outcome == {"doc0#c0": "failed", "doc0#c1": "failed"}
    with get_session(engine) as s:
        tasks = list(s.scalars(select(GenTask)))
    assert {t.status for t in tasks} == {"failed"} and all(t.last_error for t in tasks)

    # once the provider recovers, the same command completes them
    router2, _ = make_router(engine, answer_all)
    assert run_batch(engine, router2, SPEC, ["doc0#c0", "doc0#c1"]) == {"doc0#c0": "done", "doc0#c1": "done"}


# --- backlog -----------------------------------------------------------------------------------------


def test_pending_chunk_ids_excludes_blacklisted_unparsed_and_done(engine):
    seed(engine, n_docs=1, chunks_per_doc=2)
    with get_session(engine) as s:
        s.add(Document(doc_id="bad", source="t", external_id="b", title="t", norm_title="t", status="chunked",
                       blacklisted=True))
        s.add(Document(doc_id="raw", source="t", external_id="r", title="t", norm_title="t", status="fetched"))
        s.flush()
        s.add(Chunk(chunk_id="bad#c0", doc_id="bad", order=0, tokens=5, text="t"))
        s.add(Chunk(chunk_id="raw#c0", doc_id="raw", order=0, tokens=5, text="t"))
        s.add(GenTask(task_type="take_notes", key="take_notes:doc0#c0", status="done"))

    assert pending_chunk_ids(engine, "take_notes", limit=10) == ["doc0#c1"]
    assert pending_chunk_ids(engine, "take_notes", limit=10, force=True) == ["doc0#c0", "doc0#c1"]
    assert pending_chunk_ids(engine, "take_notes", limit=10, force=True, restrict_to={"doc0#c1"}) == ["doc0#c1"]
    assert pending_chunk_ids(engine, "take_notes", limit=1, force=True) == ["doc0#c0"]


def test_backlog_processes_everything_in_batches(engine):
    seed(engine, n_docs=2, chunks_per_doc=3)
    router, calls = make_router(engine, answer_all)

    result = run_backlog(engine, router, SPEC, limit=100, batch_size=4)

    assert result.pending == 6 and result.outcomes["done"] == 6 and not result.stopped
    assert len(calls) == 2  # ceil(6 / 4) batches
    assert pending_chunk_ids(engine, "take_notes", limit=100) == []


def test_backlog_limit_and_resume(engine):
    seed(engine, n_docs=1, chunks_per_doc=5)
    router, _ = make_router(engine, answer_all)

    first = run_backlog(engine, router, SPEC, limit=2, batch_size=2)
    second = run_backlog(engine, router, SPEC, limit=100, batch_size=2)

    assert first.outcomes["done"] == 2
    assert second.pending == 3 and second.outcomes["done"] == 3  # only what was left


def test_backlog_with_concurrency_gives_the_same_result(engine):
    seed(engine, n_docs=4, chunks_per_doc=4)
    router, calls = make_router(engine, answer_all)

    result = run_backlog(engine, router, SPEC, limit=100, batch_size=2, concurrency=4)

    assert result.outcomes["done"] == 16 and len(calls) == 8
    with get_session(engine) as s:
        assert len(list(s.scalars(select(Note)))) == 16


def test_backlog_backs_off_then_recovers_when_a_batch_fully_fails(engine):
    seed(engine, chunks_per_doc=2)
    slept = []

    def down_once(n, kw):
        if n == 1:
            raise RuntimeError("503 service unavailable")
        return answer_all(n, kw)

    router, _ = make_router(engine, down_once)
    result = run_backlog(engine, router, SPEC, limit=10, batch_size=2, retry_wait_seconds=300, sleep=slept.append,
                         clock=_fake_clock(slept))

    assert result.outcomes["done"] == 2 and not result.stopped
    assert slept and slept[0] >= 0.1  # it waited for the backoff to elapse instead of hammering the route


def test_backlog_stops_after_too_many_consecutive_fully_failed_batches(engine):
    seed(engine, chunks_per_doc=2)
    slept = []

    def down(n, kw):
        raise RuntimeError("503 service unavailable")

    router, calls = make_router(engine, down)
    result = run_backlog(engine, router, SPEC, limit=10, batch_size=2, retry_wait_seconds=1,
                         max_consecutive_failures=3, sleep=slept.append, clock=_fake_clock(slept))

    assert result.stopped
    assert result.outcomes["done"] == 0
    assert len(calls) == 3  # one router call per fully-failed attempt; no inner retry when the route is exhausted
    with get_session(engine) as s:  # nothing lost: tasks are failed (pending for the next run), none done
        assert {t.status for t in s.scalars(select(GenTask))} == {"failed"}
    assert len(pending_chunk_ids(engine, "take_notes", limit=10)) == 2


def _fake_clock(slept):
    """A clock that advances by whatever the code 'slept', so backoff logic runs without real waiting."""
    def clock():
        return 1000.0 + sum(slept)
    return clock


# --- diversification ---------------------------------------------------------------------------------


def test_round_robins_across_documents_preserving_each_docs_order():
    chunk_ids = [
        "big:doc#c0", "big:doc#c1", "big:doc#c2", "big:doc#c3",
        "small:doc#c0",
        "medium:doc#c0", "medium:doc#c1",
    ]
    # first pass takes one from each doc (in first-seen order), then continues round-robin
    assert diversify_by_doc(chunk_ids) == [
        "big:doc#c0", "small:doc#c0", "medium:doc#c0",
        "big:doc#c1", "medium:doc#c1",
        "big:doc#c2",
        "big:doc#c3",
    ]


def test_a_single_huge_document_cannot_starve_a_bounded_limit():
    huge = [f"huge:doc#c{i}" for i in range(700)]
    others = [f"paper{i}:doc#c0" for i in range(50)]
    doc_ids = {c.split("#", 1)[0] for c in diversify_by_doc(huge + others)[:100]}
    assert len(doc_ids) == 51  # every other document got at least one slot within the first 100


def test_empty_and_single_doc_inputs():
    assert diversify_by_doc([]) == []
    assert diversify_by_doc(["a:d#c0", "a:d#c1"]) == ["a:d#c0", "a:d#c1"]


def test_chunk_ids_filter_file(tmp_path):
    f = tmp_path / "ids.txt"
    f.write_text("a#c0\n\n  b#c1  \n")
    assert load_chunk_ids_filter(f) == {"a#c0", "b#c1"}
    assert load_chunk_ids_filter(None) is None
