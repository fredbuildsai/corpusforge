# Changelog

All notable changes to this project are documented here. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [0.1.0] - unreleased

First release, extracted from [`batterygemma`](https://github.com/fredbuildsai/batterygemma).

### Added
- Discovery from OpenAlex, Crossref (ChemRxiv), arXiv (OAI-PMH) and local PDFs, with cross-source de-duplication and a
  polite HTTP client that detects (and never bypasses) bot challenges.
- License normalization and gate; deterministic keyword relevance screening.
- Full-text fetch chain: Europe PMC JATS XML -> licensed PDF -> CORE -> Unpaywall mirrors; PMC figure download.
- JATS and Docling parsers, cleaning, section-aware chunking with real-tokenizer sizes and a garble metric.
- Resumable `gen_tasks` queue and the generic batched-chunk runner (`ChunkTaskSpec`, `run_batch`, `run_backlog`).
- Failure triage (`pipeline-failures`) and `blacklist`.
- By-paper train/eval split, MinHash near-duplicate marking, evidence grounding check.
- CPT export with attribution manifests and `ExtraCptRow` hook; export license gate; domain-neutral Hugging Face model
  card (`ModelCardInfo`).
- `corpusforge` CLI (`init`, `discover`, `screen`, `fetch`, `images`, `add-local`, `parse`, `fetch-failures`,
  `pipeline-failures`, `blacklist*`, `stats`, `db init`, `logs *`) with commands registrable on a host Typer app.
- Structured logging to a queryable `logs.db`.
- In-package Alembic migrations with a private version table; `stamp()` to adopt existing databases.
- `Settings` (`CF_*`) with `set_settings()` for embedding.
- README, arc42 architecture documentation and tests that verify both.
