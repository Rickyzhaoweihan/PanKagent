# Planner domain prompt catalog

`pankagent_vnext/planning_prompt_catalog.py` lists MODULES in load order. Both
normal grounded planning and grounding-timeout fallback load the same catalog.
The rendered module contents participate in the grounded plan cache digest.

| Module | Editable source | Responsibility |
| --- | --- | --- |
| colocalization | `../../coloc_planning_guidance.py` | Gene-to-disease topology and exact signal linkage |
| donor_samples | `donor_samples.md` | Donor-ID follow-ups, HAS_SAMPLE direction, typed input bindings and counts |
| sample_modalities | `modalities.json` | Recorded label and terminology hints |

To add a modality:
1. Verify its label and property owner against the deployed graph release.
2. Add its exact label and unambiguous spelling variants to modalities.json.
   Do not encode biological capability expansions as spelling aliases.
3. Run tests_vnext/test_planning_prompt_catalog.py and relevant semantic tests.
4. Replay a donor-to-sample request and compare distinct sample IDs/counts with
   a reference read-only query before claiming end-to-end support.

Adding a new modality to the same Sample_node.data_modality field does not
require another donor/sample workflow or formatter changes. A different graph
shape or a cross-modality capability (for example RNA components of multiome)
requires a reviewed execution/semantic rule, not only an entry in this list.
The catalog is prompt guidance; runtime inventory and request authorization
remain authoritative. It does not create backend bindings to previous runs.
When complete verified IDs are not supplied, the planner must retrieve the
prior cohort under all preserved filters instead of reading displayed IDs.

Validation 2026-09-23: 29 focused tests passed. A fresh fallback planning call
using the saved HPAP Stage 2 conversation produced a valid donor source binding
and one HAS_SAMPLE sample query. Incremental paid validation cost $0.032948.
This validates planning output; a complete new follow-up answer/browser replay
was not performed in this change. The prior direct graph check found one exact
scRNA-seq sample for the retained donor, but that is not a lifecycle replay.
Implementation commit 47acc61; private validation and deployment evidence:
`/db/pankagent-vnext-private/operations/sample-guidance/`.
