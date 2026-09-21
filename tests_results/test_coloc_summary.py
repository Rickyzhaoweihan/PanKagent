"""Coloc summaries reuse the shared answer pipeline without new retrieval policy."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from pankagent_vnext.answer_router import AnswerSkillRouter
from pankagent_vnext.llm import ClaudeGateway, STYLE_VERSION
from pankgraph_results.coloc_summary import summary_identity, summary_source
from pankgraph_results.store import ResultStore
from tests_results.test_app import FakeGateway, finished, service
from tests_results.test_coloc import FakeQuery, FakeResources, core, member, no_coordinates, tsv

PREFIX = "/pankgraph-vnext/api/coloc/records"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SharedGateway(FakeGateway):
    """Use the real shared compiler/router with an offline provider stream."""
    def __init__(self, chunks=("The recorded comparison supports a shared signal, not proof of a causal mechanism [G1].",)):
        super().__init__()
        self.answer_router = AnswerSkillRouter()
        self.chunks = chunks
        self.prepared = None
        self.evidence = None
        self.gate = None
        self.error = None

    prepare_answer = ClaudeGateway.prepare_answer

    async def synthesize(self, question, evidence, *, prepared=None):
        self.calls += 1
        self.evidence = copy.deepcopy(evidence)
        self.prepared = prepared
        assert prepared is not None
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        for chunk in self.chunks:
            yield chunk


async def selected(s):
    s.runtime.coloc.coordinates = no_coordinates
    response = await s.client.get(PREFIX)
    assert response.status_code == 200
    rid = response.json()["records"][0]["id"]
    response = await s.client.get(PREFIX + "/" + rid)
    assert response.status_code == 200
    return rid, response.json()


async def submit(s, rid):
    response = await s.client.post(PREFIX + "/" + rid + "/summary")
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.anyio
async def test_explicit_post_reuses_detail_shared_router_and_result_polling(tmp_path):
    gateway, query, resources = SharedGateway(), FakeQuery(), FakeResources(tmp_path)
    async with service(tmp_path, query=query, resources=resources, gateway=gateway) as s:
        rid, detail = await selected(s)
        reads, downloads = len(query.calls), len(resources.calls)
        assert gateway.calls == 0
        initial = await submit(s, rid)
        assert initial["status"] == "ready"
        assert initial["component_status"] == {"graph": "available", "layout": "not_requested", "resources": "not_requested", "answer": "pending"}
        result = await finished(s.client, initial["result_id"])
        assert result["component_status"]["answer"] == "available"
        assert result["answer_validation"]["valid"]
        assert result["source"]["record_id"] == rid
        assert result["answer_profile"]["bundle_sha256"] == gateway.answer_router.bundle_hash
        assert result["answer_configuration"]["style_version"] == STYLE_VERSION
        rules = {rule["id"] for rule in gateway.prepared.profile["selected_rules"]}
        assert {"edge.signal_coloc_with", "edge.part_of_gwas_signal", "edge.part_of_qtl_signal"} <= rules
        assert "proof of a causal" in json.loads(gateway.prepared.body)["question"]
        assert gateway.calls == 1 and len(query.calls) == reads and len(resources.calls) == downloads
        assert s.layout.calls == 0 and s.upstream_calls == []
        with s.runtime.store.db() as db:
            events = {row[0] for row in db.execute("SELECT kind FROM audit_events WHERE result_id=?", (initial["result_id"],))}
        assert {"coloc_summary_requested", "answer_profile"} <= events
        assert (await s.client.get(PREFIX + "/" + rid)).status_code == 200
        assert gateway.calls == 1  # A subsequent detail GET never regenerates it.


@pytest.mark.anyio
async def test_parallel_posts_and_reopened_store_reuse_one_scientific_identity(tmp_path):
    gateway = SharedGateway()
    gateway.gate = asyncio.Event()
    async with service(tmp_path, query=FakeQuery(), resources=FakeResources(tmp_path), gateway=gateway) as s:
        rid, detail = await selected(s)
        first, second = await asyncio.gather(submit(s, rid), submit(s, rid))
        assert first["result_id"] == second["result_id"]
        for _ in range(30):
            if gateway.calls:
                break
            await asyncio.sleep(.005)
        assert gateway.calls == 1
        # Presentation timestamps, source-check clocks, and download routing
        # cannot spend a second inference budget for identical observations.
        cached = s.runtime.coloc._summary_snapshots[rid][1]
        cached["checked_at"] = "2099-01-01"
        for source in cached["sources"]:
            source["checked_at"] = "2099-01-02"
            source["download_url"] = "/a-different-presentation-prefix"
        s.runtime.store = ResultStore(s.runtime.settings.state_dir)
        s.runtime.settings.max_queue = 0
        s.runtime.settings.max_concurrent = 1
        repeated = await submit(s, rid)
        assert repeated["result_id"] == first["result_id"]
        assert gateway.calls == 1
        gateway.gate.set()
        await finished(s.client, first["result_id"])


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["posterior", "source_hash", "member", "model", "bundle", "style"])
async def test_scientific_or_shared_answer_version_change_invalidates_identity(tmp_path, monkeypatch, change):
    gateway = SharedGateway()
    async with service(tmp_path, query=FakeQuery(), resources=FakeResources(tmp_path), gateway=gateway) as s:
        rid, detail = await selected(s)
        first = await submit(s, rid)
        await finished(s.client, first["result_id"])
        cached = s.runtime.coloc._summary_snapshots[rid][1]
        if change == "posterior":
            cached["record"]["posteriors"].update(h3=.184, h4=.81)
        elif change == "source_hash":
            cached["sources"][0]["sha256"] = "f" * 64
        elif change == "member":
            cached["variants"][0]["qtl"]["pip"] = .6
        elif change == "model":
            s.runtime.vnext.model = "claude-haiku-4-5-20251001"
        elif change == "bundle":
            monkeypatch.setattr(gateway.answer_router, "bundle_hash", "f" * 64)
        else:
            monkeypatch.setattr("pankgraph_results.coloc_summary.STYLE_VERSION", "another-shared-style")
        second = await submit(s, rid)
        assert second["result_id"] != first["result_id"]
        await finished(s.client, second["result_id"])
        assert gateway.calls == 2


@pytest.mark.anyio
async def test_evidence_preserves_denominators_exact_record_and_both_study_top_rows(tmp_path):
    gwas = [member("rs" + str(i), size=12, pip=i / 100) for i in range(1, 13)]
    query = FakeQuery(gwas=gwas, qtl=[member(role="qtl", size=2)])
    # A file-only QTL member must stay tabular, never become a Neo4j edge.
    resources = FakeResources(tmp_path, raw=tsv(("rs1", "rs99")))
    async with service(tmp_path, query=query, resources=resources, gateway=SharedGateway()) as s:
        rid, detail = await selected(s)
        initial = await submit(s, rid)
        result = await finished(s.client, initial["result_id"])
        evidence = s.gateway.evidence
        record_rows = evidence["recorded_coloc"]["rows"]
        assert record_rows[0]["id"] == rid
        assert record_rows[0]["nsnp"] == 1273
        assert record_rows[1]["gwas_count"] == 12
        assert record_rows[1]["qtl_count"] == 2
        assert record_rows[1]["shared_count"] == 1
        assert record_rows[1]["variant_count"] == 13
        gwas_rows, qtl_rows = evidence["gwas_credible_set"]["rows"], evidence["qtl_credible_set"]["rows"]
        assert gwas_rows[0]["returned"] == 12 and gwas_rows[0]["members_not_shown_in_top_rows"] == 7
        assert [row["variant_id"] for row in gwas_rows[1:]] == ["rs12", "rs11", "rs10", "rs9", "rs8"]
        assert {row["variant_id"] for row in qtl_rows[1:]} == {"rs1", "rs99"}
        assert all(edge["start_id"] != "rs99" for edge in evidence["recorded_coloc"]["edges"])
        assert all(not evidence[step]["edges"] for step in ("gwas_credible_set", "qtl_credible_set", "source_provenance"))
        body = json.loads(s.gateway.prepared.body)
        assert body["evidence"][0]["rows"][0]["nsnp"] == 1273
        assert body["evidence"][1]["rows"][0]["returned"] == 12
        assert body["evidence"][2]["rows"][0]["returned"] == 2
        assert result["answer_configuration"]["bundle_sha256"] == s.gateway.answer_router.bundle_hash


@pytest.mark.anyio
async def test_unavailable_file_preserves_primary_record_and_partial_membership(tmp_path):
    from pankgraph_results.resources import ResourceError
    async with service(tmp_path, query=FakeQuery(), resources=FakeResources(tmp_path, error=ResourceError("denied")), gateway=SharedGateway()) as s:
        rid, detail = await selected(s)
        result = await finished(s.client, (await submit(s, rid))["result_id"])
        evidence = s.gateway.evidence
        assert result["completeness"] == "partial"
        assert evidence["recorded_coloc"]["rows"][0]["posteriors"]["h4"] == .8
        summary = evidence["qtl_credible_set"]["rows"][0]
        assert summary["returned"] == 1 and summary["expected"] == 2 and not summary["complete"]
        assert "unavailable" in json.dumps(evidence["source_provenance"])


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["empty", "exception", "invalid_citation"])
async def test_shared_answer_failures_are_durable_without_paid_implicit_retry(tmp_path, mode):
    gateway = SharedGateway(chunks=() if mode == "empty" else ("Supported text [G999].",) if mode == "invalid_citation" else ())
    if mode == "exception":
        gateway.error = RuntimeError("synthetic provider failure")
    async with service(tmp_path, query=FakeQuery(), resources=FakeResources(tmp_path), gateway=gateway) as s:
        rid, detail = await selected(s)
        initial = await submit(s, rid)
        result = await finished(s.client, initial["result_id"])
        assert result["status"] == "ready"
        assert result["component_status"]["graph"] == "available"
        assert result["component_status"]["answer"] == ("partial" if mode == "invalid_citation" else "unavailable")
        assert "G999" not in result["answer"]
        assert (await submit(s, rid))["result_id"] == initial["result_id"]
        assert gateway.calls == 1


@pytest.mark.anyio
async def test_unknown_empty_catalog_and_failed_retrieval_do_not_schedule_inference(tmp_path):
    query = FakeQuery(catalog=[])
    async with service(tmp_path, query=query, resources=FakeResources(tmp_path), gateway=SharedGateway()) as s:
        for identity in ("bad", "a" * 64):
            assert (await s.client.post(PREFIX + "/" + identity + "/summary")).status_code == 404
        query.fail.add("catalog")
        s.runtime.coloc._catalog_time = 0
        assert (await s.client.post(PREFIX + "/" + "a" * 64 + "/summary")).status_code == 503
        assert s.gateway.calls == 0 and s.runtime.tasks == {}


@pytest.mark.anyio
async def test_queue_admission_cancel_and_restart_keep_shared_job_semantics(tmp_path):
    gateway = SharedGateway()
    gateway.gate = asyncio.Event()
    async with service(tmp_path, query=FakeQuery(), resources=FakeResources(tmp_path), gateway=gateway) as s:
        rid, detail = await selected(s)
        s.runtime.settings.max_concurrent = 1
        s.runtime.settings.max_queue = 0
        initial = await submit(s, rid)
        assert (await submit(s, rid))["result_id"] == initial["result_id"]
        s.runtime.coloc._summary_snapshots[rid][1]["record"]["posteriors"].update(h3=.094, h4=.9)
        assert (await s.client.post(PREFIX + "/" + rid + "/summary")).status_code == 429
        task = s.runtime.tasks[initial["result_id"]]
        while gateway.calls == 0:
            await asyncio.sleep(.005)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        result = await finished(s.client, initial["result_id"])
        assert result["component_status"]["answer"] == "cancelled"
        assert s.runtime.active == 0
        # Service restart marks pending answers interrupted without scheduling.
        s.runtime.store.update(initial["result_id"], component_status={"answer": "pending"})
        s.runtime.store.interrupt()
        assert s.runtime.store.get(initial["result_id"])["component_status"]["answer"] == "interrupted"
        cached_time, _ = s.runtime.coloc._summary_snapshots[rid]
        s.runtime.coloc._summary_snapshots[rid] = (cached_time, detail)
        assert (await submit(s, rid))["result_id"] == initial["result_id"]
        assert gateway.calls == 1


@pytest.mark.anyio
async def test_authentication_and_cross_site_post_policy_unchanged(tmp_path):
    gateway = SharedGateway()
    async with service(tmp_path, query=FakeQuery(), resources=FakeResources(tmp_path), gateway=gateway, testing=False) as s:
        path = PREFIX + "/" + "a" * 64 + "/summary"
        assert (await s.client.post(path)).status_code == 401
        assert (await s.client.post(path, auth=("demo", "synthetic-test-password"), headers={"Origin": "https://untrusted.invalid"})).status_code == 403
        assert gateway.calls == 0 and s.query.calls == []


@pytest.mark.anyio
@pytest.mark.parametrize("exhausted", [False, True])
async def test_actual_shared_gateway_reserves_same_budget_and_preserves_exhaustion(tmp_path, monkeypatch, exhausted):
    from pankagent_vnext.budget import Budget
    from pankagent_vnext.config import Settings
    provider_calls = []

    class Stream:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        @property
        def text_stream(self):
            async def tokens():
                yield "The recorded comparison supports a shared signal [G1]."
            return tokens()
        async def get_final_message(self):
            return SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(
                model_dump=lambda: {"input_tokens": 100, "output_tokens": 20}))

    class Client:
        def __init__(self, **kwargs):
            self.messages = self
        def stream(self, **kwargs):
            provider_calls.append(kwargs)
            return Stream()
        async def close(self):
            pass

    monkeypatch.setattr("pankagent_vnext.llm.anthropic.AsyncAnthropic", Client)
    settings = Settings(state_dir=tmp_path / "shared-ledger", anthropic_key="synthetic-test-key")
    gateway = ClaudeGateway(settings)
    if exhausted:
        gateway.budget.limit = 0
    observer = Budget(settings.state_dir / "budget.sqlite3", settings.budget_usd)
    async with service(tmp_path, query=FakeQuery(), resources=FakeResources(tmp_path), gateway=gateway) as s:
        rid, _ = await selected(s)
        initial = await submit(s, rid)
        result = await finished(s.client, initial["result_id"])
        assert (await submit(s, rid))["result_id"] == initial["result_id"]
        if exhausted:
            assert result["component_status"]["answer"] == "unavailable"
            assert observer.snapshot()["calls"] == 0 and provider_calls == []
        else:
            assert result["component_status"]["answer"] == "available"
            assert observer.snapshot()["calls"] == 1
            assert observer.snapshot()["spent_usd"] == gateway.budget.snapshot()["spent_usd"] > 0
            assert observer.snapshot()["pending_calls"] == 0
            assert len(provider_calls) == 1
            assert provider_calls[0]["model"] == settings.model
            with s.runtime.store.db() as db:
                kinds = {row[0] for row in db.execute("SELECT kind FROM audit_events WHERE result_id=?", (initial["result_id"],))}
            assert {"model_reserved", "model_settled", "answer_scope_validation"} <= kinds


@pytest.mark.anyio
async def test_atomic_source_survives_interruption_before_task_creation(tmp_path, monkeypatch):
    async with service(tmp_path, query=FakeQuery(), resources=FakeResources(tmp_path), gateway=SharedGateway()) as s:
        rid, _ = await selected(s)
        def interrupted_audit(*args):
            raise RuntimeError("synthetic interruption after atomic create")
        monkeypatch.setattr(s.runtime.store, "audit_event", interrupted_audit)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            await s.runtime.create_coloc_summary(rid)
        with s.runtime.store.db() as db:
            payload = json.loads(db.execute("SELECT payload FROM results").fetchone()[0])
        assert payload["source"]["kind"] == "coloc"
        assert payload["source"]["record_id"] == rid
        assert payload["component_status"]["answer"] == "pending"
        s.runtime.store.interrupt()
        saved = await s.runtime.create_coloc_summary(rid)
        assert saved["result_id"] == payload["result_id"]
        assert saved["source"] == payload["source"]
        assert saved["component_status"]["answer"] == "interrupted"
        assert s.gateway.calls == 0 and s.runtime.tasks == {}


@pytest.mark.parametrize("field", ["result_id", "version", "created_at", "updated_at"])
def test_initial_payload_cannot_override_store_identity(tmp_path, field):
    store = ResultStore(tmp_path)
    with pytest.raises(ValueError, match="immutable_result_identity"):
        store.create({"kind": "test"}, {"identity": "test"}, initial={field: "replacement"})
    assert store.by_identity({"identity": "test"}) is None


@pytest.mark.anyio
async def test_lead_cross_membership_uses_full_sets_even_outside_graph_and_top_rows(tmp_path):
    # ADCY3-shaped regression: the QTL lead is absent from the complete GWAS
    # set, but the GWAS lead is a low-PIP QTL member outside its graph/top5.
    gwas_lead, qtl_lead = "rs55893453", "rs10176214"
    qtl_ids = [qtl_lead, gwas_lead, *["rs" + str(i) for i in range(3, 9)]]
    raw = "snp\tpip\tnominal_p\teffect_allele\tother_allele\tslope\tlbf\n"
    for variant in qtl_ids:
        pip = .8 if variant == qtl_lead else .001 if variant == gwas_lead else .01
        raw += f"{variant}\t{pip}\t0.0001\tG\tA\t0.2\t5\n"
    query = FakeQuery(catalog=[core(gwas_lead_vars=gwas_lead, qtl_lead_vars=qtl_lead)],
        gwas=[member(gwas_lead), member("rs999")],
        qtl=[member(qtl_lead, role="qtl", size=8, pip=.8)])
    async with service(tmp_path, query=query, resources=FakeResources(tmp_path, raw=raw.encode()), gateway=SharedGateway()) as s:
        rid, _ = await selected(s)
        await finished(s.client, (await submit(s, rid))["result_id"])
        evidence = s.gateway.evidence
        facts = next(row for row in evidence["recorded_coloc"]["rows"] if row["kind"] == "recorded_lead_membership")
        leads = {row["variant_id"]: row for row in facts["leads"]}
        assert facts["gwas_complete"] and facts["qtl_complete"]
        assert leads[qtl_lead] == {"variant_id": qtl_lead, "gwas_lead": False, "qtl_lead": True,
            "in_returned_gwas": False, "gwas_member": False, "gwas_pip": None,
            "in_returned_qtl": True, "qtl_member": True, "qtl_pip": .8}
        assert leads[gwas_lead] == {"variant_id": gwas_lead, "gwas_lead": True, "qtl_lead": False,
            "in_returned_gwas": True, "gwas_member": True, "gwas_pip": .5,
            "in_returned_qtl": True, "qtl_member": True, "qtl_pip": .001}
        assert gwas_lead not in {row.get("variant_id") for row in evidence["qtl_credible_set"]["rows"]}
        assert not any(edge["type"] == "PART_OF_QTL_SIGNAL" and edge["start_id"] == gwas_lead
            for edge in evidence["recorded_coloc"]["edges"])
        body = json.loads(s.gateway.prepared.body)
        compiled_facts = next(row for row in body["evidence"][0]["rows"] if row["kind"] == "recorded_lead_membership")
        assert compiled_facts["leads"] == facts["leads"]
        question = body["question"]
        assert "at most 100 prose words" in question
        assert "shared-member count" in question
        assert "exactly one table with two study rows: GWAS and QTL" in question
        assert "Do not add headings, a H0–H4 table, or full signal IDs" in question
        assert "Never infer other-study lead membership from sampled graph edges or top-row excerpts" in question
        source = s.runtime.store.source((await submit(s, rid))["result_id"])
        assert source["summary_version"] == "coloc-summary-2"


@pytest.mark.anyio
async def test_absent_lead_is_unknown_when_other_study_membership_is_incomplete(tmp_path):
    from pankgraph_results.resources import ResourceError
    query = FakeQuery(catalog=[core(gwas_lead_vars="rs1", qtl_lead_vars="rs2")],
        qtl=[member("rs2", role="qtl", size=28)])
    async with service(tmp_path, query=query, resources=FakeResources(tmp_path, error=ResourceError("denied")), gateway=SharedGateway()) as s:
        rid, _ = await selected(s)
        await finished(s.client, (await submit(s, rid))["result_id"])
        facts = next(row for row in s.gateway.evidence["recorded_coloc"]["rows"] if row["kind"] == "recorded_lead_membership")
        leads = {row["variant_id"]: row for row in facts["leads"]}
        assert not facts["qtl_complete"]
        assert leads["rs1"]["gwas_member"] is True
        assert leads["rs1"]["in_returned_qtl"] is False
        assert leads["rs1"]["qtl_member"] is None and leads["rs1"]["qtl_pip"] is None
        assert leads["rs2"]["qtl_member"] is True
