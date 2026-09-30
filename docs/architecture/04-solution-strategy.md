# 4. Solution strategy

| Goal | Strategy |
|---|---|
| Domain neutrality | *Generic core + mechanisms.* The package provides pipeline stages and a generic runner; the host injects prompts, schemas and row builders via `ChunkTaskSpec`, and CPT extras via `ExtraCptRow`. Domain tables live on the host's own SQLAlchemy base and refer to corpus rows by string id (no foreign keys), so schemas migrate independently. |
| Provenance | Normalize licenses at ingestion, gate before fetching, keep `license_evidence`, and write a per-row attribution manifest at export. |
| Resumability | Persist state after every unit of work: document `status`/`status_reason`, one `gen_tasks` row per LLM job, commits per batch of records. Selection queries are derived from state, so "resume" is just "run again". |
| One code path | `run_batch` is the only annotation path; a single chunk is a batch of one, using the same batch-shaped prompt and schema. |
| Reliability against flaky providers | Delegate to `llmrouter-free`; on top, the runner backs off when a whole batch fails and stops (without loss) after too many consecutive failures. |
| Safe concurrency on SQLite | Never hold a transaction across an LLM call; short sessions for bookkeeping and persistence; WAL + 30 s busy timeout on every connection. |
| Polite and lawful | A single `PoliteClient` used by every source: per-host interval, backoff, identified User-Agent, bot-challenge detection that aborts rather than evades. |
| Portability and distribution | Portable SQL types; Alembic scripts and templates inside the package; own version table; configuration through a replaceable `Settings` object. |
| Trustworthy docs | README examples and the arc42 links/cited tests are executed by the test suite. |

Technology choices: SQLAlchemy 2 + Alembic (portable persistence and migrations), httpx (HTTP), Pydantic (settings and LLM
output schemas), Typer + Rich (CLI), Docling and lxml (parsing, optional), `tokenizers` (real token counts, local cache),
datasketch (MinHash, optional).
