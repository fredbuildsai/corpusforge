import json

from corpusforge.db.session import get_session
from corpusforge.export.corpus import ExtraCptRow, export_cpt
from corpusforge.models import Chunk, Document, File


def read_attribution(path):
    return json.loads(path.read_text())


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def seed(engine):
    with get_session(engine) as s:
        s.add(Document(doc_id="d1", source="t", external_id="1", title="t", norm_title="t", split="train"))
        s.add(Document(doc_id="d2", source="t", external_id="2", title="t", norm_title="t", split="eval"))
        s.add(Document(doc_id="d3", source="t", external_id="3", title="t", norm_title="t", split=None))  # not split yet
        s.add(Chunk(chunk_id="d1#c0", doc_id="d1", order=0, tokens=10, text="Train chunk text.",
                    images=["fig1.jpg", "missing.jpg"]))  # missing.jpg was never downloaded
        s.add(Chunk(chunk_id="d2#c0", doc_id="d2", order=0, tokens=10, text="Eval chunk text."))
        s.add(File(doc_id="d1", kind="image", path="/data/images/t/1/fig1.jpg", sha256="x", bytes=1))
        s.add(Chunk(chunk_id="d3#c0", doc_id="d3", order=0, tokens=10, text="Unsplit chunk - must be excluded."))
        s.add(Chunk(chunk_id="d1#c1", doc_id="d1", order=1, tokens=0, text="   "))  # blank - must be excluded


def test_export_cpt_splits_by_document_and_skips_blank_and_unsplit(engine, tmp_path):
    seed(engine)
    with get_session(engine) as s:
        counts = export_cpt(s, tmp_path)
    assert counts == {"train": 1, "eval": 1}
    train_rows = read_jsonl(tmp_path / "cpt_train.jsonl")
    eval_rows = read_jsonl(tmp_path / "cpt_eval.jsonl")
    assert train_rows == [{"text": "Train chunk text."}]
    assert eval_rows == [{"text": "Eval chunk text."}]


def test_extra_rows_are_appended_to_train_only_and_attributed_to_their_own_key(engine, tmp_path):
    seed(engine)
    extras = [
        ExtraCptRow(text="Definition one.", attribution_key="ontology:materials", license="CC-BY-4.0", title="Onto"),
        ExtraCptRow(text="Definition two.", attribution_key="ontology:cells", license="CC-BY-4.0", title="Onto"),
    ]
    with get_session(engine) as s:
        counts = export_cpt(s, tmp_path, extra_rows=extras)

    assert counts == {"train": 3, "eval": 1}
    train_rows = read_jsonl(tmp_path / "cpt_train.jsonl")
    assert train_rows == [{"text": "Train chunk text."}, {"text": "Definition one."}, {"text": "Definition two."}]
    assert read_jsonl(tmp_path / "cpt_eval.jsonl") == [{"text": "Eval chunk text."}]  # never held out to eval

    manifest = read_attribution(tmp_path / "cpt_train.attribution.json")
    assert manifest["ontology:materials"] == {
        "doc_id": "ontology:materials", "license": "CC-BY-4.0", "title": "Onto", "chunks": [1]}
    assert manifest["ontology:cells"]["chunks"] == [2]


def test_extra_rows_accepts_a_generator(engine, tmp_path):
    seed(engine)
    gen = (ExtraCptRow(text=f"row {i}", attribution_key=f"k{i}", license="MIT", title="t") for i in range(2))
    with get_session(engine) as s:
        assert export_cpt(s, tmp_path, extra_rows=gen)["train"] == 3


def test_images_are_omitted_by_default(engine, tmp_path):
    """The default export carries no `images` field at all, even for a row whose chunk has a downloaded image."""
    seed(engine)
    with get_session(engine) as s:
        export_cpt(s, tmp_path)
    assert all("images" not in row for row in read_jsonl(tmp_path / "cpt_train.jsonl"))


def test_include_images_links_each_row_to_its_chunks_downloaded_figures(engine, tmp_path):
    seed(engine)
    with get_session(engine) as s:
        export_cpt(s, tmp_path, include_images=True)
    row = read_jsonl(tmp_path / "cpt_train.jsonl")[0]
    assert row["images"] == ["/data/images/t/1/fig1.jpg"]  # missing.jpg silently dropped, not downloaded


def test_export_cpt_packs_chunks_across_the_whole_document_up_to_the_token_budget(engine, tmp_path):
    """Packing is document-scoped only, not section-scoped: a real corpus measurement showed
    stopping at each section (even after merging subsections into their parent) left 94.2% of
    sections short of a 2000-token budget on their own, so packing now continues across a genuine
    section change too, as long as it's still the same document."""
    with get_session(engine) as s:
        s.add(Document(doc_id="d1", source="t", external_id="1", title="t", norm_title="t", split="train"))
        s.add(Document(doc_id="d2", source="t", external_id="2", title="t", norm_title="t", split="train"))
        s.add(Chunk(chunk_id="d1#c0", doc_id="d1", section_path=["Intro"], order=0, tokens=40,
                    overlap_prev_tokens=0, text="First part of the introduction."))
        s.add(Chunk(chunk_id="d1#c1", doc_id="d1", section_path=["Intro"], order=1, tokens=40,
                    overlap_prev_tokens=0, text="Second part of the introduction."))
        # A different section within the SAME document now packs right along with Intro.
        s.add(Chunk(chunk_id="d1#m0", doc_id="d1", section_path=["Methods"], order=2, tokens=10,
                    overlap_prev_tokens=0, text="Methods section text."))
        s.add(Chunk(chunk_id="d1#m1", doc_id="d1", section_path=["Methods"], order=3, tokens=40,
                    overlap_prev_tokens=0, text="A chunk that overflows the 100-token budget."))
        # A different DOCUMENT must never be packed in, budget or not.
        s.add(Chunk(chunk_id="d2#c0", doc_id="d2", section_path=["Intro"], order=0, tokens=10,
                    overlap_prev_tokens=0, text="Unrelated other paper."))

    with get_session(engine) as s:
        export_cpt(s, tmp_path, pack_tokens=100)

    rows = read_jsonl(tmp_path / "cpt_train.jsonl")
    texts = {r["text"] for r in rows}
    assert ("First part of the introduction.\n\nSecond part of the introduction.\n\n"
            "Methods section text.") in texts  # 40+40+10=90 <= 100, crosses the Intro->Methods boundary
    assert "A chunk that overflows the 100-token budget." in texts  # own row: 90+40 > 100
    assert "Unrelated other paper." in texts  # never merged across a document boundary
    assert len(rows) == 3


def test_export_cpt_without_pack_tokens_keeps_one_row_per_chunk(engine, tmp_path):
    """Default behavior (pack_tokens unset) is unchanged: one row per chunk."""
    with get_session(engine) as s:
        s.add(Document(doc_id="d1", source="t", external_id="1", title="t", norm_title="t", split="train"))
        s.add(Chunk(chunk_id="d1#c0", doc_id="d1", section_path=["Intro"], order=0, tokens=10, text="A."))
        s.add(Chunk(chunk_id="d1#c1", doc_id="d1", section_path=["Intro"], order=1, tokens=10, text="B."))

    with get_session(engine) as s:
        counts = export_cpt(s, tmp_path)

    assert counts == {"train": 2, "eval": 0}


def test_export_cpt_strips_the_overlap_prefix_shared_with_the_previous_chunk(engine, tmp_path):
    """parse/chunk.py prepends up to overlap_tokens worth of the previous chunk's trailing sentences,
    joined by a space, followed by a blank line, then the chunk's own body - see its module docstring.
    CPT training must use the body only: that overlap exists for retrieval-style grounding, not for a
    language-modeling objective, where it would just be the same tokens getting extra gradient updates."""
    with get_session(engine) as s:
        s.add(Document(doc_id="d1", source="t", external_id="1", title="t", norm_title="t", split="train"))
        s.add(Chunk(chunk_id="d1#c0", doc_id="d1", order=0, tokens=5, overlap_prev_tokens=0,
                    text="First chunk, no overlap."))
        s.add(Chunk(chunk_id="d1#c1", doc_id="d1", order=1, tokens=12, overlap_prev_tokens=4,
                    text="Trailing sentence from chunk zero.\n\nSecond chunk's own new body text."))

    with get_session(engine) as s:
        export_cpt(s, tmp_path)

    rows = read_jsonl(tmp_path / "cpt_train.jsonl")
    assert rows[0]["text"] == "First chunk, no overlap."  # unchanged: nothing to strip
    assert rows[1]["text"] == "Second chunk's own new body text."  # overlap prefix dropped


def test_blacklisted_documents_are_excluded_from_the_export(engine, tmp_path):
    """Blacklisting must retroactively drop already-produced content, not just stop future generation."""
    seed(engine)
    with get_session(engine) as s:
        s.get(Document, "d1").blacklisted = True
    with get_session(engine) as s:
        assert export_cpt(s, tmp_path) == {"train": 0, "eval": 1}  # d1's chunk dropped, d2's kept


def test_attribution_manifest_traces_every_row_back_to_its_source_document(engine, tmp_path):
    with get_session(engine) as s:
        s.add(Document(doc_id="d1", source="t", external_id="1", title="Paper One", norm_title="t",
                        split="train", doi="10.1/one", license="CC-BY"))
        s.add(Document(doc_id="d2", source="t", external_id="2", title="Paper Two", norm_title="t",
                        split="train", license="all-rights-reserved"))  # no DOI - falls back to doc_id
        s.add(Chunk(chunk_id="d1#c0", doc_id="d1", order=0, tokens=10, text="First chunk."))
        s.add(Chunk(chunk_id="d1#c1", doc_id="d1", order=1, tokens=10, text="Second chunk."))
        s.add(Chunk(chunk_id="d2#c0", doc_id="d2", order=0, tokens=10, text="Third chunk."))

    with get_session(engine) as s:
        export_cpt(s, tmp_path)

    manifest = read_attribution(tmp_path / "cpt_train.attribution.json")
    assert manifest["10.1/one"]["license"] == "CC-BY"
    assert manifest["10.1/one"]["title"] == "Paper One"
    assert sorted(manifest["10.1/one"]["chunks"]) == [0, 1]
    assert manifest["d2"]["license"] == "all-rights-reserved"  # keyed by doc_id: no DOI
    assert manifest["d2"]["chunks"] == [2]
