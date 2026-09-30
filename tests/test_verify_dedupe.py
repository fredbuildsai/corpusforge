from sqlalchemy import JSON, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from corpusforge.db.session import get_session
from corpusforge.verify.dedupe import find_duplicate_ids, mark_duplicates


def test_find_duplicate_ids_flags_near_identical_text_within_a_bucket():
    items = [
        ("a", "mechanism|cathode", "Why does capacity fade above 4.2 V in NMC811?"),
        ("b", "mechanism|cathode", "Why does capacity fade above 4.2V in NMC811 cathodes?"),  # near-duplicate of a
        ("c", "mechanism|cathode", "Why does silicon anode capacity fade during cycling?"),  # distinct
    ]
    duplicates = find_duplicate_ids(items, threshold=0.6)
    assert duplicates == {"b"}  # "a" is kept (first seen), "b" is flagged, "c" is distinct enough


def test_find_duplicate_ids_does_not_compare_across_buckets():
    items = [
        ("a", "mechanism|cathode", "Why does capacity fade above 4.2 V?"),
        ("b", "trade_off|cathode", "Why does capacity fade above 4.2 V?"),  # identical text, different bucket
    ]
    assert find_duplicate_ids(items, threshold=0.9) == set()


def test_find_duplicate_ids_handles_empty_input():
    assert find_duplicate_ids([]) == set()


class _Base(DeclarativeBase):
    pass


class Row(_Base):
    """Stand-in for a host project's annotated-record table: `mark_duplicates` only needs `id`, `status`,
    `reject_reason` and whatever columns the host buckets/compares on."""

    __tablename__ = "rows"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="accepted")
    reject_reason: Mapped[str | None] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16), default="mechanism")
    part: Mapped[str] = mapped_column(String(16), default="cathode")
    turns: Mapped[list] = mapped_column(JSON, default=list)  # text lives inside JSON, hence text_fn


def add_row(s, row_id, question, status="accepted"):
    s.add(Row(id=row_id, status=status, turns=[{"role": "user", "content": question}]))


def test_mark_duplicates_rejects_near_duplicate_rows(engine):
    _Base.metadata.create_all(engine)
    with get_session(engine) as s:
        add_row(s, "r1", "Why does capacity fade above 4.2 V in NMC811?")
        add_row(s, "r2", "Why does capacity fade above 4.2V in NMC811 cathodes?")  # near-dup of r1
        add_row(s, "r3", "How does FEC additive affect silicon anode SEI?")
        add_row(s, "r4", "Why does capacity fade above 4.2 V in NMC811?", status="rejected")  # not "accepted"

    with get_session(engine) as s:
        n = mark_duplicates(s, Row, bucket_columns=("kind", "part"),
                            text_fn=lambda row: row.turns[0]["content"], threshold=0.6)
    assert n == 1

    with get_session(engine) as s:
        assert s.get(Row, "r1").status == "accepted"
        assert s.get(Row, "r2").status == "rejected" and s.get(Row, "r2").reject_reason == "near_duplicate"
        assert s.get(Row, "r3").status == "accepted"
        assert s.get(Row, "r4").status == "rejected" and s.get(Row, "r4").reject_reason is None  # untouched
