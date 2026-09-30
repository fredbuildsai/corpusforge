# 1. Introduction and goals

## Purpose
`corpusforge` turns open-access scientific literature into a **licensed, chunked, LLM-annotated training corpus**
for any research domain. It owns the mechanics of the pipeline - discovery, license and relevance screening,
full-text retrieval, parsing and chunking, a resumable LLM task queue, train/eval splitting, and provenance-carrying
export - and owns *nothing* about a particular field. A field is supplied as configuration (keywords) plus, for
structured extraction, a few small Python objects (prompt, schema, row builder).

It exists because `batterygemma` (a lithium-ion battery expert model) proved that the pipeline is reusable: the
second domain should be a configuration exercise, not a fork.

## Requirements overview

| # | Requirement | Where it is met |
|---|---|---|
| R1 | Only material whose license permits the intended use enters the corpus, and this is provable per training row. | [Licensing](08-crosscutting-concepts.md#licensing-and-provenance), `screen.license`, `export.corpus`, `export.licensing` |
| R2 | A multi-day, rate-limited, interruptible run never loses or repeats work. | [Resumability](08-crosscutting-concepts.md#resumability), `gen_tasks`, `runner` |
| R3 | The same code path handles 1 chunk or N chunks per LLM call. | [ADR-3](09-architecture-decisions.md), `runner.run_batch` |
| R4 | Domain knowledge lives in the host project, never in this package. | [ADR-1](09-architecture-decisions.md), settings injection, own-base tables |
| R5 | Be a good citizen toward public APIs and publishers. | `sources.base.PoliteClient`, bot-challenge detection (never bypassed) |
| R6 | Train/eval leakage is structurally impossible. | Split by paper, `verify.split` |
| R7 | Runs on a laptop with SQLite and moves to PostgreSQL without a rewrite. | Portable column types |

## Quality goals (top 3)
1. **Correctness of provenance** - a wrong license or attribution is worse than a missing paper.
2. **Resumability** - every long operation can be killed and re-run.
3. **Domain neutrality** - a new domain needs no change to this package.

## Stakeholders

| Stakeholder | Expectation |
|---|---|
| Domain project author (e.g. `batterygemma`) | A stable Python API and CLI to build on; no battery-specific code inside. |
| Someone starting a new domain | `corpusforge init`, edit one YAML, run. |
| Downstream dataset consumers | Every row traceable to a DOI and license. |
| API providers (OpenAlex, Crossref, Europe PMC, arXiv, CORE, Unpaywall) | Polite, identified traffic that backs off. |
