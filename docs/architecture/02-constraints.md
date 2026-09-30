# 2. Constraints

## Technical
- **Python 3.11-3.12**, SQLAlchemy 2.x, Pydantic 2, Typer. Docling (PDF) and lxml are optional extras because Docling
  pulls PyTorch and is large.
- **Database portability**: only portable column types (`String`, `Text`, `Integer`, `Float`, `Boolean`, `JSON`,
  `DateTime`) so the same models run on SQLite and PostgreSQL.
- **LLM access only through `llmrouter-free`.** corpusforge never talks to a provider itself; the router owns rate
  limits, failover, caching and the daily-quota ledger.
- **Distribution from GitHub** (tags) for now, PyPI later. The package must therefore be self-contained: migrations and
  templates ship *inside* it (`corpusforge/migrations`, `corpusforge/templates`).
- **No network in tests.** HTTP is mocked, the LLM is a scripted fake.

## Organizational / legal
- **Open licenses only for redistributable output.** Default allow-list: CC0, CC-BY, public domain. NC/ND material is
  never exported publicly.
- **No circumvention.** Bot challenges (Cloudflare, Radware, ...) and paywalls are recorded and skipped, never bypassed.
  A human can retrieve the paper and attach it (`add-local --doc-id`).
- **Polite crawling.** Every request identifies the tool (and a contact e-mail when configured), respects a per-host
  minimum interval and honors `Retry-After`.
- **Public repository**: no secrets, data or databases are ever committed (`.gitignore` covers `.env`, `data/`, `*.db`).

## Conventions
- src layout, hatchling build, `uv` for development, ruff (E, F, I, B) for linting, 120-column lines.
- Every behavior change ships with a test; the suite must be green before any commit.
- Documentation is tested (`tests/test_docs.py`, `tests/test_readme_examples.py`).
