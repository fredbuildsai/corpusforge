"""Corpus-level export: continued-pretraining (CPT) text, plus the shared building blocks every export uses.

Splits are by paper (`Document.split`, set by `verify.split`), so every row inherits its source document's
split - a row is never assigned its own independent split, which is what would let one paper's content leak
across train/eval. Blacklisted documents are excluded from every export, retroactively.

Every exported row is traceable: `AttributionManifest` writes, next to each JSONL file, a
`<name>_<split>.attribution.json` mapping the source document (DOI, or `doc_id` when it has none) to its
license, title and the row indices derived from it. That is what makes a redistributed dataset carry per-record
CC-BY-style attribution rather than one blanket credit in a README.

Host projects build their own row types (SFT, DPO, ...) from the same helpers - `write_jsonl`,
`AttributionManifest`, `row_images`, `get_doc` - and inject domain-specific CPT text through
`export_cpt(extra_rows=...)`.
"""

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from corpusforge.images import resolve_chunk_image_paths
from corpusforge.models import Chunk, Document
from corpusforge.parse.chunk import strip_overlap_prefix

logger = logging.getLogger(__name__)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def get_doc(session: Session, doc_id: str) -> Document | None:
    return session.get(Document, doc_id)


def doc_split(session: Session, doc_id: str) -> str | None:
    doc = get_doc(session, doc_id)
    return doc.split if doc else None


class AttributionManifest:
    """Tracks, per source document, which row indices of an export file were derived from it.

    Rows are traced back to their source document's DOI (falling back to `doc_id` when a
    document has none, e.g. some theses/OSTI records) with its license, so a redistributed
    export carries per-record provenance sufficient for CC-BY-style attribution - required at
    the redistribution level, not just satisfied by a single blanket credit in a README.
    """

    def __init__(self) -> None:
        self._by_split: dict[str, dict[str, dict[str, Any]]] = {}

    def record(self, split: str, doc: Document, row_index: int) -> None:
        key = doc.doi or doc.doc_id
        entry = self._by_split.setdefault(split, {}).setdefault(
            key, {"doc_id": doc.doc_id, "license": doc.license, "title": doc.title, "chunks": []}
        )
        entry["chunks"].append(row_index)

    def record_external(self, split: str, key: str, *, license: str, title: str, row_index: int) -> None:
        """Like `record`, for a row with no source `Document` - e.g. an ontology-derived CPT row (see
        `export_cpt`'s `include_ontology`), attributed to the ontology itself rather than a paper."""
        entry = self._by_split.setdefault(split, {}).setdefault(
            key, {"doc_id": key, "license": license, "title": title, "chunks": []}
        )
        entry["chunks"].append(row_index)

    def write(self, output_dir: Path, name: str) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        for split, manifest in self._by_split.items():
            path = output_dir / f"{name}_{split}.attribution.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
            paths[split] = path
        return paths


def row_images(session: Session, chunk_ids: list[str]) -> list[str]:
    """Downloaded figure paths for every chunk `chunk_ids` points to, deduped, in chunk order. This is how a
    generated example's `images` field links back to the actual figures its source chunk(s) referenced -
    see images.py for how a chunk's caption filenames resolve to real downloaded files."""
    paths: list[str] = []
    seen: set[str] = set()
    for chunk_id in chunk_ids:
        chunk = session.get(Chunk, chunk_id)
        if not chunk or not chunk.images:
            continue
        for path in resolve_chunk_image_paths(session, chunk.doc_id, chunk.images):
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def cpt_text(chunk: Chunk) -> str:
    """Drop the leading overlap-with-previous-chunk sentences `parse.chunk` prepends for
    retrieval-style use (so a fact split across a chunk boundary isn't lost when only one chunk is
    retrieved). CPT has no such boundary - the model trains on the literal token sequence, so that
    overlap is pure redundancy: the same tokens get extra gradient updates for no benefit."""
    return strip_overlap_prefix(chunk.text, chunk.overlap_prev_tokens)


def pack_cpt_chunks(chunks: list[Chunk], pack_tokens: int) -> list[list[Chunk]]:
    """Group chunks by `doc_id` only (no section boundary at all - see below), then greedily pack
    each document's chunks, in document order (`order` is a global, monotonically increasing sequence
    number for the whole document, not reset per section or subsection - confirmed against real
    chunk_ids, e.g. `#s00-c00, #s00-c01, #s01-c00, ...`), into batches whose combined (overlap-
    stripped) length stays under `pack_tokens`.

    An earlier version of this function stopped at the top-level section boundary (matching
    `parse.chunk`'s own rule, added there so chunks never cross a chapter and mix unrelated content
    for a *retrieval* unit). For CPT that concern doesn't apply the same way: there is no retrieval
    boundary being violated, only a plain-text sequence being trained on, and a real corpus
    measurement showed the section-respecting version left 94.2% of sections short of even a 2000-
    token budget - most documents simply don't have one section that long. Dropping the boundary
    entirely (grouping by doc_id only) was measured, on this same real corpus, to raise the average
    packed row from 897 to 1,632 tokens with only 3% of rows still under 500 tokens, so it's kept
    document-scoped (never crossing into a different paper) but no longer section-scoped within one.
    Cross-document row order in the output doesn't matter: each packed row becomes an independent,
    shuffled training example either way."""
    by_doc: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        by_doc.setdefault(chunk.doc_id, []).append(chunk)

    groups: list[list[Chunk]] = []
    for doc_chunks in by_doc.values():
        doc_chunks.sort(key=lambda c: c.order)
        current: list[Chunk] = []
        current_tokens = 0
        for chunk in doc_chunks:
            piece_tokens = chunk.tokens - chunk.overlap_prev_tokens
            if current and current_tokens + piece_tokens > pack_tokens:
                groups.append(current)
                current, current_tokens = [], 0
            current.append(chunk)
            current_tokens += piece_tokens
        if current:
            groups.append(current)
    return groups


@dataclass(frozen=True)
class ExtraCptRow:
    """A CPT text row that has no source `Document` - e.g. a definition sentence generated from an ontology.

    It is attributed to `attribution_key` (with `license` and `title`) instead of a paper, and always goes to
    the `train` split: there is no paper-level split to inherit, and holding out a handful of definition
    sentences would not test anything an eval split is meant to test.
    """

    text: str
    attribution_key: str
    license: str
    title: str


def export_cpt(session: Session, output_dir: Path, *, include_images: bool = False,
               pack_tokens: int | None = None, extra_rows: Iterable[ExtraCptRow] = ()) -> dict[str, int]:
    """Write `cpt_train.jsonl` / `cpt_eval.jsonl` (+ attribution manifests) and return the row count per split.

    `pack_tokens`: if set, concatenate consecutive chunks of a document up to this many tokens per row instead
    of one row per chunk - see `pack_cpt_chunks`. Leave unset for exports where per-chunk granularity matters.

    `extra_rows`: rows without a source document, appended to the train split after all paper-derived rows.
    """
    counts: dict[str, list[dict[str, Any]]] = {"train": [], "eval": []}
    attribution = AttributionManifest()
    eligible = []
    for chunk in session.scalars(select(Chunk)).all():
        doc = get_doc(session, chunk.doc_id)
        if doc and doc.split and not doc.blacklisted and chunk.text.strip():
            eligible.append(chunk)
    groups = pack_cpt_chunks(eligible, pack_tokens) if pack_tokens else [[c] for c in eligible]

    for group in groups:
        doc = get_doc(session, group[0].doc_id)
        row: dict[str, Any] = {"text": "\n\n".join(cpt_text(c) for c in group)}
        if include_images:
            images: list[str] = []
            seen: set[str] = set()
            for c in group:
                for path in resolve_chunk_image_paths(session, c.doc_id, c.images):
                    if path not in seen:
                        seen.add(path)
                        images.append(path)
            if images:
                row["images"] = images
        attribution.record(doc.split, doc, len(counts[doc.split]))
        counts[doc.split].append(row)

    for extra in extra_rows:
        attribution.record_external(
            "train", extra.attribution_key, license=extra.license, title=extra.title,
            row_index=len(counts["train"]),
        )
        counts["train"].append({"text": extra.text})

    attribution.write(output_dir, "cpt")
    return {split: write_jsonl(output_dir / f"cpt_{split}.jsonl", rows) for split, rows in counts.items()}
