import json
from unittest import IsolatedAsyncioTestCase
import httpx
from pankagent_vnext.literature import LiteratureAdapter
from pankagent_vnext.literature_sources import plain_literature_body, GLKB_INSTRUCTION
from tests_vnext.test_literature import settings, FragmentedStream, attempt

class SingleLiteratureTests(IsolatedAsyncioTestCase):
    async def test_single_query_no_wrapper_fanout(self):
        requests = []
        async def emit(*args):
            pass
        def handler(request):
            requests.append(json.loads(request.content))
            frame = dict(step='Complete', **attempt()['result'])
            body = ('data: ' + json.dumps(frame) + '\n\n').encode()
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=FragmentedStream(body))
        adapter = LiteratureAdapter(settings(literature_api_version='hirn-single-v1'), transport=httpx.MockTransport(handler))
        try:
            result = await adapter.search('What does CFTR do?', [], emit)
            self.assertEqual(requests, [{'question':'What does CFTR do?'}])
            self.assertEqual(result['status'], 'complete')
            self.assertEqual(result['perspectives'], [])
            self.assertEqual(result['references'][0]['pmid'], '12345678')
        finally:
            await adapter.close()

    def test_heading_demotion_preserves_citations_and_bullets(self):
        source = '# Overview\n\n## Context ##\nText [1](#citation-1).\n\nEvidence\n---\n- Finding [2](#citation-2).\n<h3>Gap</h3>\n**Conclusion**'
        result = plain_literature_body(source)
        self.assertEqual(result, 'Overview\n\nContext\nText [1](#citation-1).\n\nEvidence\n- Finding [2](#citation-2).\nGap\nConclusion')
        self.assertIn('180 words', GLKB_INSTRUCTION)
        self.assertIn('Do not use any title', GLKB_INSTRUCTION)
