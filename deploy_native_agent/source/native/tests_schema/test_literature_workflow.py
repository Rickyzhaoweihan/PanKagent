"""Exercise the release entrypoint through confirmation and literature delivery."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
import query_release


class LiteratureWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, include_literature=None, unavailable=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / 'token'
            token.write_text('offline-test')
            config = root / 'config.json'
            config.write_text(json.dumps(dict(schema_path='offline', release_id='offline',
                model_url='http://unused', model='offline', model_token_file=str(token),
                query_token_file=str(token))))
            app = FastAPI()
            pack = SimpleNamespace(data={'release': 'offline', 'semantics_version': 'schema-semantics-v1'}, sha256='offline')
            with patch.object(query_release.core, 'SchemaPack', return_value=pack), \
                 patch.object(query_release.core, 'context_identity', return_value={}), \
                 patch.object(query_release.native_client, 'Client'), \
                 patch.object(query_release, 'IntegratedTransport'):
                query_release.install(app, None, root, config)
            workflow = app.state.current_workflow
            # This assertion fails if the actual release entrypoint disconnects the adapter.
            self.assertIs(workflow.literature, query_release.literature)
            result = dict(status='complete', retrieval_status='complete',
                metapath={'nodes': [], 'edges': []}, graph={'nodes': [], 'edges': []})
            async def runner(*args, **kwargs):
                return copy.deepcopy(result)
            async def formatter(*args, **kwargs):
                return {'answer': 'Recorded graph evidence. [G1]'}
            calls = []
            async def literature(question, history, answer, emit):
                calls.append((question, answer))
                sources = {'hirn': {'status': 'complete', 'references': [{'id': 'paper-1'}]},
                           'glkb': {'status': 'unavailable' if unavailable else 'complete', 'references': []}}
                value = {'status': 'partial' if unavailable else 'complete', 'sources': sources, 'perspectives': []}
                emit('literature_sources', value)
                return value
            workflow.runner, workflow.formatter, workflow.literature = runner, formatter, literature
            try:
                admitted = workflow.create('An offline question', include_literature=include_literature)
                rid = admitted['run_id']
                await workflow.tasks[rid]
                self.assertEqual(workflow.snapshot(rid)['plan']['literature'], include_literature is not False)
                workflow.confirm(rid)
                await workflow.tasks[rid]
                saved = workflow.snapshot(rid)
                if include_literature is False:
                    self.assertFalse(calls)
                else:
                    self.assertEqual(len(calls), 1)
                    self.assertTrue(saved['literature_complete'])
                    self.assertEqual(set(saved['literature']['sources']), {'hirn', 'glkb'})
                    self.assertTrue(saved['literature']['sources']['hirn']['references'])
                    self.assertIn('literature_complete', [event['type'] for event in workflow.runs[rid]['_events']])
                    restored = type(workflow)(runner, root / 'workflow-state-1104')
                    self.assertEqual(restored.snapshot(rid)['literature'], saved['literature'])
                self.assertEqual(saved['status'], 'partial' if unavailable else 'completed')
            finally:
                clocks = list(workflow.clocks.values())
                for task in clocks:
                    task.cancel()
                import asyncio
                await asyncio.gather(*clocks, return_exceptions=True)

    async def test_default_searches_both_sources_and_persists(self):
        await self.exercise()

    async def test_explicit_opt_out_skips_search(self):
        await self.exercise(include_literature=False)

    async def test_source_failure_keeps_sibling_and_marks_partial(self):
        await self.exercise(unavailable=True)
