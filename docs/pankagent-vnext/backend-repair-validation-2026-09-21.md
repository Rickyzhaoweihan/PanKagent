# Backend repair validation — September 21, 2026

**Backend candidate implemented and tested; release acceptance remains blocked.**
The PR targets `codex/dev-functional-axis`. No dev deployment, Ringo promotion,
production change, or authentication removal has been performed.

## Exact code and test coverage

- Base: `4ddc90dca43a69c148231a19808547323ac6d679`.
- Final runtime code: `6f33747a96405d7ce6c4447eca49f383422b673a` on
  `codex/backend-audit-repair-20260921`; subsequent documentation commits do not
  change runtime code. Existing dirty checkouts were preserved.
- Final offline command: `python -m pytest tests_vnext tests_results tests_health tests_reliability -q`:
  **2,691 passed, four skipped, 269 subtests passed**. Loopback permission was
  needed for proxy/health tests. Earlier sandbox-only socket failures were
  rerun successfully; they were not application failures. Legacy `tests/`
  collection requires external configuration and is not included in this claim.
- Paid replay attempted all **16 authored questions**, **24 historical cases**
  with original wording/predecessors/duplicates, and **three fresh PLEKHM1
  repetitions**. Failures and targeted retests are retained in separate epochs.
  This is not a claim that all scientific answers passed, or that every paid
  case was rerun on the final commit. The complete
  [case matrix](backend-repair-case-results-2026-09-21.md) records tested commits,
  run IDs, exact questions, and remaining limitations.
- Private source corpus SHA256:
  `45a373dd283191ec2c83deb63bad672e845478ded4f27f536cd3daf22332ff33`.
  No 2,416-occurrence replay was run.

## Before / after findings

| Reproduced problem | Repair and observed result |
| --- | --- |
| Source, tissue or gene scope changed between dependent checks | Canonical scope checks and conservative query splitting; rs689 and ADCY3 retests completed. GSDMB uses the existing verified colocalization dataset/tissue mapping and an independent QTL tissue filter. No guessed path or disease-wide fallback. |
| INS/plural assay bundles and disease-qualified PTPN22 overviews failed planning | Independent supported checks plus registered, bounded historical overview forms; preserve disease restrictions only on compatible evidence and retain ATAC. |
| Skipped GWAS presented as zero; bounded empty GO query presented as failed | Distinct execution provenance for skipped, executed-empty, failed, truncated and context-omitted. Source 45 now reports only the executed bounded zero matches. |
| PTPN22 interactions or truncated INS evidence lost during compaction | Full-record canonical facts precede sampling. Paid PTPN22 retest retains 61 records / 52 partners. Audited INS fixtures retain 65 records / 62 partners with truncation qualified. |
| Unsupported prose, unstable citations and reversed comparison | Stable evidence IDs; model selects schema-constrained fact IDs; application validates and renders before answer SSE. Invalid selections yield a deterministic partial summary, without another judge. Explicit paid alpha-cell ATAC comparison correctly renders 49.6068 (T1D) lower than 52.9265 (ND); mean/median and ATAC/RNA remain distinct. |
| Misclassified source labels and PLEKHM1 entity roles | Canonical facts retain 13 Possible, 25 Moderate, 8 Strong, 8 Causal source labels and 202 unclassified records in the audited fixture; source labels are not causal proof. Three fresh PLEKHM1 graph records agree, with Gene → beta-cell roles and no unsupported hormone/cognate-cell assurances. BIM bundle 1.10.0 is hash-pinned and tested. |
| Aggregate cohort omitted the assay intersection and reported false zero samples | Required donor–sample scope is preserved. Unqueried sample counts are unknown. Final run `bb5a3e37-3c27-4015-8172-a59ceb60655f` on `6f33747` returns three qualifying stage-1 HPAP donors and 13 islet assay records; no donor/sample identifier patterns in inspected public run, result or SSE artifacts. |
| Five selected donors described as five contributors | Add finite contributor counts across the trace and per timepoint while retaining the old inventory count. Audited saved answer and mean-only plot report five selected / three contributing donors, unchanged 50 means, nine stimulus intervals, units and filters. Missingness regressions pass. |
| Literature-only requests required fake graph work; partial graph suppressed explicit literature | Confirmable literature-only mode and independent outcomes; exact question/history, bounded correlation hashes and allowlisted errors. IFIH1 literature completes while graph remains qualified partial. |
| “Disable literature” revision was overwritten by default enrichment | Final retest honors the preference, preserves original graph scope and preview, performs no literature call, and completes. Default enrichment remains enabled. Earlier failed attempt is retained. |
| Reopening results could be mistaken for re-execution | Inspected saved runs/results preserve answer identity and incur zero provider calls; historical answers are not regenerated. Additive/versioned metadata and cache identities cover the repaired contracts. |

The stage-1 cohort count (three donors / 13 assays) and the Functional Data trace
(five selected / three contributors) are different requests and denominators.
The final cohort test retained the original authored question and added the
explicit “Disable literature” revision; it is a graph-only lifecycle retest,
not a successful full literature rerun of that question.

Progress SSE remains live; inspected factual-answer SSE is buffered until
validation. Privacy projection precedes synthesis and public result construction;
offline checks cover graph/table/download projections. Identifier-pattern review
supplements those structural tests; it does not certify the source data itself.

## Scientific and operational limits by owner

| Owner | Remaining issue / release implication |
| --- | --- |
| HIRN upstream | PLEKHM1 literature relevance varies; human-primary restriction still includes cell-line/stem-cell models; claim citations and publication/model/assay metadata remain incomplete. Historical source 97 literature timed out while graph evidence survived. See the standalone handoff. |
| PanKagent capability / graph data owner | Genomic-neighborhood and gene-body analysis requires verified same-assembly variant/gene coordinates and a distance window. “Most specific” GO ranking lacks a defined ontology metric. Requests now return precise limitations, not substituted searches or fabricated results. These are not successful executed analyses. |
| PanKagent resources / data owner | Exact `GWAS_finemapping_V1` object mapping remains unverified. Resource stays explicitly unavailable; no guessed bucket alias was added. |
| Dev operator / access integration | The actual outer dev gate is unverified. Deployment records report Amplify Basic disabled; optional Cognito does not establish API protection. Removing application challenges would not meet the approved boundary. Authentication is unchanged. |
| Browser acceptance / frontend owner | Approved browser showed Functional Data still loading charts; the documented health route returned `ERR_BLOCKED_BY_CLIENT`. Rendered agent/tool/health acceptance is incomplete. Functional BMI data returned 106 observations, but its rendered interpretation remains unaccepted. |

ssGSEA execution, named-gene exclusions, new datasets and the future health domain
remain outside this repair. Their unavailable responses are explicit.

Audit-only HIRN provider dispatch was serialized to make worst-case reservations
fit the allocation. One IFIH1 run failed at the original 60-second deadline; an
isolated retest used 180-second agent/wrapper deadlines and 240-second harness
confirmation. Production defaults and upstream prompts/index/ranking were
unchanged. These are content/integration results, not production-latency claims.

## Spending and closure

| Allocation | Cap | Settled usage estimate | Retained uncertain reservations |
| --- | ---: | ---: | ---: |
| Targeted baseline | $2 | $0 | $0 |
| Candidate regressions | $10 | $7.88911823 | $0.02747130 |
| Controlled dev acceptance | $4 | $0 | $0 |
| Contingency / reproduced-defect retests | $4 | $2.20969525 | $0.02760015 |
| **New campaign** | **$20** | **$10.09881348** | **$0.05507145** |

Conservative total consumption is **$10.15388493**; remaining total allowance is
$9.84611507, still subject to the separate phase caps. No cap was raised or
unused prior allowance borrowed. Every physical Claude/OpenAI request and retry
reserved through an atomic shared admission check. Worst-case requests that did
not fit closed admissions; low actual spend did not authorize bypassing holds.
Four unresolved usage reservations remain. These are ledger estimates, not
invoice reconciliation.

The previous campaign remains separate at $12.43224284 settled and $0.10979610
uncertain, with 2,459 ledger rows. The new ledger has 2,205 rows. Paid admissions
are closed and all owned audit services are stopped; no monitoring continues.

## Rollback and deployment evidence

Candidate installation used immutable hash-verified releases and ownership-aware
managers on loopback 8894, 8895, 8896 and 18991. Final code release:
`/var/local/serviceuser/projects/pankgraph-demo/releases/20260921-repair-6f33747a9640/backend`
(784 archive files verified). Private logs use `/db/pankagent-vnext-private/`,
serviceuser ownership, 0700 directories, 0600 files and umask 077.

Private rollback inventory:
`/var/local/serviceuser/projects/pankgraph-demo/audits/backend-repair-20260921/rollback-final-code-config.private.json`.
It records retained release/managers, final ledger identities and the rechecked
live baseline:

| Service | Preserved PID | Preserved release / operation |
| --- | ---: | --- |
| Agent 8794 | 2344671 | `20260920-independent-f2c0bb81ef0d/backend` |
| Results 8795 | 2344716 | `20260920-functional-4ddc90dca43a/backend` |
| Health 8796 | 2344772 | `health-logging-20260920` |

No controlled dev rollout occurred, so no dev rollback was executed. The artifact
is a recorded recovery inventory, not a passed rollback rehearsal. Any future
rollback must restore code/configuration only and preserve the current ledger,
unknown reservations, durable sessions and health log-rotation fix. Production
and shared graph services were not restarted.

The prior automatic approval review rejected an IGV external-script exception
because that script could access private saved results. No exception or alternate
route was used to bypass that restriction. A draft PR is reviewable now; dev
activation remains gated on verified access and approved browser acceptance.

The [HIRN handoff](hirn-api-handoff-2026-09-20.md) is separate and shareable. It
contains no donor records, credentials or raw service logs and has not been sent
to another team automatically.
