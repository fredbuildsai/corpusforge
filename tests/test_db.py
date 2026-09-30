from sqlalchemy import select

from corpusforge.db.session import get_session
from corpusforge.models import Chunk, Document, GenTask


def test_document_chunk_and_task_roundtrip(engine):
    with get_session(engine) as s:
        doc = Document(
            doc_id="chemrxiv:abc", source="chemrxiv", external_id="abc", title="NMC811 cracking",
            norm_title="nmc811 cracking", license="CC-BY-4.0", topic_tags=["cathode", "NMC811"],
        )
        doc.chunks.append(
            Chunk(chunk_id="chemrxiv:abc#s3.2-c04", order=4, tokens=612, section_path=["3", "3.2"],
                  text="Above 4.2 V vs Li/Li+, NMC811 undergoes the H2-H3 phase transition.")
        )
        s.add(doc)
        s.add(GenTask(task_type="extract", key="extract:chemrxiv:abc#s3.2-c04", payload={"n": 1}))

    with get_session(engine) as s:
        loaded = s.scalars(select(Document)).one()
        assert loaded.topic_tags == ["cathode", "NMC811"]
        assert loaded.chunks[0].section_path == ["3", "3.2"]
        assert loaded.status == "discovered" and loaded.blacklisted is False
        task = s.scalars(select(GenTask)).one()
        assert task.status == "pending" and task.attempts == 0 and task.payload == {"n": 1}
