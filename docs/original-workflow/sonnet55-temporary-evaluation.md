# Temporary Sonnet 5.5 evaluation fork

User-authorized temporary fork of new-agent commit `91e4a434090aab497cfe12ee4745ff87d063245a`, on `codex/sonnet55-eval`. Original dev and `codex/workflow57-review` stay unchanged. No deployment or frontend changes.

The model is exactly `claude-sonnet-5-5`. Compatibility changes are limited to the model allowlist, $2/$10 per-million input/output pricing with existing caching accounting, `thinking.type=between_tools`, explicit high effort, and conversion of rejected forced tool selections to `auto`. Existing tool schemas, response validation, bounded retries, prompts, four database schemas and query workflow remain intact. Thinking blocks already pass through unchanged in the append-only planning conversation. No model fallback is enabled.

Official references:
- https://platform.claude.com/docs/en/models/sonnet-5-5/overview
- https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5

Validation: 28 local tests passed (provider compatibility, cached-token budgeting, planning sessions, compiler gateway and OpenAI adapter regression checks). These do not establish live Sonnet 5.5 compatibility or answer quality.

Evaluation completed after VPN restoration on 2026-09-29. The user explicitly authorized a fresh additional $20 Claude and $20 OpenAI; separate round ledgers were used without resetting previous ledgers. Same frozen 57 questions and PanKgraph_08_04 as the saved comparison. Sonnet 5.5 is a sequential follow-on run, not interleaved with Sonnet 5.

| Measure | New / Sonnet 5 | Temporary new / Sonnet 5.5 |
|---|---:|---:|
| Positive core coverage | 44/53 | 47/53 |
| Core or verified empty | 46/55 | 49/55 |
| Advisory quality screen after manual review | 40/55 | 47/55 |
| Appropriate clarification | 2/2 | 2/2 |
| Matched preview median, 47 pairs | 7.31s | 6.20s |
| Planning + repair cost per attempt | $0.0357 | $0.0356 |
| Formatting cost per attempt | $0.0395 | $0.0412 |
| Total API cost per attempt | $0.0752 | $0.0768 |

Gained core: Q16, Q47, Q53; none lost. Core failures remain Q08, Q12, Q34, Q35, Q37 and Q42. Q11/Q19 still fail interpretation despite complete core retrieval. Keep the fork experimental pending repeated and held-out validation. Q17/Q57 clarification is verified; PostgreSQL overlap and the Sonnet 5.5 accepted-clarification continuation were not tested in this round.

The earlier new/Sonnet 5 quality score of 41/55 was revised to 40/55 after confirming an additional Q56 sample-annotation overclaim. No saved output changed. Quality screening is advisory; manual overrides preserve scope-qualified answers and do not require every returned record in prose. Full decisions and question-level evidence are in project-home `Research/Reports/pankagent-sonnet55-20260929/final/` (ignored research artifact, not part of this repo). Original-dev historical comparison remains 36/55 retrieval and 32/55 quality screen.

Fresh-round settled estimates: Claude $4.377017, OpenAI reviewer $1.990946; zero pending reservations. All 57 reviews completed. All 57 candidate runtime hashes match evaluated commit `33ec930a9ab39fa955851895f492589f1a881c50`, and all 57 comparator files match the earlier immutable artifacts. No deployment, frontend change, database write or active service change. The 28 local tests and live evaluation validate this temporary adapter; they do not prove a general model ranking.
