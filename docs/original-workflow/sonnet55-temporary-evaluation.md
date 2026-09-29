# Temporary Sonnet 5.5 evaluation fork

User-authorized temporary fork of new-agent commit `91e4a434090aab497cfe12ee4745ff87d063245a`, on `codex/sonnet55-eval`. Original dev and `codex/workflow57-review` stay unchanged. No deployment or frontend changes.

The model is exactly `claude-sonnet-5-5`. Compatibility changes are limited to the model allowlist, $2/$10 per-million input/output pricing with existing caching accounting, `thinking.type=between_tools`, explicit high effort, and conversion of rejected forced tool selections to `auto`. Existing tool schemas, response validation, bounded retries, prompts, four database schemas and query workflow remain intact. Thinking blocks already pass through unchanged in the append-only planning conversation. No model fallback is enabled.

Official references:
- https://platform.claude.com/docs/en/models/sonnet-5-5/overview
- https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5

Validation: 28 local tests passed (provider compatibility, cached-token budgeting, planning sessions, compiler gateway and OpenAI adapter regression checks). These do not establish live Sonnet 5.5 compatibility or answer quality.

Evaluation protocol: same frozen 57 questions, new-agent workflow, same graph release and execution limits as the 2026-09-29 original/new comparison. Use the existing shared round ledgers, not a new $20 allowance: before this fork, Claude settled $9.336391 of $20, and OpenAI settled $2.520423 plus $0.214643 retained reservations of $20. Compare with the saved original/Sonnet 5 and new/Sonnet 5 results. Label timing as a sequential follow-on run, not interleaved simultaneous testing.

Live evaluation is pending: the SSH evaluation host timed out before source upload or any Sonnet 5.5 API call. No measured Sonnet 5.5 score, cost or speed result is claimed.
