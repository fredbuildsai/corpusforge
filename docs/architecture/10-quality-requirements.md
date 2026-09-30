# 10. Quality requirements

## Quality tree
Provenance correctness > resumability > domain neutrality > politeness > portability > maintainability.

## Quality scenarios and how they are verified

| ID | Scenario | Verified by |
|---|---|---|
| Q1 | A paper with a non-allowed or unknown license never reaches fetch/export. | `test_license.py`, `test_screening.py`, `test_sources.py` |
| Q2 | Every exported row appears in an attribution manifest with DOI and license; extra rows are attributed to their own key. | `test_export_corpus.py` |
| Q3 | Blacklisted documents disappear from exports retroactively. | `test_export_corpus.py`, `test_runner.py` |
| Q4 | Killing an annotation run and re-running redoes only unfinished chunks; `force` redoes done ones and bypasses the cache. | `test_runner.py` |
| Q5 | 1 chunk and N chunks use the same prompt shape and budget rule. | `test_runner.py` |
| Q6 | A chunk dropped by the model is retried exactly once, never looped. | `test_runner.py` |
| Q7 | When every provider is down the run backs off, then stops without losing state. | `test_runner.py` |
| Q8 | One huge document cannot starve the rest of a bounded run. | `test_runner.py` |
| Q9 | A bot challenge is recorded, not bypassed; polite intervals and backoff are honored. | `test_http_client.py`, `test_fetch.py` |
| Q10 | The migrated schema equals the models; migrations use a private version table; existing databases can be adopted without DDL. | `test_migrate.py` |
| Q11 | Migrating never leaves the application's loggers disabled. | `test_db_session_logging.py` |
| Q12 | Parsing is idempotent and never crosses section boundaries; overlap is recorded and strippable. | `test_parse.py`, `test_parse_pipeline.py`, `test_export_corpus.py` |
| Q13 | A host can embed the package via `set_settings` and reuse the CLI commands. | `test_settings_paths.py`, `test_cli.py` |
| Q14 | Documentation matches the code: README examples run, links resolve, cited tests exist. | `test_docs.py`, `test_readme_examples.py` |
| Q15 | Model cards are domain-neutral unless the host supplies domain text. | `test_export_hf_release.py` |

## Non-functional targets
- Full test suite < 30 s, no network, no keys.
- Memory bounded by one document at a time in parse; the runner holds only chunk ids and small batches.
