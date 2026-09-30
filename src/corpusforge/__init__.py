"""corpusforge: turn open-access scientific literature into licensed, chunked, LLM-annotated training corpora.

Domain-agnostic: everything topic-specific (queries, prompts, output schemas, ontologies) is supplied by the
host project; this package supplies the pipeline mechanics. The names below are the supported public API.
"""

from corpusforge.db.session import get_engine, get_session, init_db, migrate, stamp
from corpusforge.export.corpus import AttributionManifest, ExtraCptRow, export_cpt
from corpusforge.logs import configure_logging
from corpusforge.models import Base, Chunk, Document, File, GenTask, Release
from corpusforge.runner import (
    BacklogResult,
    ChunkTaskSpec,
    diversify_by_doc,
    pending_chunk_ids,
    run_backlog,
    run_batch,
)
from corpusforge.settings import Settings, get_settings, load_config, set_settings

__version__ = "0.1.0"

__all__ = [
    "AttributionManifest", "BacklogResult", "Base", "Chunk", "ChunkTaskSpec", "Document", "ExtraCptRow", "File",
    "GenTask", "Release", "Settings", "__version__", "configure_logging", "diversify_by_doc", "export_cpt",
    "get_engine", "get_session", "get_settings", "init_db", "load_config", "migrate", "pending_chunk_ids",
    "run_backlog", "run_batch", "set_settings", "stamp",
]
