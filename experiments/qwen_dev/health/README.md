# Health dashboard update

Deployed to the existing standalone health service on 8796. These replace pankgraph_health/simple_agent_view.py and pankgraph_health/web/dashboard.js in health-simple-agent-20261013-v2; they are not application frontend files.

The Qwen candidate health endpoint and authenticated /v1/models through 33924 are polled without inference. Model identity must equal pank-candidate. Combined relay/model reachability is separate from agent liveness and deterministic renderer configuration. No GPU utilization, Slurm expiry or scientific correctness is inferred. Historical Claude/Cypher probes remain optional. Old incident records are retained; old query_loop history predates this architecture change.

Verified: live snapshot architecture qwen-native-two-loop-direct-lighthouse, stale=false; Qwen agent, model/relay and renderer healthy. Correct and mismatched model identity cases tested; Python and JavaScript syntax passed. Public /health/api/snapshot remains authentication protected (anonymous 401). Updated script served by local dashboard. Only dashboard supervisor restarted. Its log compatibility link was preserved; target log directory/file permissions restored to 0700/0600 as required by supervisor.

Back up both files before replacement; restore them and restart only the dashboard supervisor for rollback. No agent/results/model restart is required. Existing authentication configuration and historical observations are unchanged.
