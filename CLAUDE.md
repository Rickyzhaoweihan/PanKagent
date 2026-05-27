# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PanKgraph AI Assistant — a multi-agent, multi-source system for querying a Type 1 Diabetes knowledge graph in natural language. A PlannerAgent orchestrates specialized sub-pipelines: **Cypher (Neo4j KG)**, **SQL (PostgreSQL genomic coordinates)**, **Functional Data API (REST)**, and **GLKB literature**. Claude (Sonnet) handles orchestration/formatting/reasoning; a fine-tuned local vLLM (`cypher-writer`) handles text-to-Cypher AND text-to-SQL generation.

## Commands

### Setup
```bash
pip install -r requirements.txt
pip install -r requirements-server.txt   # for API server mode
# Create config.py from config.py.example (or set ANTHROPIC_API_KEY in .env)
# Create .env from .env.example
```

Active env vars the system reads at startup:
- `ANTHROPIC_API_KEY` (required — falls back to `CLAUDE_API_KEY` in .env via server.py alias)
- `NEO4J_BOLT_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` (default `bolt://localhost:8687`, `neo4j`/`password`/`pankgraph`)
- `VLLM_PORT` (default 8002)
- `PORT` (server)
- `MAX_CONCURRENT_QUERIES` (default 5 — max pipelines run concurrently; bounds the `_pipeline_semaphore`)
- `CACHE_VERSION` (default `1` — folded into the answer-cache key; bump to invalidate every cached answer, e.g. after a Neo4j reload)
- `CACHE_HIT_DELAY_SECONDS` (default `15` — deliberate wait before returning a cached answer so a hit isn't suspiciously fast; `0` disables)
- `GLKB_URL` (default `http://localhost:8004/stream` — the local GLKB_agent SSE endpoint for literature synthesis)
- `OPENAI_API_KEY` (only required for `batch_evaluator.py`)

### Running
```bash
python3 main.py                          # interactive REPL
python3 main.py "your question"          # single question
python3 main.py --plan "your question"   # plan mode (show plan → confirm/revise/quit)
python3 main.py --rigor "..."            # stricter evidence-only format
python3 server.py                        # FastAPI server on port 8080
python3 server.py 8001                   # custom port
```

Background + logs:
```bash
nohup python3 server.py 8001 > server.log 2>&1 & echo $! > server.pid && disown
tail -f server.log
kill $(cat server.pid)
```

### vLLM (cypher-writer, required for Cypher + SQL generation)
```bash
nohup python -m vllm.entrypoints.openai.api_server \
  --model /db/usr/rickyhan/cypher-writer --served-model-name cypher-writer \
  --host 0.0.0.0 --port 8002 --gpu-memory-utilization 0.9 \
  --max-model-len 8192 --max-num-seqs 32 > vllm.log 2>&1 & disown
```

### External services used at runtime
- **Local Neo4j PanKgraph ADA** at `bolt://localhost:8687` / browser `:8475` — 5.4M nodes, schema in `PankBaseAgent/text_to_cypher/data/input/neo4j_schema_ada.json`
- **Local PostgreSQL 17** at `127.0.0.1:5432` db `pankgraph` (user `postgres` / pw `password`, connect with `gssencmode=disable`), four entity tables: `ensembl_genes_node`, `gwas_snp_id_node`, `ocr_peak_node`, `qtl_snp_node` (5.4M rows total)
- **GLKB API** — local `GLKB_agent` FastAPI service at `http://localhost:8004/stream` (SSE-streaming literature synthesis; override with the `GLKB_URL` env var); called by `skills/glkb/scripts/glkb_client.py`. Must be running for literature to work (else `call_glkb` returns `status:"failed"` and the literature block is simply omitted). The old remote (`glkb.dcmb.med.umich.edu/api/frontend/llm_agent`) was retired — it now 301-redirects to a static site; the client guards against that (rejects redirects / non-`event-stream` responses). HIRN is fully disabled
- **RDS Lambda** — gene-name → Ensembl-ID resolution for text2sql
- **Anthropic Claude** — Sonnet for orchestration + format, Haiku for chat follow-up classifier

### Tests
```bash
# Unit tests (mocked — no external deps)
pytest tests/                                      # chat classifier, GLKB client, literature merge
pytest PankBaseAgent/text_to_cypher/test_*.py      # Cypher validator

# Standalone text-to-SQL smoke test (requires vLLM + PostgreSQL)
python3 PankBaseAgent/text_to_sql/test_text2sql.py

# Integration (require running server)
python3 test_server.py [port]
python3 test_server_stream.py
```

## Architecture

### Agent orchestration flow

```
User question
  ├─ PlannerAgent (main.py) — runs N=5 candidates in parallel, picks best by non-empty results
  ├─ each candidate dispatches in threads:
  │   ├─ pankbase_chat_one_round → query-planner skill → execute_plan()
  │   │     ├─ plan_type "parallel":  KG steps combined via WITH + non-KG steps run; non-KG respects depends_on
  │   │     ├─ plan_type "chain" (pure KG):  existing combine_chain() compound Cypher
  │   │     └─ plan_type "chain" (cross-source): strict sequential, entities flow step→step
  │   └─ run_literature_parallel → GLKB literature retrieval (HIRN disabled)
  └─ final pipeline:
      ├─ FormatAgent (simple)  → compresses + formats + hallucination check
      └─ ReasoningAgent (complex) → multi-hop reasoning + hallucination check
```

Two independent test-time-scaling loops:
- **Outer** (`PLANNER_CANDIDATES=5` in `main.py`) — 5 top-level PlannerAgent candidates, score by non-empty Neo4j results
- **Inner** (`NUM_CANDIDATES=1` in `qp_query_planner.py`) — per-sub-pipeline candidates

A per-request `rigor` flag (defaults True at the server endpoints) routes to `rigor-format-agent` / `rigor-reasoning-agent` which enforce stricter evidence-only output. `rigor` is threaded explicitly through `_select_pipeline(is_complex, rigor, **kwargs)` / `run_plan_confirm` / `chat_one_round` — there is no `RIGOR_MODE` module global (removed so concurrent requests with different rigor settings don't interfere).

### Data sources and their query languages

| Source | Where | Generated by | Step `source` |
|---|---|---|---|
| Knowledge graph | Neo4j Bolt `localhost:8687` | vLLM `cypher-writer` via `Text2CypherAgent` | (none — KG default) |
| Genomic coordinates | PostgreSQL `pankgraph` db | vLLM `cypher-writer` via `Text2SQLAgent` | `"genomic"` |
| Literature | GLKB SSE API | `glkb_client.py` → `literature_runner.py` | dispatched separately |

HPAP MySQL skill is **disabled** — donor metadata now lives in the Neo4j KG as `donor` nodes (193 donors with `diabetes_type`, `t1d_stage`, `aab_state`, `hla_status`, etc.). `_run_hpap_step` remains in `main.py` but is never wired (`hpap_handler=None` at all call sites).

### Cross-source chain plans (new)

When a later step needs entities from an earlier step (e.g. "find effector genes → get functional data on them"), the planner emits `plan_type: "chain"` with mixed sources. The executor:

1. Runs steps strictly sequentially in `id` order.
2. After each step, `_extract_entities_from_result()` pulls `gene_names`, `gene_ids`, `snv_ids`, `donor_ids` from either `records[*].nodes[*].properties` (KG) or `rows[*]` (SQL rows).
3. Passes them as `prior_entities` to the next step's handler.

Handler signatures all accept the optional kwarg:
```python
def _run_<source>_step(question_text: str, prior_entities: dict | None = None) -> dict
```

### Neo4j ADA schema

Current schema: `PankBaseAgent/text_to_cypher/data/input/neo4j_schema_ada.json` (loaded by `schema_loader.py`). Key changes from the legacy schema:

- `snv` replaces `snp`, `OCR_peak` replaces `OCR`, `anatomical_structure` replaces `cell_type`
- New relationships: `OCR_peak_in`, `gene_activity_score_in`, `gene_detected_in`, `gene_enriched_in`, `T1D_DEG_in`, `pathway_annotation;KEGG`, `pathway_annotation;reactome`, `fGSEA_gene_enriched_in`, `fGSEA_enriched_in`
- Relationship names with semicolons MUST be backtick-escaped in Cypher: `` [r:`function_annotation;GO`] ``
- New nodes: `donor`, `Sample node`, `data_modality`, `anatomical_structure`, `kegg`, `reactome`

### Server endpoints (`server.py`)

All agents are pre-initialized at startup via FastAPI lifespan. `server.py` loads `.env` before any module imports so that `ANTHROPIC_API_KEY` is available to `claude.py`/`qp_query_planner.py` (which read env at import/first-use time). An alias `CLAUDE_API_KEY → ANTHROPIC_API_KEY` is applied for backward-compat.

**Chat (multi-turn, built on plan mode):**
- `POST /chat/start` — plan_start → auto plan_confirm → return answer, session_id, plan summary
- `POST /chat/message` — Haiku-classified as `context_only` (answer from history) or `new_query` (plan + auto-confirm)
- `POST /chat/plan/confirm` — confirm a pending plan. **Idempotent**: the answer is cached for `CONFIRM_RESULT_TTL_SECONDS` (600s) keyed by `plan_session_id` (`_confirm_results`), so a retry of a slow/timed-out confirm replays the cached `ChatResponse` (200) instead of 404. Concurrent confirms for the same plan are de-duplicated via `_confirm_inflight` (an `Event`); the non-owner waits and replays. Core work is the synchronous `_confirm_execute`.
- `POST /chat/plan/confirm/stream` — same as `/chat/plan/confirm` but returns NDJSON (`{"event":"heartbeat"|"result"|"error"}`); a heartbeat every ~15s keeps the connection alive so long answers survive an upstream proxy's idle-read timeout. Validation (404/409/410) happens before the stream opens; shares the `_confirm_results` cache.
- `POST /chat/revise` — revise the last plan in the session, auto-confirm, replace last assistant turn
- `GET /chat/history` / `DELETE /chat/end`

**Plan (manual review flow):**
- `POST /plan/start` — returns plan + session_id for user to review
- `POST /plan/revise` — revise the plan (repeatable)
- `POST /plan/confirm` — run the final format/reasoning pipeline on the session's neo4j_results

**NOTE**: legacy `/query` and `/query/stream` endpoints were removed — they went through `chat_one_round()` which has a bug path that returns 0 records silently. All public traffic should use `/chat/*` or `/plan/*` which go through the plan pipeline (`run_plan_start` → `run_plan_confirm`).

**Plan-stage functional_data links**: `main.py:enrich_plan_functional_data_links(plan)` runs inside `run_plan_start` / `run_plan_revise` (so every plan-returning endpoint is covered). For each step with `source == "functional_data"` it resolves the REST call (same logic as `POST /functional-data`: `extract_endpoint_and_params` → `_validate_selection` → URL build) and attaches `step["functional_data_api"] = {endpoint, url, params}` (or `{error}`) to `plan_json`. This lets the frontend surface the API link at plan time without waiting for `/plan/confirm`. Each functional_data step costs one extra Sonnet call (`extract_endpoint_and_params`); plans with no functional_data steps pay nothing.

### Text-to-Cypher (`PankBaseAgent/text_to_cypher/`)

1. `schema_loader.py` — caches schema, produces compact ~400-token string for vLLM
2. `text2cypher_agent.py` — LangChain wrapper around vLLM (port 8002) — lazy singleton
3. `cypher_validator.py` (~2000 lines) — scores 0-100, auto-fixes quotes, relationship variables, DISTINCT, direction, LIMIT injection for heavy relationships (`OCR_peak_in`, `gene_activity_score_in`, etc.)
4. Refinement loop: if score < 90, retry up to 5 iterations with error feedback

### Text-to-SQL (`PankBaseAgent/text_to_sql/`)

Mirrors the text2cypher pipeline for PostgreSQL genomic coordinate queries:
- `src/text2sql_agent.py` — same vLLM model, system prompt tuned for the four entity-specific PostgreSQL tables
- `src/sql_validator.py` — scores SQL, auto-quotes `"chr"`, `"start"`, `"end"` (reserved words), injects LIMIT, blocks destructive statements
- `src/pg_schema_loader.py` — compact schema string
- `src/gene_resolver.py` — pre-resolves gene symbols → Ensembl IDs via RDS Lambda before SQL generation

### ssGSEA — DISABLED

The immune-cell ssGSEA REST integration is fully disabled. `skills/ssgsea/ssgsea_client.py` remains on disk but is unreferenced; `_run_ssgsea_step` in `main.py` is a backstop stub that emits `ssgsea_disabled` and returns an empty result. The planner prompt no longer documents `source: "ssgsea"`, so no new plan should ever route there.

### Query planner skill (`skills/query-planner/scripts/`)

Core executor: `qp_query_planner.py:execute_plan()` has three paths:
- `_execute_pure_kg_chain()` — existing `combine_chain()` + single compound Cypher via WITH clauses
- `_execute_cross_source_chain()` — sequential, entities flow via `depends_on`
- `_execute_parallel_with_deps()` — KG in parallel + non-KG steps respect `depends_on`

### Streaming events (`stream_events.py`)

NDJSON per line: `{"event": str, "ts": float, "data": dict}`. Event prefixes: `plan_*`, `planner_*`, `pipeline_*`, `cypher_*`, `text2cypher_*`, `genomic_*`, `functional_data_*`, `chain_step_*`, `format_*`, `rigor_format_*`, `hallucination_check_*`, `markdown_normalized`, `answer_cache_*`, `literature_cache_*`.

`markdown_normalized` is emitted by `markdown_normalizer.repair_markdown` (wired into `main.py:extract_markdown`, covering every `answer_markdown`) ONLY when the deterministic GFM repair changed something — payload `{changed, fixes: [...], llm_repair: bool}`. The deterministic pass fixes blank-lines-around-tables/HRs, delimiter rows, and cell-count mismatches; a Claude Haiku fallback repairs the rare table block left structurally ambiguous. The literature block (`combine_literature_block`) runs the deterministic pass only.

### Experience buffer

`PankBaseAgent/experience_buffer.py` — in-context learning from past successful plans. Raw → `query_log.jsonl`; curated → `experience_buffer.jsonl` (repo root). Read by the query-planner skill to guide future plans.

`batch_evaluator.py` curates entries: uses OpenAI GPT-4 (requires `OPENAI_API_KEY`) to score raw `query_log.jsonl` entries and promote high-quality examples into `experience_buffer.jsonl`. Run with `python3 batch_evaluator.py --limit 100` or `--all`.

### Hallucination checker

`skills/format-agent/scripts/hallucination_checker.py` — regex-validates `GO_XXXXXXX` and PubMed IDs in output against retrieved data. `remove_hallucinated_ids()` strips fabricated IDs. Called from both FormatAgent and ReasoningAgent (and their rigor variants).

### FormatAgent data requirements

`prompts/format_prompt.txt` enforces exhaustive extraction — intentional and critical:
- List ALL GO terms individually (grouped by category), ALL SNPs (rsID, chromosome, PIP, tissue, effect), exact numeric values
- Zero fabricated PubMed IDs — only cite IDs present in retrieved data

When a cross-source chain runs, the format agent gets a hint in its user_input explaining that downstream supplementary results (e.g. functional_data) were retrieved using entities from a prior KG step — enabling coherent narrative summaries.

### Literature pipeline (`literature_runner.py`)

`run_literature_parallel(question, kg_context)` calls GLKB only — HIRN is fully disabled. `combine_literature_block()` still accepts a `hirn` kwarg for call-site compat but ignores it. GLKB response is capped to ~120 words and framed as complementary (never contradicting) to PanKgraph findings.

### Session persistence (`session_store.py`)

SQLite-backed store (WAL + synchronous=NORMAL) for `PlanSession` / `ChatSession`. Upserts happen synchronously on the request thread before the response is returned. Lock order: `server.py _sessions_lock` / `_chat_sessions_lock` → `session_store._conn_lock` (always in this order to avoid deadlock). Expired sessions are restored at server startup; rows are never deleted (keep-forever policy).

### Answer cache (`session_store.py` `answer_cache` table)

The slowest part of a confirm is the Sonnet rigor format/reasoning agent. `main.py:run_plan_confirm` short-circuits it: it computes `_answer_cache_fingerprint(neo4j_results, rigor, complexity, use_literature)` = `sha256` over the **sorted executed query artifacts** (`neo4j_results[*].query` — uniformly the Cypher, SQL, or functional `GET` URL; error results skipped) plus `rigor`/`complexity`/`use_literature` and `CACHE_VERSION`. NL phrasing / chat context are deliberately excluded. On a hit (`session_store.get_cached_answer`) it waits `CACHE_HIT_DELAY_SECONDS` (env, default 15 — a deliberate delay so a hit doesn't return suspiciously fast; set 0 to disable) then returns the stored pipeline JSON string directly and skips `_select_pipeline` (the rest of the flow — clean → markdown-normalize → history → follow-ups — is unchanged; the hallucination check already ran when the answer was first generated). On a miss it runs the agent then `put_cached_answer`. All cache I/O is best-effort (try/except → `answer_cache_error`); a cache failure never breaks a confirm. Persisted in `logs/sessions.sqlite`, survives restarts, benefits every confirm path (chat/plan/stream/auto_confirm). Distinct from `server.py:_confirm_results` (in-memory, `plan_session_id`-keyed, 10-min, for idempotent retries). **Invalidate after a Neo4j reload** via `POST /cache/clear` or by bumping `CACHE_VERSION` + restart. Events: `answer_cache_hit` / `answer_cache_store` / `answer_cache_cleared` / `answer_cache_error`.

**GLKB literature is cached the same way** (`literature_cache` table). The slow (~25-35s) GLKB call in `POST /chat/literature` is keyed by `main._literature_cache_fingerprint(neo4j_results)` = the executed-query artifacts + `CACHE_VERSION` (no rigor/complexity — GLKB is data-driven). On a hit it replays the stored `{glkb, markdown}` and skips the GLKB API (only successful GLKB results are stored). `POST /cache/clear` clears BOTH `answer_cache` and `literature_cache`. Events: `literature_cache_hit` / `literature_cache_store` / `literature_cache_error`.

### Watchdog (`watchdog/watchdog.py`)

Cron-driven daemon (runs every minute via flock) that probes the server on `:8001`, applies a two-strike rule, restarts via `server.pid`, and sends an email + writes to a bug log on failure. Crontab entry is documented in the file header.

### Thread-based parallelism

`_thread.start_new_thread` + `Queue` for sub-agent calls. `multi_thread_workers.py` provides `map_once()` and `map_infinite_retry()` helpers. Thread-local storage in `utils.py` (`_tls`) prevents Planner test-time-scaling candidates from corrupting each other's cypher/result buffers — and is also what makes cross-request concurrency safe.

### Concurrent requests

The server runs multiple query pipelines at once, bounded by `_pipeline_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_QUERIES)` in `server.py` (env `MAX_CONCURRENT_QUERIES`, default 5; sized against the vLLM `max_num_seqs=32` batch since each query fans out to ~5 candidates, not CPU). This replaced the old single `_request_lock` that serialized everything. Safe because per-request state is isolated: `rigor` is an explicit parameter (no module global), the cypher/neo4j/planning buffers are thread-local (`utils.py:_tls`), the Neo4j driver/vLLM agents/format-reasoning singletons are stateless or thread-safe, and PostgreSQL connections are per-call. The bound protects the single GPU (vLLM) and external API rate limits; raise it only with backend headroom.

## Conventions

- PEP 8, snake_case functions, PascalCase classes
- Prompt templates: `<agent>/prompts/<purpose>_prompt.txt`; skill prompts in `skills/<skill>/scripts/prompts.py`
- Commit messages: short, present-tense ("Add format agent", "fix cross-source chain ordering")
- Performance: `performance_monitor.py` decorators log to `logs/performance.log`

### Git remote policy

The ONLY remote configured is `rickyzhao` (`git@github.com:Rickyzhaoweihan/Pankagent.git`). `main` tracks `rickyzhao/main`, so a bare `git push` publishes there. Do not add any other remote (in particular, not `wangyiqunumich/pank3-ai-agent` — that repo is unrelated to this working copy).

## RL training (`rl_implementation/`)

Reinforcement learning for Cypher generation quality. `CypherGeneratorAgent` runs multi-turn episodes (max 5 steps) against `GraphReasoningEnvironment` (wraps Neo4j executor). `train_collaborative_system.py` orchestrates via the `rllm` framework. `DualResourcePoolManager` manages separate GPU pools for Cypher Generator + Orchestrator. Not wired into the live server — training infrastructure only.
