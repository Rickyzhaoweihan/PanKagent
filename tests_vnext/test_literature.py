"""Synthetic, publication-oriented HIRN contract tests. No live model calls."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

import httpx

from pankagent_vnext.literature import LiteratureAdapter, _normalize_legacy


def settings(**overrides):
    return SimpleNamespace(**{
        "literature_url": "http://hirn.test", "literature_timeout": 2,
        "corpus_version": "test-papers-v1", "source_policy": "mixed", **overrides,
    })


def attempt(identifier="paper-1", *, answer=None, refs=None):
    return {
        "attempt_id": "r1-1", "status": "complete", "query": "Synthetic publication question",
        "result": {
            "response": answer if answer is not None else "Synthetic mechanistic finding [1].",
            "references": refs if refs is not None else [{
                "id": identifier, "document_id": "document-1", "pmid": "12345678",
                "title": "Synthetic publication used only for interface testing",
                "source": "publication", "source_type": "research_article",
                "authors": ["Test Author"], "url": "https://example.org/paper",
                "evidence": [{"quote": "PRIVATE_RETRIEVED_EXCERPT"}],
            }],
            "trajectory": ["PRIVATE_PROCESSING"],
        },
    }


def final_payload():
    return {
        "perspectives": {
            "context_mechanism": {"label": "Mechanism evidence", "selected": attempt(), "alternatives": []},
            "alternative_explanation": {"label": "Alternative explanation", "selected": attempt(
                "paper-2", answer="A synthetic alternative finding [12345678]."), "alternatives": []},
        },
        "usage_status": {"month": "2026-09", "estimated_monthly_cost_usd": 15.0,
                         "claude_calls": 7, "private": "PRIVATE_USAGE_FIELD"},
        "attempts": ["PRIVATE_RAW_ATTEMPTS"], "audit": {"reason": "PRIVATE_AUDIT"},
    }


def event(kind, value):
    return f"event: {kind}\ndata: {json.dumps(value)}\n\n".encode()


class FragmentedStream(httpx.AsyncByteStream):
    def __init__(self, body, size=13, delay=0):
        self.body, self.size, self.delay = body, size, delay
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.body), self.size):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield self.body[offset:offset+self.size]

    async def aclose(self):
        self.closed = True


class LiteratureTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapters = []
        self.events = []

    async def asyncTearDown(self):
        for adapter in self.adapters:
            await adapter.close()

    def adapter(self, handler, **options):
        adapter = LiteratureAdapter(settings(**options), transport=httpx.MockTransport(handler))
        self.adapters.append(adapter)
        return adapter

    async def emit(self, kind, payload):
        self.events.append((kind, payload))

    async def test_one_request_preserves_units_metadata_and_separates_usage(self):
        requests = []
        body = event("planning", {"strategy": "PRIVATE_PLAN"})
        body += event("attempt_complete", {"result": "PRIVATE_RETRIEVED_EXCERPT"})
        body += event("Processing", {"content": "PRIVATE_PROCESSING"})
        body += event("complete", final_payload())

        def handler(request):
            requests.append(request)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=FragmentedStream(body))

        adapter = self.adapter(handler)
        result = await adapter.search("Which publications describe this mechanism?", [], self.emit)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.path, "/stream")
        self.assertEqual(result["status"], "complete")
        selected = result["perspectives"][0]
        self.assertEqual(selected["answer"], "Synthetic mechanistic finding [1].")
        self.assertEqual(selected["references"][0]["document_id"], "document-1")
        self.assertEqual(selected["references"][0]["source_type"], "research_article")
        self.assertEqual(selected["citation_validation"]["status"], "linked")
        self.assertIsNone(result["upstream_usage"]["per_request_cost_usd"])
        self.assertEqual(result["upstream_usage"]["service_cumulative_snapshot"]["estimated_monthly_cost_usd"], 15)
        self.assertNotIn("PRIVATE", json.dumps([result, self.events]))
        self.assertEqual(len([x for x in self.events if x[0] == "literature_perspective"]), 2)

    async def test_no_evidence_and_missing_perspective_are_visible(self):
        payload = final_payload()
        payload["perspectives"]["context_mechanism"]["selected"] = attempt(answer="Not found in the indexed literature.", refs=[])
        payload["perspectives"]["alternative_explanation"]["selected"] = None
        adapter = self.adapter(lambda request: httpx.Response(200, headers={"content-type":"text/event-stream"}, content=event("complete", payload)))
        result = await adapter.search("What is the evidence?", [], self.emit)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["perspectives"][0]["status"], "no_evidence")
        self.assertEqual(result["perspectives"][1]["status"], "unavailable")
        self.assertEqual(result["perspectives"][0]["references"], [])

    async def test_bad_reference_link_is_flagged_without_inventing_sources(self):
        payload = final_payload()
        payload["perspectives"]["context_mechanism"]["selected"] = attempt(answer="Finding [98765432].")
        result = _normalize_legacy(payload)[0]
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["citation_validation"]["unresolved_markers"], ["98765432"])
        self.assertEqual(len(result["references"]), 1)

    async def test_dedup_does_not_combine_answers_or_renumber_references(self):
        payload = final_payload()
        main = payload["perspectives"]["context_mechanism"]
        main["alternatives"] = [copy.deepcopy(main["selected"]), attempt(answer="Different finding [1].")]
        perspectives = _normalize_legacy(payload)
        self.assertEqual(len(perspectives[0]["alternatives"]), 1)
        self.assertEqual(perspectives[0]["alternatives"][0]["answer"], "Different finding [1].")
        self.assertEqual(perspectives[0]["answer"], "Synthetic mechanistic finding [1].")

    async def test_unsafe_url_removed_and_numeric_pmid_preserved(self):
        payload = final_payload()
        ref = payload["perspectives"]["context_mechanism"]["selected"]["result"]["references"][0]
        ref.update(url="javascript:alert(1)", fulltext_url="https://user:password@example.org/x", pmid=12345678)
        clean = _normalize_legacy(payload)[0]["references"][0]
        self.assertNotIn("url", clean)
        self.assertNotIn("fulltext_url", clean)
        self.assertEqual(clean["pmid"], 12345678)

    async def test_fragmented_crlf_and_final_without_separator(self):
        body = event("complete", final_payload()).replace(b"\n", b"\r\n").rstrip()
        adapter = self.adapter(lambda request: httpx.Response(200, headers={"content-type":"text/event-stream"}, stream=FragmentedStream(body,1)))
        self.assertEqual((await adapter.search("question", [], self.emit))["status"], "complete")

    async def test_incomplete_stream_is_unavailable_without_attempt_leakage(self):
        adapter = self.adapter(lambda request: httpx.Response(200, headers={"content-type":"text/event-stream"}, content=event("attempt_complete",attempt())))
        result = await adapter.search("question", [], self.emit)
        self.assertEqual(result["error_category"], "incomplete_stream")
        self.assertEqual(result["perspectives"], [])

    async def test_error_text_is_never_forwarded(self):
        adapter = self.adapter(lambda request: httpx.Response(200, headers={"content-type":"text/event-stream"}, content=event("error",{"message":"PRIVATE_TOKEN"})))
        result = await adapter.search("question", [], self.emit)
        self.assertEqual(result["error_category"], "upstream_error")
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))

    async def test_http_failure_categories(self):
        for status, category in [(401,"authentication"),(429,"rate_limit"),(503,"upstream_http")]:
            adapter = self.adapter(lambda request, status=status: httpx.Response(status,text="PRIVATE_TOKEN"))
            result = await adapter.search("question", [], self.emit)
            self.assertEqual(result["error_category"],category)

    async def test_absolute_deadline_despite_incoming_progress(self):
        stream = FragmentedStream(event("planning",{}) * 100, size=10, delay=.005)
        adapter = self.adapter(lambda request:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream),literature_timeout=.03)
        result = await adapter.search("question", [], self.emit)
        self.assertEqual(result["error_category"],"timeout")
        self.assertTrue(stream.closed)

    async def test_cancellation_closes_stream_and_is_not_an_outage(self):
        stream = FragmentedStream(event("planning",{}) * 100, size=10, delay=.005)
        adapter = self.adapter(lambda request:httpx.Response(200,headers={"content-type":"text/event-stream"},stream=stream))
        task = asyncio.create_task(adapter.search("question",[],self.emit))
        await asyncio.sleep(.02)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)
        self.assertIsNone(adapter.last_error)

    async def test_unsupported_policy_never_sends_a_request(self):
        requests=[]
        def handler(request):
            requests.append(request)
            return httpx.Response(200)
        adapter=self.adapter(handler,source_policy="papers_only")
        result=await adapter.search("question",[],self.emit)
        self.assertEqual(result["error_category"],"unsupported_source_policy")
        self.assertEqual(requests,[])

    async def test_corpus_and_endpoint_migration_invalidate_cache(self):
        handler=lambda request:httpx.Response(200)
        first=self.adapter(handler)
        changed=self.adapter(handler,corpus_version="test-papers-v2")
        endpoint=self.adapter(handler,literature_url="http://clean.test")
        self.assertNotEqual(first.cache_identity,changed.cache_identity)
        self.assertNotEqual(first.cache_identity,endpoint.cache_identity)
        unsupported=self.adapter(handler,literature_api_version="clean-v2")
        result=await unsupported.search("question",[],self.emit)
        self.assertEqual(result["error_category"],"unsupported_adapter_version")

    async def test_health_is_read_only_and_distinguishes_upstream_failure(self):
        calls=[]
        def handler(request):
            calls.append((request.method,request.url.path))
            return httpx.Response(503,json={"status":"degraded","hirn_healthy":False,"anthropic_configured":True,"model":"test-model"})
        adapter=self.adapter(handler)
        probe=await adapter.probe()
        self.assertEqual(calls,[("GET","/health")])
        self.assertEqual(probe["state"],"degraded")
        self.assertTrue(probe["wrapper_reachable"])
        self.assertFalse(probe["upstream_reachable"])
        self.assertIsNone(probe["last_retrieval_success"])

    async def test_invalid_complete_contract_is_unavailable(self):
        for payload in [{"selected":attempt()}, {"perspectives":[]}]:
            adapter=self.adapter(lambda request,payload=payload:httpx.Response(200,headers={"content-type":"text/event-stream"},content=event("complete",payload)))
            result=await adapter.search("question",[],self.emit)
            self.assertEqual(result["status"],"unavailable")

    async def test_request_history_is_wrapper_compatible(self):
        requests=[]
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,headers={"content-type":"text/event-stream"},content=event("complete",final_payload()))
        adapter=self.adapter(handler)
        await adapter.search("follow up",[{"question":"prior","answer":"prior answer [1]","references":[{"pmid":"12345678","evidence":"PRIVATE"}],"private":"PRIVATE"}],self.emit)
        self.assertEqual(requests[0]["conversation"],[{"question":"prior","response":"prior answer [1]","references":[{"pmid":"12345678"}]}])

    async def test_runtime_role_history_is_converted(self):
        requests=[]
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200,headers={"content-type":"text/event-stream"},content=event("complete",final_payload()))
        adapter=self.adapter(handler)
        await adapter.search("follow up",[{"role":"user","content":"prior question"},{"role":"assistant","content":"prior graph answer"}],self.emit)
        self.assertEqual(requests[0]["conversation"],[{"question":"prior question","response":"prior graph answer","references":[]}])
