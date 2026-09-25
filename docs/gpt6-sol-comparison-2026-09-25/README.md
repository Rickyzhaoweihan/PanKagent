# GPT-6 Sol backend and public-evidence policy

Implemented on Ringo from `1e42e80` (deployed behavior base `947777c`). Public API: https://dev.pankgraph.org/gpt6/ . No frontend source files changed. The existing Sonnet dev route remains Sonnet; model selection has not been changed globally.

## Results

Ten graph questions, two repeats each. Initial order: GPT, Sonnet, Sonnet, GPT. A revised generic GPT prompt was then tested twice on the same cases, so the tuned results are in-sample development evidence, not a held-out model ranking. Same graph/schema/GPU Cypher service, fresh sessions, application plan cache off, reasoning/thinking off. Literature disabled equally and excluded from cost/quality.

| Measure | GPT initial | GPT tuned | Sonnet 5 |
|---|---:|---:|---:|
| Reviewable plans | 11/20 | 14/20 | 18/20 |
| Exact reference membership + complete selected checks | 5/20 | 11/20 | 14/20 |
| Median plan-ready time among successful attempts | 6.899 s | 7.240 s | 11.530 s |
| Mean total API cost per attempted query | $0.028956 | $0.031585 | $0.068217 |
| Mean planning cost per attempted query | $0.018693 | $0.018045 | $0.036194 |
| Mean formatter cost when invoked | $0.019281 | $0.019343 | $0.033115 |
| Formatter invocations | 10 | 14 | 18 |

Plan-ready time includes checked graph preparation but excludes browser paint/public networking. Failed attempts are included in cost, excluded from successful latency. A zero-output failure is not a successful empty-result query. The strict metric penalizes narrower class coverage and a useful partial coloc answer with an additional failed check; manual review distinguishes these from wholly unsupported answers.

Five identical frozen evidence inputs isolated writing from planning. Common input-body hashes matched across providers. Final GPT: mean $0.016915, median completion 2.828 s, first visible text 1.042 s, mean 620 characters. Sonnet: $0.032304, 5.304 s, 1.133 s, 1,299 characters. These are returned-usage estimates, not invoices.

Recommendation: retain Sonnet planning for now; evaluate a fixed Sonnet-planner/GPT-formatter combination next if selected by the owner. No hybrid has been deployed. GPT is concise and often better calibrated about incomplete evidence. Sonnet reaches more usable plans. Neither is flawless: GPT added irrelevant sample-absence wording to one donor-only answer, and Sonnet made an unsupported opening absence claim for an unverified empty intersection.

Both models incorrectly narrowed the alias enrichment question to the parent cell type, omitting a recorded subtype. Both failed a valid recorded-control/Stage-1 intersection because a shared preparation check treated it as requiring diagnosed diabetes. Initial shared-partner plans used undefined combination roles despite both raw partner sets containing the seven expected shared genes. The revised GPT instructions fixed both shared-partner repeats and both definition repeats; donor follow-up remained inconsistent, and colocalization planning remained weaker than Sonnet. No expected gene lists/answer IDs were inserted in the prompt.

## Implementation

`pankagent_vnext/openai_provider.py` adapts OpenAI Responses to the existing message/tool/stream boundary. It preserves tool call IDs and optional schemas, normalizes cached/read/write/output usage, detects missing final usage, keeps ambiguous reservations, and settles definitive pre-generation rejections at zero. GPT uses `gpt-6-sol`, reasoning `none`, Standard tier, `store=false`, no automatic retries. Existing response fields and legacy Claude component/class names remain for compatibility; actual model identity is explicit.

The GPT instruction layer is versioned in `answer_skills/providers/gpt6_sol.md`, validated by answer bundle manifest 1.10.3. SHA-256: `820b41efb0d7ce74b7fe714e78ff9a4cd59a4f3d54d0f70f8b76b7b34630687e`. It adds generic verified-candidate reuse, class coverage, precise definition retrieval, valid combination roles, targeted repair, and concise evidence-backed writing. Common biological contracts remain authoritative. The deterministic entity resolver remains unchanged.

The owner explicitly authorized all public PanKgraph donor/sample/clinical metadata for model/API processing. Active privacy gates were removed from answer facts, compact scientific excerpts, public payloads, aggregate projections and event-context redaction. Aggregate-only is a presentation preference, not a public-record transport restriction. Size limits, query/request-scope validation, authentication, credentials and spending controls remain.

`pankgraph_results/gpt6_proxy.py` provides a separate allowlisted loopback ingress to 8798, streams SSE, excludes operator endpoints, strips incoming credentials, and rewrites returned API links into `/gpt6/`. Hosting adds only `/gpt6` rules; all existing rules remain. A broad root `/v2` rewrite was rejected by automatic approval review and never applied. The safer prefix-only API alternative was approved. The API root documents the request workflow; the existing demo frontend requires a serving-prefix adjustment that was asked about but not authorized. No delivered HTML or frontend source was modified.

## Accounting

Cumulative new test ledgers under `/db/pankagent-vnext-private/operations/model-comparison-20260925/budgets/`:

- GPT: $1.498748 spent of $20; $18.501252 remains; 143 calls; zero pending reservations.
- Claude: $1.679480 spent of $10; $8.320520 remains; 69 calls; zero pending reservations.

Includes smoke, failed development trials, repeats, prompt tuning, both formatter evaluations and public plan smoke. No reset. Both models use $2/M input and $10/M output; cache reads $0.20/M and writes $2.50/M in the configured short-context tier. GPT >272K context pricing and conservative reservations are handled separately. Reasoning tokens are already included in output. Prices verified against [OpenAI model docs](https://developers.openai.com/api/docs/models/gpt-6-sol) and [Anthropic Sonnet 5 docs](https://platform.claude.com/docs/en/models/sonnet-5/whats-new-sonnet-5).

GPU/infrastructure and the separate shared HIRN service are excluded. No HIRN inference was called in this evaluation. The deployed service retains the existing literature integration, whose per-request usage is not available in the local ledger.

## Validation and deployment

Final offline suite: 2809 passed, 19 failed, 5 skipped, 231 subtests passed. Failure identities exactly match the unchanged baseline's 19 failures. Results overlay: 196 passed, four failures identical to its unchanged live baseline. New proxy tests pass. No globally green claim.

Services on jieliu3:

- GPT 8798: `/db/pankagent-vnext-private/operations/model-comparison-20260925/releases/gpt6-v4/backend`; dedicated state `dev.pankgraph.gpt6/` under the same operations root.
- Sonnet 8794: `/var/local/serviceuser/projects/pankgraph-demo/releases/20260925-public-evidence/backend`; existing state/model/ledger preserved.
- Results 8795: `20260925-gpt6-ingress-v3/backend` under that release root; additive overlay of exact live `20260924-localhost-cors`, retaining all previous auth/CORS/dashboard behavior and identical frontend symlinks.
- Health 8796: unchanged, including its existing log-rotation fix.

All four health endpoints returned 200. The existing dev HTML hash is unchanged. Saved Sonnet results now retain public IDs/classifications. One public GPT plan became confirmable in 6.487 seconds including network polling, reported the correct model, preserved public donor records, returned prefix-safe links, and passed cancellation/SSE replay. It was not confirmed, avoiding separate literature charges. Full graph execution and formatting were exercised by the real-runtime comparison harness against live graph/model services.

Operations used owner-aware managers, checked idle work, backed up SQLite, and preserved existing symlinked logs. Logs/backups/artifacts remain serviceuser-owned under the private `/db` tree, directories 0700/files 0600. Release-local virtual environments are reused via symlinks and were not modified. No production process was restarted.

GPT management as serviceuser from its exact recorded release: `python -B -m deploy_reliability.manage_gpt6 status|stop|start --release <recorded-backend>`. Sonnet/results use `deploy_reliability.manage` with the corresponding service argument and exact release. Roll back code with the owned manager; never restore an old budget/session DB to roll back code. Preserved originals: Sonnet `20260925-exploration-947777c`; results `20260924-localhost-cors`. Saved hosting rules permit removal of only the three added `/gpt6` rules. Refresh live ownership before operating.

## Reproduction/artifacts

- `scripts/acceptance/model_comparison.py`: live runtime, independent Cypher references, per-purpose timing/usage, saved actual answers, SSE equality and typed-ID completeness.
- `scripts/acceptance/formatter_comparison.py`: identical-evidence paired writing, first-text and completion latency, exact input hashes.
- Service-owned raw evidence, release manifests, logs, accounting, backups and deployment records: operations root above.
- Local ignored report: PanKgraph project home `Research/Reports/gpt6-sol-comparison-20260925/README.md`, `comparison.html`, `metrics.json`, compressed raw artifacts. Individual donor results are not committed.

The report contains every answer from both repeats, failures, per-query/per-role costs, exact reference memberships and an explicit scientific review. Final model choice remains with the owner.
