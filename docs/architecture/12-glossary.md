# 12. Glossary

| Term | Meaning |
|---|---|
| **Corpus** | The set of documents, files and chunks in `corpus.db`. |
| **Document** | One paper/preprint/thesis record (`documents` row), identified by `doc_id = "<source>:<external_id>"`. |
| **Chunk** | A section-bounded, token-sized piece of a document's text, identified by `chunk_id = "<doc_id>#s<section>-c<NN>"`. |
| **Overlap** | Trailing sentences of the previous chunk repeated at the start of the next; length stored in `overlap_prev_tokens`. |
| **Screening** | License gate plus keyword relevance scoring, producing `accepted` / `borderline` / `rejected`. |
| **License family** | A license id without version (`CC-BY-4.0` -> `CC-BY`); the unit of allow/flag decisions. |
| **Flagged** | Allowed to proceed but marked for review (e.g. share-alike). |
| **JATS** | The XML format of PubMed Central / Europe PMC full text. |
| **CPT** | Continued pre-training: training on plain domain text. |
| **Task** | A `gen_tasks` row: one unit of LLM work keyed `"<task_type>:<chunk_id>"`. |
| **Stage / ChunkTaskSpec** | A host-defined per-chunk LLM job (prompt, schema, persistence). |
| **Batch** | Several chunks sent in one LLM call; a single chunk is a batch of one. |
| **Backlog** | The pending chunks for a stage; `run_backlog` drains it. |
| **Route** | (llmrouter-free) an ordered list of LLM deployments to try. |
| **Blacklist** | Flag on a document that makes selection ignore it and export exclude it. |
| **Attribution manifest** | `<name>_<split>.attribution.json`: source -> license, title, exported row indices. |
| **Host project** | A domain project (e.g. batterygemma) that depends on corpusforge. |
| **Split** | `train` / `eval`, assigned per paper by hashing `doc_id`. |
| **Polite pool** | The faster, friendlier tier public APIs give clients that identify themselves with a contact e-mail. |
