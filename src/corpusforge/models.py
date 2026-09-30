"""Core database schema: the corpus itself (documents, files, chunks) plus the task queue and release ledger.

Uses only portable SQLAlchemy types so the same models run on SQLite and PostgreSQL. Domain-specific tables
(extracted facts, generated Q&A, ...) belong to the host project, on its OWN declarative base: they refer to
these rows by plain string ids (`doc_id`, `chunk_id`) rather than foreign keys, so the two schemas can be
migrated independently.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[str] = mapped_column(String(128), primary_key=True)  # "<source>:<external_id>"
    source: Mapped[str] = mapped_column(String(32), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(Text)
    norm_title: Mapped[str] = mapped_column(Text, index=True)
    authors: Mapped[list[Any]] = mapped_column(default=list)
    year: Mapped[int | None] = mapped_column(Integer)
    venue: Mapped[str | None] = mapped_column(Text)
    abstract: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text)
    xml_url: Mapped[str | None] = mapped_column(Text)
    license: Mapped[str | None] = mapped_column(String(64), index=True)
    license_evidence: Mapped[str | None] = mapped_column(Text)
    license_flagged: Mapped[bool] = mapped_column(Boolean, default=False)
    relevance: Mapped[float | None] = mapped_column(Float)
    topic_tags: Mapped[list[Any]] = mapped_column(default=list)
    status: Mapped[str] = mapped_column(String(16), default="discovered", index=True)
    status_reason: Mapped[str | None] = mapped_column(Text)
    split: Mapped[str | None] = mapped_column(String(8), index=True)
    duplicate_of: Mapped[str | None] = mapped_column(String(128))
    blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    blacklist_reason: Mapped[str | None] = mapped_column(Text)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    files: Mapped[list["File"]] = relationship(back_populates="document", cascade="all, delete-orphan")
    chunks: Mapped[list["Chunk"]] = relationship(back_populates="document", cascade="all, delete-orphan")


class File(Base):
    __tablename__ = "files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.doc_id"), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # pdf | xml | tex
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    document: Mapped[Document] = relationship(back_populates="files")


class Chunk(Base):
    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(String(200), primary_key=True)  # "<doc_id>#s<section>-cNN"
    doc_id: Mapped[str] = mapped_column(ForeignKey("documents.doc_id"), index=True)
    section_path: Mapped[list[Any]] = mapped_column(default=list)
    section_type: Mapped[str | None] = mapped_column(String(32))  # intro/methods/results/discussion/...
    order: Mapped[int] = mapped_column(Integer)
    tokens: Mapped[int] = mapped_column(Integer)
    overlap_prev_tokens: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    captions: Mapped[list[Any]] = mapped_column(default=list)
    images: Mapped[list[Any]] = mapped_column(default=list)  # figure filenames from this chunk's captions -
                                                              # see images.py for how these resolve to actual
                                                              # downloaded files under data/images/
    quality: Mapped[dict[str, Any]] = mapped_column(default=dict)
    purpose: Mapped[str] = mapped_column(String(8), default="sft")  # sft | cpt

    document: Mapped[Document] = relationship(back_populates="chunks")


# --- Operations ----------------------------------------------------------------------------------


class GenTask(Base):
    __tablename__ = "gen_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(40), index=True)
    key: Mapped[str] = mapped_column(String(200), unique=True)  # idempotency key, e.g. "qa:<chunk_id>"
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/running/done/failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Release(Base):
    __tablename__ = "releases"

    version: Mapped[str] = mapped_column(String(32), primary_key=True)
    filters: Mapped[dict[str, Any]] = mapped_column(default=dict)
    counts: Mapped[dict[str, Any]] = mapped_column(default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
