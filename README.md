# corpusforge

**Turn open-access scientific literature into a licensed, chunked, LLM-annotated training corpus - for any domain.**

Fine-tuning or evaluating a language model on a scientific field needs a *corpus*: thousands of papers you are
actually allowed to use, cleaned into model-sized pieces, with structured facts extracted from them, and with a
paper trail proving where every training example came from. Building that by hand is a pile of fragile scripts:
one per literature API, one per PDF quirk, one per rate limit, and a license spreadsheet nobody trusts.

`corpusforge` is that pile of scripts, done once, tested, and domain-agnostic. You describe your field in one
YAML file (keywords) and, if you want structured extraction, one small Python object per LLM task. The tool does
the rest:

```
 discover ──► screen ──► fetch ──► parse ──► annotate ──► verify ──► export
 OpenAlex     license     Europe     JATS/      your LLM    split by    JSONL + per-row
 Crossref     gate +      PMC XML,   PDF ->     stages via  paper,       attribution
 arXiv        relevance   PDF, CORE, sections   the batched dedupe       manifest, license
 local PDFs   score       Unpaywall  -> chunks  runner                   gate
```

It was developed inside [`batterygemma`](https://github.com/fredbuildsai/batterygemma) - a pipeline that
builds a lithium-ion-battery expert model from open literature - and extracted so the same machinery serves
the next domain (perovskite solar cells, protein engineering, climate science, ...) without a fork.

LLM calls go through [`llmrouter-free`](https://github.com/fredbuildsai/llmrouter-free), a quota-aware failover
router for free-tier providers, so a large annotation run survives rate limits and provider outages.

---

## Contents
- [What you get](#what-you-get) · [Install](#install) · [Quickstart](#quickstart) · [The pipeline stage by stage](#the-pipeline-stage-by-stage)
- [Configuration reference](#configuration-reference) · [CLI reference](#cli-reference) · [Python API](#python-api)
- [Writing an annotation stage](#writing-an-annotation-stage-the-batched-chunk-runner) · [Exporting and licensing](#exporting-a-dataset-and-proving-its-licenses)
- [Embedding in your own project](#embedding-in-your-own-project) · [Data model](#data-model) · [Limitations](#limitations)
- [Architecture docs](docs/architecture/README.md) · [Changelog](CHANGELOG.md)

## What you get

| Capability | What it does |
|---|---|
| **Multi-source discovery** | OpenAlex, Crossref (ChemRxiv), arXiv (OAI-PMH) and local PDFs, with cross-source de-duplication by DOI and normalized title. Polite crawling: per-host interval, back-off on 429/5xx, contact e-mail in the User-Agent. |
| **License gate first** | Every license is normalized (`CC-BY-4.0`, `CC0`, `public-domain`, `other:<raw>`), and only families you allow proceed. Unknown license = rejected, not "probably fine". |
| **Relevance screening** | Keyword scope (`must` groups AND-ed, `boost` groups scored, `comparison_only` demoted) on title + abstract. No LLM, deterministic, free. |
| **Honest full-text fetch** | Europe PMC JATS XML -> the licensed PDF -> CORE -> Unpaywall mirrors. Bot challenges (Cloudflare, ...) are **recorded and skipped, never bypassed**; a review CSV lists what needs manual retrieval. |
| **Structure-aware parsing** | JATS XML or born-digital PDF (Docling) -> sections -> chunks that never cross a section, with sentence overlap, figure captions, real-tokenizer sizes and a garble metric for extraction junk. |
| **Resumable task queue** | Every LLM job is a row in `gen_tasks` keyed by `"<type>:<chunk_id>"`. Kill the run any time; re-run and only the unfinished work happens. |
| **Batched chunk runner** | One mechanism for 1 or N chunks per LLM call: bookkeeping, retry of chunks the model dropped, back-off when every provider is down, concurrency, progress table. You supply prompts and a schema. |
| **Failure triage** | `pipeline-failures` rolls every failed task up to its document; `blacklist` stops a document that fails every time from burning quota. |
| **Provenance-carrying export** | Continued-pretraining JSONL split **by paper** (no train/eval leakage), plus a per-row attribution manifest (DOI, license, title) and a license gate that can strip anything not redistributable. |
| **Database-agnostic** | SQLAlchemy models on portable column types: SQLite today, PostgreSQL later. Migrations ship inside the package. |
| **Structured logs** | Every module logs to a `logs.db` you can query (`corpusforge logs tail -f`). |

## Install

`corpusforge` is installed from GitHub for now (PyPI later). Python 3.11 or 3.12.

```bash
# uv (recommended) - pulls llmrouter-free from GitHub automatically
uv pip install "corpusforge @ git+https://github.com/fredbuildsai/corpusforge.git@v0.1.0"

# pip
pip install "corpusforge @ git+https://github.com/fredbuildsai/corpusforge.git@v0.1.0"

# with the optional extras: PDF/JATS parsing (Docling, lxml) and MinHash de-duplication
uv pip install "corpusforge[parse,dedupe] @ git+https://github.com/fredbuildsai/corpusforge.git@v0.1.0"
```

In a `pyproject.toml` (hatchling projects also need `[tool.hatch.metadata] allow-direct-references = true`):

```toml
dependencies = [
    "corpusforge[parse] @ git+https://github.com/fredbuildsai/corpusforge.git@v0.1.0",
]
```

Extras: `parse` = Docling + lxml (needed for `corpusforge parse`; Docling is large and pulls PyTorch),
`dedupe` = datasketch, `dev` = pytest/respx/ruff. Once published on PyPI this becomes `pip install corpusforge`.

## Quickstart

```bash
mkdir my-corpus && cd my-corpus
corpusforge init                 # writes configs/, .env, data/ and the database
$EDITOR configs/sources.yaml     # describe YOUR field: scope keywords + search terms
$EDITOR .env                     # CF_CONTACT_EMAIL=you@example.org  (+ provider keys if you will annotate)

corpusforge discover --limit 500     # find candidate papers, de-duplicated across sources
corpusforge screen                   # license gate + relevance score  -> accepted / borderline / rejected
corpusforge fetch --limit 100        # full text for accepted papers
corpusforge parse --limit 100        # -> section-aware chunks
corpusforge stats                    # what do I have?
corpusforge fetch-failures           # CSV of papers to retrieve by hand
```

`init` is idempotent: it never overwrites your edited configs unless you pass `--force`.

## The pipeline stage by stage

Every document moves through a small state machine stored in `documents.status`:

```
discovered ──screen──► accepted ──fetch──► fetched ──parse──► chunked
    │                     │
    │                     └─ (no full text obtainable: stays accepted; see fetch-failures)
    ├──► borderline   (in scope but weak / comparison-only title: kept, not fetched)
    ├──► rejected     (license not allowed, or out of scope)  - status_reason says which
    └──► duplicate    (same DOI / long title as a canonical record; points at it via duplicate_of)
```

`status_reason` always records *why* (e.g. `license:rejected:CC-BY-NC-4.0`, `relevance:out_of_scope`,
`fetch:bot_protection`, `parse:docling:57_chunks`), so nothing is a silent black box.

### 1. `discover`
Pulls records from the enabled sources in `configs/sources.yaml`.
OpenAlex and Crossref take search terms (the `--limit` is split evenly across terms so every topic contributes);
arXiv is harvested by OAI-PMH set and filtered locally by category and `scope.must`. Records are committed in
batches (`--batch-size`), so an interrupted run keeps its progress. A record whose DOI or long normalized title
matches an existing document is stored as `duplicate`; if the duplicate carries an *allowed* license and the
canonical does not, the canonical adopts the duplicate's license **together with** its PDF/XML links, so the text
you later fetch is always the copy the license applies to.

### 2. `screen`
Deterministic and LLM-free. First the **license gate** (`license_allow` / `license_flag`), then a **relevance
score**: each `must` group needs at least one keyword hit (title hits weigh more than abstract hits), each `boost`
group hit adds a point and becomes a `topic_tags` entry. `comparison_only` terms in the title demote a paper to
`borderline`. Re-screen with `--rescreen` after editing the config.

### 3. `fetch`
Tries, in order: Europe PMC open-access **JATS XML** (when EPMC reports an allowed license for that copy) -> the
document's own licensed **PDF** -> **CORE**'s re-hosted copy -> **Unpaywall** repository mirrors (following the
`citation_pdf_url` meta tag that repositories publish for exactly this purpose). Files are stored under
`data/raw/<source>/` with a sha256 and a `files` row.
Bot protection is detected and **never bypassed**: the document is marked `fetch:bot_protection` and appears in
`corpusforge fetch-failures` so you can retrieve it lawfully by hand and attach it with
`corpusforge add-local --doc-id <doc_id> paper.pdf` (which keeps the original DOI and license).
`corpusforge images` additionally downloads the figures referenced by JATS XML from the PMC Open Access bucket.

### 4. `parse`
JATS XML is parsed directly; born-digital PDFs go through Docling (OCR off; page furniture, flattened citation
lists and author blocks are stripped). Sections are classified (`intro`, `methods`, `results`, ...) and boilerplate
(references, acknowledgements) dropped. Chunking rules:

- a chunk never crosses a section boundary;
- paragraphs are packed up to `target_tokens`; a paragraph longer than `max_tokens` is split by sentences;
- each chunk after the first in a section starts with trailing sentences of the previous one (`overlap_tokens`),
  recorded in `overlap_prev_tokens` so exporters can strip it again;
- sizes come from a **real tokenizer** (`CF_TOKENIZER_MODEL`, read from your local Hugging Face cache, with a
  word-count fallback);
- each chunk gets a `quality` record including `garble_ratio` (share of tokens in runs of >= 4 single characters -
  axis-label soup scores ~0.7, prose ~0) and a `garbled` flag above `max_garble_ratio`.

`--reparse` redoes documents already chunked; `--from-existing-chunks` re-chunks at new sizes from the stored chunk
text without touching Docling.

### 5. Annotate (your code, corpusforge's mechanism)
LLM extraction is domain-specific, so *you* write the prompt and schema and corpusforge runs it - see
[Writing an annotation stage](#writing-an-annotation-stage-the-batched-chunk-runner).

### 6. Verify
- `verify.split.assign_document_splits` gives each *paper* a deterministic `train`/`eval` split (a hash of its
  `doc_id`, 10% eval by default, so re-running never reshuffles) - rows inherit their paper's split, which is what
  prevents one paper's content leaking across the boundary.
- `verify.dedupe.mark_duplicates` flags near-duplicate rows in any table of yours with MinHash (`[dedupe]` extra).
- `annotate.grounding.is_grounded` is a cheap no-LLM check that a piece of "evidence" an LLM claims to quote really
  occurs in its source chunk.

### 7. Export
`export_cpt` writes `cpt_train.jsonl` / `cpt_eval.jsonl` and attribution manifests; see
[Exporting a dataset](#exporting-a-dataset-and-proving-its-licenses).

## Configuration reference

Two layers: **files** in `configs/` (per-project, edit freely) and **environment** (`CF_*` variables or `.env`).

### `configs/sources.yaml`

| Key | Meaning |
|---|---|
| `scope.must` | List of keyword groups. A paper must hit at least one keyword in **every** group. This is the on-topic gate. |
| `scope.boost` | `{name: [keywords]}`. Each group hit adds one point and adds `name` to the paper's `topic_tags`. |
| `scope.comparison_only` | Keywords marking neighbouring fields; a hit in the *title* demotes the paper to `borderline`. |
| `license_allow` | License families that may proceed and be redistributed (default `CC0`, `CC-BY`, `public-domain`). |
| `license_flag` | Families that proceed but are marked `license_flagged` for review (default `CC-BY-SA`). |
| `sources.<name>.enabled` | Turn a source on/off. |
| `sources.openalex.terms` / `.filter` | Search terms (limit is split across them) and an OpenAlex filter expression. |
| `sources.chemrxiv.terms` / `.base_url` | Search terms for Crossref-hosted ChemRxiv preprints. |
| `sources.arxiv.oai_sets` / `.categories` | OAI-PMH sets to harvest and the arXiv categories to keep. |
| `sources.europepmc` / `.core` / `.osti` | Reserved per-source term lists. |

Keyword matching is case-insensitive, whole-word, and tolerant of plurals (`battery` matches `batteries`).

### `configs/generation.yaml` - `chunking`

| Key | Default | Meaning |
|---|---|---|
| `target_tokens` | 1500 | Pack paragraphs up to about this many tokens. Bigger chunks = fewer LLM calls later. |
| `max_tokens` | 2000 | Hard cap; longer paragraphs are split by sentence. Also the worst-case input for LLM context sizing. |
| `overlap_tokens` | 150 | Trailing sentences of the previous chunk repeated at the start of the next (keep <= ~10%). |
| `max_garble_ratio` | 0.10 | Chunks above this are flagged `garbled` in `chunks.quality`. |

Unknown top-level keys in this file are ignored by corpusforge, so a host project can keep its own settings there.

### `configs/llm_routes.yaml`
The `llmrouter-free` routes file - see [its configuration reference](https://github.com/fredbuildsai/llmrouter-free#configuration-reference).
corpusforge's runner uses a route named `extract` by default (`ChunkTaskSpec.route`), so define one (or set `route=`).

### Environment (`CF_` prefix, or `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `CF_DATABASE_URL` | `sqlite:///data/corpus.db` | Any SQLAlchemy URL. Relative SQLite paths resolve against the project directory. |
| `CF_LOG_DATABASE_URL` | `sqlite:///data/logs.db` | Separate DB for structured logs and LLM call metrics (keeps the write lock free). |
| `CF_DATA_DIR` / `CF_CONFIGS_DIR` | `data/` / `configs/` | Where raw files, exports and YAML live. |
| `CF_CONTACT_EMAIL` | *(empty)* | Sent in the User-Agent (`mailto:`) to OpenAlex/Crossref/Unpaywall. **Set this** - it puts you in their polite pool. |
| `CF_APP_NAME` / `CF_APP_URL` | `corpusforge` / repo URL | Identity in the User-Agent. |
| `CF_TOKENIZER_MODEL` | `unsloth/gemma-4-E2B-it` | HF tokenizer used to size chunks (from the local HF cache; word-count fallback). Match your target model. |
| `CF_ALLOW_PAID` / `CF_MAX_USD_PER_DAY` | `false` / `5` | For the router's paid-model guard (pass them to `build_router`). |
| provider keys | | `OPENROUTER_API_KEY`, `GROQ_API_KEY`, ... read by LiteLLM; `CORE_API_KEY`, `OPENALEX_API_KEY` optional. |

The project directory is the **current working directory** (like git): `cd` into any folder with a `configs/`
and everything resolves there.

## CLI reference

Every command is safe to interrupt and re-run.

| Command | Purpose |
|---|---|
| `corpusforge init [--force]` | Scaffold configs, `.env`, data directories and the database. |
| `corpusforge db init` | Apply migrations to the latest revision. |
| `corpusforge discover [--source openalex\|chemrxiv\|arxiv\|all] [--limit N] [--from-date D] [--batch-size N] [--fields default\|all\|a,b]` | Discover and store candidates. |
| `corpusforge screen [--rescreen]` | License gate + relevance. |
| `corpusforge fetch [--limit N]` | Download full text for accepted documents. |
| `corpusforge images [--limit N]` | Download figures referenced by stored JATS XML. |
| `corpusforge add-local FILE... [--license L] [--doc-id ID]` | Add local PDFs, or attach one to an existing document. |
| `corpusforge parse [--limit N] [--reparse] [--from-existing-chunks]` | Parse and chunk fetched documents. |
| `corpusforge fetch-failures [--output CSV]` | Accepted documents with no full text, with a link and the reason. |
| `corpusforge pipeline-failures [--output CSV]` | Documents with failed annotation tasks, across every stage. |
| `corpusforge blacklist [--threshold N]` / `blacklist-list` / `blacklist-remove ID` | Stop retrying documents that always fail. |
| `corpusforge stats` | Row counts and document breakdowns by status, source and license. |
| `corpusforge logs tail [-n N] [-f]` / `logs query` / `logs stats` / `logs clear` | Query the structured log. |

`--verbose` (before the command) logs at DEBUG.

## Python API

```python
from corpusforge import (
    Settings, set_settings, migrate, get_session,
    Document, Chunk, GenTask,
    ChunkTaskSpec, run_batch, run_backlog,
    export_cpt, ExtraCptRow,
)
```

Everything the CLI does is also a function (`corpusforge.fetch.fetch_document`, `corpusforge.parse.pipeline.parse_and_chunk`,
`corpusforge.screen.screening.screen_documents`, `corpusforge.sources.store.upsert_records`, ...), so you can drive
the pipeline from a notebook or your own orchestration.

```python
from sqlalchemy import select
from corpusforge import Document, get_session, migrate

migrate()                                   # idempotent
with get_session() as s:
    chunked = s.scalars(select(Document).where(Document.status == "chunked")).all()
    print(len(chunked), "parsed papers,", sum(len(d.chunks) for d in chunked), "chunks")
```

## Writing an annotation stage (the batched chunk runner)

A stage is *"for each chunk, ask an LLM for structured output and store rows"*. You provide four things; the
runner provides the rest.

```python
from pydantic import BaseModel
from corpusforge import ChunkTaskSpec, run_backlog, run_batch

# 1. the shape of the answer - ALWAYS batch-shaped: one entry per excerpt, carrying its index
class ChunkNotes(BaseModel):
    chunk_index: int
    notes: list[str]

class BatchNotes(BaseModel):
    results: list[ChunkNotes]

# 2. the prompt, for a list of excerpts (a single chunk is just a list of one)
def build_messages(texts: list[str]) -> list[dict[str, str]]:
    body = "\n\n".join(f'Excerpt {i}:\n"""\n{t}\n"""' for i, t in enumerate(texts))
    return [
        {"role": "system", "content": "You extract notes from scientific excerpts."},
        {"role": "user", "content": 'Return JSON {"results": [{"chunk_index": int, "notes": [str]}]} with exactly '
                                    "one entry per excerpt, in order.\n\n" + body},
    ]

# 3. how to store one chunk's result. Replace earlier rows for the chunk; return a small summary payload.
def persist(session, chunk, chunk_result, model: str) -> dict:
    session.query(Note).filter(Note.chunk_id == chunk.chunk_id).delete()
    session.add_all(Note(chunk_id=chunk.chunk_id, text=t, generator_model=model) for t in chunk_result.notes)
    return {"notes": len(chunk_result.notes)}

spec = ChunkTaskSpec(
    task_type="take_notes",             # gen_tasks key = "take_notes:<chunk_id>"
    build_messages=build_messages,
    response_schema=BatchNotes,
    persist_result=persist,
    output_tokens_per_chunk=800,        # a batch of N chunks gets max_tokens = 800 * N
    route="extract",                    # route name in llm_routes.yaml
)

# 4. run it
run_batch(engine, router, spec, ["doc:1#s00-c00"])                          # one chunk == a batch of one
run_backlog(engine, router, spec, limit=2000, batch_size=5, concurrency=4)  # the whole backlog
```

`router` is an `llmrouter_free.LLMRouter` (build it with `llmrouter_free.build_router(load_routes(...), engine=engine)`).
Your `Note` table lives on **your own** SQLAlchemy base; corpusforge only ever touches its own tables.

What the runner guarantees:

- **One mechanism.** 1 chunk and N chunks take the identical code path with the identical batch-shaped prompt - there
  is no separate single-chunk route to drift out of sync.
- **Resumable.** Each chunk has a `gen_tasks` row (`pending -> done/failed`, `attempts`, `last_error`). Done chunks
  are skipped (`force=True` re-runs and bypasses the response cache).
- **Missing chunks are retried once.** If the model returns results for only some excerpts, the missing chunks are
  re-asked, alone, exactly once, then marked `failed` (no infinite loops).
- **Back-off when everything is down.** A batch where *every* chunk failed means the route is exhausted, not that
  the chunks are bad: the batch is re-queued `retry_wait_seconds` (default 300) later. After
  `max_consecutive_failures` (default 10) such batches the run **stops without losing anything** - re-run the same
  command later.
- **Diversified selection.** Pending chunks are round-robined across documents, so one 700-chunk textbook cannot eat
  a run's whole budget. `restrict_to={chunk ids}` pins several stages to one fixed batch. Blacklisted and unparsed
  documents are excluded.
- **Concurrency without deadlocks.** `concurrency=N` runs N batches from a thread pool sharing one router. No database
  transaction is held across an LLM call (on SQLite, two overlapping writers deadlock), so bookkeeping and
  persistence each use short, committed sessions.
- **Progress.** A timestamped table (calls/min, done/skipped/failed/remaining) every `report_every` calls.

`run_backlog` returns a `BacklogResult(outcomes, api_calls, stopped, pending)`; `stopped=True` means the
consecutive-failure limit was hit.

## Exporting a dataset and proving its licenses

```python
from pathlib import Path
from corpusforge import export_cpt, ExtraCptRow, get_session

with get_session() as s:
    counts = export_cpt(
        s, Path("data/export/v1"),
        pack_tokens=2000,                      # pack a paper's consecutive chunks into ~2000-token rows
        extra_rows=[ExtraCptRow(text="A perovskite is ...", attribution_key="ontology:materials",
                                license="CC-BY-4.0", title="Domain ontology")],
    )
# -> {"train": 8123, "eval": 411}; writes cpt_train.jsonl, cpt_eval.jsonl and
#    cpt_train.attribution.json, cpt_eval.attribution.json
```

- Only papers with a `split`, not blacklisted, are exported. Overlap sentences are stripped (a language-modeling
  objective must not see the same tokens twice).
- `extra_rows` are rows that have no source paper (definitions from an ontology, say). They are attributed to their own
  key and always go to `train`.
- The **attribution manifest** maps each source (DOI, else `doc_id`) to `{doc_id, license, title, chunks: [row indices]}`.
  A redistributed dataset can therefore carry per-record CC-BY attribution rather than one blanket credit.
- `export.licensing.classify_export` reads the manifests and reports which sources are not redistributable;
  `restricted_row_indices_for_file` + `write_filtered_jsonl` produce a public-safe copy with those rows removed, and
  `write_license_status` records the verdict (`LICENSE_STATUS.json`) that release tooling can refuse to publish
  without.
- `export.hf_release.build_model_card(..., card=ModelCardInfo(tags=..., intended_use=..., limitations=...))` writes
  a Hugging Face model card whose training-data section is computed from the manifests; `push_gguf_to_hub` uploads it
  with an `ATTRIBUTION.json`.

Building your own row types (SFT, DPO, ...) with the same guarantees: use `AttributionManifest`, `write_jsonl`,
`get_doc` and `row_images` from `corpusforge.export.corpus`, exactly as `batterygemma` does.

## Embedding in your own project

corpusforge is designed to be a *library under a domain project*, not a fork target.

**Settings.** Subclass `Settings` with your own prefix and install it once:

```python
from corpusforge import Settings, set_settings
from pydantic_settings import SettingsConfigDict

class MySettings(Settings):
    model_config = SettingsConfigDict(env_prefix="MY_", env_file=".env", extra="ignore")
    my_option: str = "x"

set_settings(MySettings())      # every corpusforge module now follows it
```

**Your tables** go on your own `DeclarativeBase`, referring to corpusforge rows by *string ids* (`doc_id`,
`chunk_id`) with no foreign keys. The two schemas then migrate independently.

**Migrations.** corpusforge ships its Alembic scripts inside the package and tracks them in a private version
table (`corpusforge_alembic_version`), so your project's own `alembic_version` history can live in the same
database. `corpusforge.migrate()` upgrades; `corpusforge.stamp()` adopts a database whose tables already exist
(no DDL is run).

**CLI.** Reuse the commands on your own Typer app:

```python
import typer
from corpusforge.cli import register_pipeline_commands, logs_app

app = typer.Typer()
register_pipeline_commands(app)      # discover, screen, fetch, images, add-local, parse, blacklist, ...
app.add_typer(logs_app, name="logs")
```

**Logging.** `configure_logging(roots=("corpusforge", "llmrouter_free", "myproject"))` sends every tree to `logs.db`.

## Data model

| Table | Row | Notable columns |
|---|---|---|
| `documents` | one paper | `doc_id` (`<source>:<external_id>`), `doi`, `title`, `license`, `license_flagged`, `relevance`, `topic_tags`, `status`, `status_reason`, `split`, `duplicate_of`, `blacklisted`, `raw_metadata` |
| `files` | a stored file | `doc_id`, `kind` (pdf/xml/image), `path`, `sha256`, `bytes` |
| `chunks` | a model-sized piece | `chunk_id` (`<doc_id>#s<section>-c<NN>`), `section_path`, `section_type`, `order`, `tokens`, `overlap_prev_tokens`, `text`, `captions`, `images`, `quality`, `purpose` |
| `gen_tasks` | one unit of LLM work | `task_type`, `key` (unique), `status`, `attempts`, `last_error`, `payload` |
| `releases` | a dataset release | `version`, `filters`, `counts`, `content_hash` |

All column types are portable, so the same models run on PostgreSQL.

## Limitations

- **Text-first.** Figures are downloaded and linked to chunks, but nothing here reads them; tables in PDFs are
  flattened by Docling.
- **Born-digital PDFs.** OCR is off; scanned papers parse poorly.
- **English** keyword matching and sentence splitting.
- **Coverage is only as good as the sources.** Many papers have no lawful full-text route; the tool reports them
  rather than working around it.
- **SQLite concurrency.** Fine for one process with a thread pool; use PostgreSQL for multi-process runs.
- **Relevance is keyword-based.** It is deliberately cheap and auditable; embedding-based or LLM screening would be an
  extension.
- **License normalization is heuristic** (URLs, short codes, free text). Check `license_evidence` for anything you
  publish.

## Development

```bash
git clone https://github.com/fredbuildsai/corpusforge && cd corpusforge
# expects a sibling checkout of llmrouter-free for editable development:
#   git clone https://github.com/fredbuildsai/llmrouter-free ../llmrouter-free
uv venv && uv pip install -e ".[dev,parse,dedupe]"
uv run pytest -q          # ~220 tests, no network, no API keys
uv run ruff check src tests
```

Tests are hermetic: HTTP is mocked with `respx`, the LLM is a scripted fake, databases are temp files. The README
and architecture docs are themselves tested (`tests/test_docs.py`, `tests/test_readme_examples.py`). CI runs on
Python 3.11 and 3.12. See the [architecture documentation](docs/architecture/README.md) (arc42) before larger
changes.

## License

MIT - see [LICENSE](LICENSE). Content you build with it keeps the licenses of its sources; the attribution manifests
exist so you can honor them.
