# 9. Architecture decisions

## ADR-1 Generic core + mechanisms; the domain stays in the host
*Context:* the pipeline began inside a battery-specific project. *Options:* (a) fork per domain; (b) a fully generic
"do everything" framework; (c) generic core plus extension points. *Decision:* (c). The package owns stages and one runner;
the host supplies `ChunkTaskSpec`, `ExtraCptRow`, settings and its own tables. *Consequence:* no domain words in the package
(the model-card, User-Agent, tokenizer id, config templates are all parameters); a new domain needs no changes here.

## ADR-2 Two schemas, two Alembic histories, no foreign keys between them
*Decision:* corpusforge tables (`documents`, `files`, `chunks`, `gen_tasks`, `releases`) are on corpusforge's `Base` with a
private version table; host tables live on the host's `Base` and reference corpus rows by string id only.
*Consequence:* each side migrates independently in one database; an existing monolithic database is adopted with `stamp()`
(no DDL, no data movement). *Trade-off:* no referential integrity across the boundary; the host's exports resolve ids
defensively.

## ADR-3 One batched code path for 1 or N chunks
Two mechanisms (a single-chunk one and a batch one) drift apart and double the surface to test. *Decision:* the prompt and
schema are always batch-shaped; a single chunk is a batch of one; `max_tokens` scales with batch size.

## ADR-4 Skip, never bypass, access controls
Bot challenges and paywalls are recorded (`fetch:bot_protection`) and surfaced for lawful manual retrieval. This keeps the
tool defensible, and the licensed-source guarantee intact.

## ADR-5 Split by paper, derive everything from it
A hash of `doc_id` assigns train/eval; every derived row inherits the split at export time. Deterministic (no persisted
randomness), leak-free, and stable across re-runs.

## ADR-6 LLM access exclusively through llmrouter-free
Rate limits, failover, caching and quota accounting are a separate concern with their own tests and release cycle;
corpusforge depends on its public API only (`LLMRouter.complete`, `json_validator`, `json_schema_response_format`).

## ADR-7 Migrations and templates ship inside the package
A repo-root `alembic/` directory would not exist in a `pip install git+...`. Alembic is configured programmatically from
`corpusforge/migrations`.

## ADR-8 Settings via replaceable object
`get_settings()` returns whatever `set_settings()` installed, else a `CF_`-prefixed default. Embedding projects keep their
own env prefix and existing `.env` files working.

## ADR-9 Distribute from GitHub tags first
Simplest path for a young project; the README documents `git+https` installs. PyPI publication later changes nothing but
the install line.
