# Native tool calls and agent configuration

The executable contract is simple_agent/core.py TOOLS and schema_pack.json, not a prose-only tool list. native_client.py converts each input_schema into OpenAI function parameters without repairing arguments. candidate_api.py overrides the library defaults to http://127.0.0.1:33924/v1 and model pank-candidate.

## Request and server settings

POST /v1/chat/completions uses Authorization Bearer from the private key file, model pank-candidate, full messages, seven function definitions, tool_choice="auto", stream=true, stream_options.include_usage=true and chat_template_kwargs.enable_thinking=false. Production definitions use strict=false; strict=true was a separate protocol test. The Qwen client does NOT explicitly set temperature, top_p or max_tokens; server/checkpoint generation defaults apply. Do not report this deployment as temperature zero.

The pinned model is Qwen/Qwen3.8-27B revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0. See deployment/launch.sh for exact paths and flags: BF16, max-model-len 65536, max-num-seqs 4, max-num-batched-tokens 8192, GPU memory utilization 0.90, prefix caching and chunked prefill. Required native flags are --enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3. No fallback to text-parsed pseudo-tools is allowed. The tests were serial; max-num-seqs 4 is not a measured concurrency guarantee.

## Seven tools

| Tool | Arguments / behavior |
|---|---|
| schema | Optional labels, relations, linked_evidence. Empty object returns type index; inspect actual directed endpoints before constructing patterns. |
| resolve_entity | Required text; optional label and mode (identity/fuzzy/broad/description). Description search is currently unavailable. Returns candidates and coverage; the model chooses identity. |
| property_values | Required owner (node/edge), type, property. Bounded distinct samples, not aggregation answers. |
| query | Either pattern + standardized_question for a new connected graph, OR query_id alone to continue retained capped results. The oneOf contract disallows mixing modes. All filters AND; no automatic weakening or insertion. |
| inspect_result | Required query_id; optional kind, offset and limit (1–30). Reads retained records without another database query; reports retained_count/next_offset. |
| select_results | Required selections of query_id/purpose (requested/context); optional replace. Confirmed evidence remains pinned; does not finish the loop or erase history. |
| finish | Required status, standardized_question, answer; optional query_ids (or compatible single query_id), clarification, condition_audit, unfulfilled_conditions, include_literature. Status is complete/empty/partial/needs_clarification/unsupported/failed. Executed IDs select requested evidence; context uses select_results. |

The nested query node/edge/filter schema is in core.py PATTERN; inspect it rather than constructing string patterns or guessing relation endpoints. schema_pack.json is pinned application metadata (SHA256 b6926959988a597632cbfd781bd08356782d6304deb6fdbadbc6fdee04622f28), not reference answers. No benchmark answer is supplied to the model. Literature is disabled in this candidate even though the shared finish schema retains include_literature for compatibility.

## Two invocations and recovery

1. Prepare: original question, SYSTEM and original tools; model resolves identities, queries and selects evidence, then calls finish. Workflow persists the plan and evidence before confirmation.
2. Confirm/enrich: retain the conversation and evidence, pin approved selections and apply ENRICHMENT. The selected crosscheck addition asks the model to review coverage and ambiguity and investigate actual unresolved uncertainty. It does not force another query when retained evidence suffices. The model calls finish for this invocation; deterministic rendering uses its finish.answer and existing presentation components.

The experimental fast SYSTEM addition is NOT enabled. No planner/rewrite stage, automatic filter, history truncation, model-enforced hard call cutoff or enrichment deadline is added; Workflow.enrichment_seconds is None. Context overflow is a recorded failure, not a reason to erase messages. HTTP client timeout is None for model calls; transport disconnects can still fail.

Assistant calls preserve their native IDs/name/argument payload. Tool replies use matching tool_call_id and retained feedback. SSE fragments are assembled by call index. Missing or duplicate IDs, non-object/malformed arguments, unexpected reasoning in nonthinking mode, missing completion boundaries and length termination are detected. Recoverable protocol/tool feedback returns to the same agent loop for autonomous correction; Python does not invent corrected arguments. Private raw request/response/SSE logs retain evidence with credentials redacted.

Flow completion includes model-reported partial/empty outcomes and is distinct from complete retrieval and scientific accuracy. See model-comparison.md for measured outcomes and limitations.
