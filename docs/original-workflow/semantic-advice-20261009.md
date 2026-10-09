# Non-blocking semantic diagnostics

Candidate implementation; not deployed. The existing dev release remains the narrower planner-authority patch.

Python semantic findings are retained as `python_diagnostics` with phase, original diagnostic code, `blocking: false`, and model-review disposition. Recovery explanations and suggested corrections remain structured detail. The planner receives new preparation findings once within its existing allowance; it can repair, clarify or retain its choice. Repeated findings do not force exhausted-retry rejection. Model-requested clarification remains effective.

Scope compilers preserve their input when a helper reports failure. Preparation/entity/binding advice no longer rejects the task. Cypher semantic findings no longer suppress database execution. Template failure can fall through to the existing writer. Returned-evidence and path-transport findings preserve retrieved records for interpretation. The formatter receives diagnostics alongside evidence, with instructions to assess grounding and uncertainty. Queries with semantic findings are not stored as verified cache entries.

Operational boundaries remain: parseable model/tool structures, read-only execution, parameter availability, graph identity, actual executable dependencies, database errors, cancellation, deadlines and resource/API budgets. These describe execution availability rather than judging user meaning. Missing data or failed operations are never manufactured as successful evidence. This change does not make unsupported backend operations executable.

No database-specific facts were added to Python or to a fifth schema. No frontend/model change, no new model stage, and no paid API calls. Existing allowance bounds the optional planner review.

Validation evidence: offline mocked-provider and database fixtures; see this round's test logs. No live-model or deployed end-to-end claim. Historical tests expecting semantic rejection have been adjusted to assert diagnostic retention and model authority.

Offline validation: 115 tests and 17 subtests passed; after the path-evidence adjustment, all 10 diagnostic tests passed, including template fallback and retention of raw path records. No full historical-suite or live-model evaluation was run. Logs and rendered workflow are retained in Research/Reports/pankagent-semantic-advice-20261009/.
