import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os

import pytest

from pankagent_vnext.grounding_inventory import (
    build_inventory,
    inventory_envelope_digest,
    inventory_identity,
    load_inventory,
    write_inventory,
)
from pankagent_vnext.preplanning_grounding import Grounder
from pankagent_vnext.preplanning_grounding import ground_question, grounding_guidance
from test_preplanning_grounding import FakeGraph


def test_consistent_disk_inventory_remains_governed_by_ttl(tmp_path):
    graph = FakeGraph()
    inventory = asyncio.run(build_inventory(graph))
    stale = deepcopy(inventory)
    stale["built_at"] = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    stale["envelope_digest"] = inventory_envelope_digest(stale)
    assert stale["content_digest"] == inventory["content_digest"]
    path = tmp_path / "public-entities.json"
    write_inventory(path, stale)

    with pytest.raises(ValueError, match="stale_or_invalid"):
        load_inventory(path, inventory_identity(graph))
    assert load_inventory(path, inventory_identity(graph), max_age_seconds=600)[
        "content_digest"
    ] == inventory["content_digest"]


def test_disk_inventory_rejects_partial_timestamp_edit_without_digest_update(tmp_path):
    graph = FakeGraph()
    inventory = asyncio.run(build_inventory(graph))
    inventory["built_at"] = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    inventory["envelope_digest"] = inventory_envelope_digest(inventory)
    tampered = deepcopy(inventory)
    tampered["built_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert tampered["envelope_digest"] != inventory_envelope_digest(tampered)
    path = tmp_path / "public-entities.json"
    write_inventory(path, tampered)

    with pytest.raises(ValueError, match="stale_or_invalid"):
        load_inventory(path, inventory_identity(graph), max_age_seconds=600)


@pytest.mark.parametrize("built_at", [None, "not-a-timestamp", "2099-01-01T00:00:00+00:00"])
def test_disk_inventory_rejects_missing_invalid_or_future_build_time(tmp_path, built_at):
    graph = FakeGraph()
    inventory = asyncio.run(build_inventory(graph))
    inventory["built_at"] = built_at
    inventory["envelope_digest"] = inventory_envelope_digest(inventory)
    path = tmp_path / "public-entities.json"
    write_inventory(path, inventory)

    with pytest.raises(ValueError, match="stale_or_invalid"):
        load_inventory(path, inventory_identity(graph))


@pytest.mark.parametrize("mode", [0o640, 0o606, 0o666])
def test_disk_inventory_rejects_group_or_other_permissions(tmp_path, mode):
    graph = FakeGraph()
    inventory = asyncio.run(build_inventory(graph))
    path = tmp_path / "public-entities.json"
    write_inventory(path, inventory)
    path.chmod(mode)

    with pytest.raises(ValueError, match="stale_or_invalid"):
        load_inventory(path, inventory_identity(graph))


def test_disk_inventory_rejects_symlink_nonregular_and_wrong_owner(tmp_path, monkeypatch):
    graph = FakeGraph()
    inventory = asyncio.run(build_inventory(graph))
    target = tmp_path / "owned-private-catalog.json"
    write_inventory(target, inventory)

    symlink = tmp_path / "catalog-link.json"
    symlink.symlink_to(target)
    with pytest.raises(ValueError, match="stale_or_invalid"):
        load_inventory(symlink, inventory_identity(graph))

    with pytest.raises(ValueError, match="stale_or_invalid"):
        load_inventory(tmp_path, inventory_identity(graph))

    effective_uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: effective_uid + 1)
    with pytest.raises(ValueError, match="stale_or_invalid"):
        load_inventory(target, inventory_identity(graph))


def test_grounder_rebuilds_cache_with_unsafe_permissions(tmp_path):
    graph = FakeGraph()
    inventory = asyncio.run(build_inventory(graph))
    path = tmp_path / "public-entities.json"
    write_inventory(path, inventory)
    path.chmod(0o666)
    calls_before = len(graph.calls)

    index = asyncio.run(Grounder(graph, path).warm())

    assert index.content_digest == inventory["content_digest"]
    assert len(graph.calls) > calls_before
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_inventory(path, inventory_identity(graph))["content_digest"] == inventory["content_digest"]


def test_in_memory_grounder_rebuilds_after_bounded_ttl():
    graph = FakeGraph()
    grounder = Grounder(graph, ttl_seconds=0)

    async def scenario():
        first = await grounder.warm()
        first_call_count = len(graph.calls)
        second = await grounder.warm()
        assert second is not first
        assert len(graph.calls) > first_call_count
        # Collection time is intentionally excluded from the semantic digest.
        assert second.content_digest == first.content_digest

    asyncio.run(scenario())


def test_grounder_rejects_unbounded_or_invalid_ttl():
    graph = FakeGraph()
    for value in (-1, True, None, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="invalid_grounding_inventory_ttl"):
            Grounder(graph, ttl_seconds=value)


def test_inventory_forces_live_semantic_refresh_and_excludes_private_clinical_values():
    class ClinicalGraph(FakeGraph):
        def __init__(self):
            super().__init__()
            self.semantic_force_values = []

        async def semantic_vocabulary(self, force=False):
            self.semantic_force_values.append(force)
            value = await super().semantic_vocabulary()
            value.update({
                'donor_categorical_values': {
                    'diabetes_type': ['PRIVATE_DIABETES_SENTINEL'],
                    't1d_stage': ['PRIVATE_STAGE_SENTINEL'],
                },
                'donor_diseases': [{'id': 'PRIVATE_DISEASE_SENTINEL',
                                    'name': 'private disease'}],
            })
            return value

    graph = ClinicalGraph()
    inventory = asyncio.run(build_inventory(graph))
    serialized = str(inventory)
    assert graph.semantic_force_values == [True]
    assert 'PRIVATE_DIABETES_SENTINEL' not in serialized
    assert 'PRIVATE_STAGE_SENTINEL' not in serialized
    assert 'PRIVATE_DISEASE_SENTINEL' not in serialized
    payload = asyncio.run(ground_question(graph, 'Find spleen RNA samples.'))
    guidance = grounding_guidance(payload)
    assert 'PRIVATE_' not in str(payload)
    assert 'PRIVATE_' not in guidance
