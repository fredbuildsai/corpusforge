# 8. Cross-cutting concepts

## Licensing and provenance
- **Normalization** (`screen.license.normalize_license`): URLs (`creativecommons.org/licenses/by/4.0`), short codes
  (`cc-by`), and free text collapse to canonical ids (`CC-BY-4.0`, `CC0-1.0`, `public-domain`; unrecognized ->
  `other:<raw>`). Decisions compare license *families* (id without version).
- **Gate** (`evaluate_license`): `allowed` / `flagged` / `rejected` / `unknown`; `unknown` is treated as rejected.
- **Paired links**: when a duplicate record carries an allowed license and the canonical does not, the canonical adopts the
  license *and* that record's PDF/XML links, so the fetched text is always the copy the license applies to.
- **Attribution manifests**: every exported row is traceable to a DOI (else `doc_id`), license and title.
- **Release gate**: `export.licensing` recomputes from the manifests which sources are not redistributable and can
  produce a filtered public copy; `LICENSE_STATUS.json` records the verdict for release tooling.

## Resumability
State is durable at every step (document status + reason, task rows, per-batch commits). Selection is derived from state,
never from in-memory progress. Failed tasks are indistinguishable from pending ones for selection purposes, which is
exactly what makes re-running correct.

## Concurrency and SQLite
Every SQLite connection gets `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=30000` (`register_sqlite_pragmas`, shared
with `llmrouter-free`). The runner never keeps a transaction open across a router call: the router writes its ledger row
through an independent session on the same file, and two overlapping write transactions from one process deadlock rather
than queue. In-memory SQLite is avoided for shared engines (a single shared connection interleaves threads' transactions
and silently loses writes - see the llmrouter-free docs).

## Politeness and safety toward the network
One `PoliteClient` for all sources: GET-only, per-host minimum interval, exponential backoff and `Retry-After`,
identifying User-Agent with optional `mailto:`, and detection of bot-management challenges (`cf-mitigated: challenge`,
known body signatures) which raises rather than retries.

## Structured LLM output
Owned by `llmrouter-free`, applied by `runner.call_and_persist`: the stage's batch-shaped Pydantic schema becomes a strict
`json_schema` `response_format` (schema-constrained decoding where the provider supports it) and a `json_validator`
(fence-tolerant parse + schema check + a guard against non-empty objects sharing no field with the schema). A reply that
fails validation is an `invalid_output` attempt - retried or failed over, never cached. Temperature defaults to 0 for
extraction. Model quirks (Nemotron's hidden reasoning eating the output budget, Ollama thinking mode) are configuration in
`llm_routes.yaml` (`extra_body`, `min_max_tokens`), not code. Missing excerpts in an otherwise valid reply are detected via
`chunk_index` and retried once.

## Logging
Standard `logging` everywhere. `configure_logging(roots=...)` attaches one `DBLogHandler` (all levels, into `logs.db`) and
a console handler (WARNING+) to each logger tree; it is idempotent. Structured context travels as
`extra={"context": {...}}`. Migrations re-enable loggers a host's Alembic `fileConfig` may have silenced.

## Configuration
YAML for per-project domain settings; `CF_*` environment for machine settings; a replaceable `Settings` object for embedding
(`set_settings`). Relative paths anchor to the project directory, not the shell's cwd at call time.

## Persistence and migrations
Portable types only. Alembic scripts live in `corpusforge/migrations`, tracked in `corpusforge_alembic_version`, driven by
`migrate()`; `stamp()` adopts existing databases. A test asserts the migrated schema equals the models column-for-column.

## Testing
Hermetic: `respx` for HTTP, a scripted fake `completion_fn` for the LLM, temp-file SQLite. Docs are tested too.
