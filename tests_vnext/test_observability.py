"""Metrics are cumulative while quantiles remain bounded and usage is durable."""

import json

from pankagent_vnext.budget import Budget
from pankagent_vnext.health import Metrics


def metric_values(metrics, budget=None):
    return {name: float(value) for name, value in (line.rsplit(" ", 1) for line in metrics.render({}, budget or {}).splitlines())}


def test_duration_counters_do_not_roll_back_with_quantile_window():
    metrics = Metrics()
    for seconds in range(1, 10006):
        metrics.observe("graph_answer", float(seconds))
    metrics.observe("plan_ready", 2.0)
    values = metric_values(metrics)
    assert len(metrics.durations["graph_answer"]) == 10000
    assert metrics.durations["graph_answer"][0] == 6.0
    assert values['pankagent_stage_seconds_count{stage="graph_answer"}'] == 10005
    assert values['pankagent_stage_seconds_sum{stage="graph_answer"}'] == 10005 * 10006 / 2
    assert values['pankagent_stage_seconds{stage="graph_answer",quantile="0.5"}'] == 5005
    assert values['pankagent_stage_seconds_count{stage="plan_ready"}'] == 1
    assert values['pankagent_stage_seconds_sum{stage="plan_ready"}'] == 2
    metrics.observe("graph_answer", 0.5)
    later = metric_values(metrics)
    assert later['pankagent_stage_seconds_count{stage="graph_answer"}'] == 10006
    assert later['pankagent_stage_seconds_sum{stage="graph_answer"}'] == 10005 * 10006 / 2 + 0.5


def test_settled_token_totals_survive_restart_and_ignore_pending_reservations(tmp_path):
    path = tmp_path / "budget.sqlite3"
    budget = Budget(path, 10)
    first = budget.reserve("claude-sonnet-5", "plan", 10000, 2000)
    second = budget.reserve("claude-haiku-4-5-20251001", "synthesis", 10000, 2000)
    pending = budget.reserve("claude-sonnet-5", "plan", 10000, 2000)
    usage = {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 50}
    budget.settle(first, usage)
    budget.settle(second, {"input_tokens": 150, "output_tokens": 25, "cache_read_input_tokens": 70, "cache_creation_input_tokens": 10})
    with budget._db() as db:
        db.execute("UPDATE usage SET tokens=? WHERE id=?", (json.dumps({"input_tokens": 99999}), pending))
    snapshot = Budget(path, 10).snapshot()
    assert {key: snapshot[key] for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")} == {
        "input_tokens": 250, "output_tokens": 45, "cache_read_tokens": 270, "cache_creation_tokens": 60,
    }
    assert snapshot["pending_calls"] == 1
    assert snapshot["reserved_usd"] > 0
    rendered = metric_values(Metrics(), snapshot)
    assert rendered["pankagent_input_tokens"] == 250
    assert rendered["pankagent_output_tokens"] == 45
    assert rendered["pankagent_cache_read_tokens"] == 270
    assert rendered["pankagent_cache_creation_tokens"] == 60
    # Correcting a settled record replaces its usage, rather than counting it twice.
    budget.settle(first, {**usage, "input_tokens": 120, "output_tokens": 22})
    corrected = Budget(path, 10).snapshot()
    assert corrected["input_tokens"] == 270
    assert corrected["output_tokens"] == 47


def test_bad_usage_metadata_does_not_break_financial_snapshot(tmp_path):
    budget = Budget(tmp_path / "budget.sqlite3", 10)
    first = budget.reserve("claude-sonnet-5", "plan", 10000, 2000)
    second = budget.reserve("claude-sonnet-5", "synthesis", 10000, 2000)
    budget.settle(first, {"input_tokens": 100, "output_tokens": 20})
    budget.settle(second, {"input_tokens": 150, "output_tokens": 25})
    before = budget.snapshot()
    with budget._db() as db:
        db.execute("UPDATE usage SET tokens=? WHERE id=?", ("malformed historical metadata", first))
    after = budget.snapshot()
    assert after["spent_usd"] == before["spent_usd"]
    assert after["remaining_usd"] == before["remaining_usd"]
    assert after["input_tokens"] == 150
    assert after["output_tokens"] == 25
