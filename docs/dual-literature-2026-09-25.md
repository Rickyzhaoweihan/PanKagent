# Dual literature and early follow-up

Dev implementation adds independent HIRN and GLKB literature sections after the graph answer. HIRN retains its current upstream contract. GLKB uses the loopback Luna chat API, requests complementary mechanisms, alternatives, supported controversies and evidence gaps, and returns its upstream answer without a second synthesis call. Each GLKB request uses a fresh upstream session and bounded context; no automatic POST retries are enabled.

The run's literature object adds `sources.hirn`, `sources.glkb` and an append-only reference registry with numeric display identities and source provenance. Legacy `perspectives` remains readable. New SSE events `literature_sources` and `followup_ready` are additive. Graph evidence markers retain their separate meaning. Reference counts are deduplicated by PMID/DOI; source-local references do not merge by title.

`POST /v2/plans` accepts an optional same-session `parent_run_id`. Its completed graph and available literature are snapshotted atomically in run audit metadata. The additive SQLite `followup_ready` column allows follow-up while the original run remains active. Background source waits use their own bounded pool and are excluded from the graph admission queue. Old sources continue updating their original run and cannot change an already-created child context. Interrupted work is not automatically resubmitted.

Frontend changes stay within the existing vNext components: two source sections with accessible information popovers; GLKB's popover links to https://glkb.org/; HIRN provenance appears at the bottom right of the reference item. Citations are ordinary numbers and use run-scoped anchors. Previous turns continue receiving events while the new question runs.

Validation before deployment:
- New offline backend contract/concurrency/snapshot tests pass; runtime and literature tests on the staged server: 45 passed.
- Full backend run: 2814 passed, 5 skipped, 231 subtests passed, with 19 pre-existing failures reproduced on unchanged 5c5cf2b. The former whole-execution AST freeze was updated to retain the graph execution/recovery block while permitting the requested literature orchestration changes; focused post-update tests: 44 passed.
- Frontend vNext suite with real Markdown transforms: 249 passed; production build passes (existing lint/dependency warnings).

Deployment is limited to agent 8794 and the existing xuteng/react dev frontend pipeline. Results 8795, health 8796, GPT comparison 8798, upstream literature services and production are not restarted. Preserve owner-only backups/logs under `/db/pankagent-vnext-private`. Roll back code via `deploy_reliability.manage agent stop/start --release ...`; do not restore old session/budget databases. The prior backend is `20260925-public-evidence/backend` and prior dev frontend commit is `fbf8e96`.

## Deployed acceptance (2026-09-25)

Backend code `750add8` is running from `20260925-dual-literature-v2/backend`; frontend `7f35dce` deployed successfully through Amplify job 814 on `xuteng/react`. The additional reference-metadata regression test passes (9 dual-literature tests). Agent readiness and both literature components report healthy; the pre-existing results process remains unchanged.

Live graph run `7bc3cb6a-a860-4d71-a566-7da638bacedc` returned graph evidence before literature, then completed both sources with 9 unique numeric references. Live literature run `061522b2-8bc3-418c-a7e3-380e879dc20f` accepted a same-session follow-up while one source was still pending; the follow-up resolved INS from the parent context. Offline lifecycle tests cover background completion on the original turn and immutable child snapshots.

Browser acceptance confirmed separate HIRN/GLKB headings, numeric citation scrolling/highlighting, HIRN text at the bottom-right of individual reference cards, and the GLKB tooltip linking to `https://glkb.org/` with `target=_blank`. This verifies integration and citation linkage, not an independent scientific audit of generated claims.
