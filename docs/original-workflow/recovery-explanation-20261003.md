# Recovery explanation quick fix — 2026-10-03

The stage-4 user run adc88c34-d3b7-42b6-a7a5-491db11b72f3 had valid preparation diagnostics, but exhausted planning retries replaced the specific recovery with generic E04 text. The final failure now retains the server-prepared recovery message and suggestions. Raw model exceptions are not displayed.

Stage recovery lists all recorded stages in natural language and orders its two revision choices by numeric proximity, without automatically changing the requested stage. For stage 4, the first choice is “Use recorded stage 3”; stages 1, 2 and 3 are listed in the description. Existing dialog/frontend unchanged. This preserves available explanations generally; it does not invent an explanation when preparation has none.

Validation: 75 focused tests and 38 subtests passed on canonical and live-base code. The new regression runs planning retries and verifies the failed run API retains the explanation and stage-3 suggestion. Isolated real-graph preparation also passes through the full retry loop with mocked model output. No paid API calls, no fresh browser/model end-to-end claim. No full-suite rerun for this narrowly scoped quick fix.

Only three backend files deployed; previous schema 2.2.2 and Sonnet 5.5 retained. Verified backup preceded activation; results service and frontend preserved. Full introduction candidate remains inactive. Diagram tooltip/provenance updated; visible labels, Error/Schema annotations and layout unchanged.

Code: `0b6c8005fd9c911904cfdd169f5d93b9647f82eb`. Release: `/var/local/serviceuser/projects/pankgraph-demo/releases/20261003-recovery-explanation-0b6c800/backend`. Application SHA256: `cf3910f8026f3ffc0f3105bcf846105b68ad8f4811cab8867776035e9a2f6ead`.

Private evidence: `/db/pankagent-vnext-private/operations/recovery-explanation-20261003/`.
