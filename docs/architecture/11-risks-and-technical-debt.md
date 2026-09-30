# 11. Risks and technical debt

| # | Risk / debt | Impact | Mitigation / plan |
|---|---|---|---|
| 1 | **Heuristic license normalization.** Free-text licenses vary; a mislabel could pass the gate. | Legal | `license_evidence` retained; unknown = rejected; export gate re-checks manifests. Add fixtures whenever a new pattern is seen. |
| 2 | **Publisher/API drift.** Endpoints, JATS variants and challenge pages change. | Broken discovery/fetch | Adapters are isolated behind `Source`; tests pin real-world samples; failures are recorded per document, not fatal. |
| 3 | **Docling weight.** Large dependency (PyTorch). | Install size | Optional `[parse]` extra; JATS path needs only lxml. |
| 4 | **Only CPT export lives here.** SFT/DPO row builders are domain-shaped and live in hosts. | Host duplication | Shared helpers (`AttributionManifest`, `write_jsonl`, `row_images`) exported; revisit when a second domain exists. |
| 5 | **No FK across the corpus/host boundary.** | Orphan rows possible | Host exports skip rows whose document/chunk is gone; ids are stable. |
| 6 | **SQLite as the default store.** | Multi-process contention | WAL + busy timeout; PostgreSQL path via portable types (not yet exercised in CI). |
| 7 | **Runner selection loads all pending chunk ids** into memory. | Fine at 10^5, heavy at 10^7 | Acceptable now; switch to keyset pagination if needed. |
| 8 | **Keyword-only relevance.** | Misses paraphrased on-topic papers | Cheap and auditable; embedding/LLM screening is a possible extension behind the same `screen_documents`. |
| 9 | **Grounding check is lexical.** | Accepts near-verbatim but semantically altered evidence | Documented; hosts add LLM judging on top. |
| 10 | **English-only sentence splitting.** | Poor chunk overlap in other languages | Out of scope for now. |
