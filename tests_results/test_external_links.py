import unittest
from pankgraph_results.resources import _reference_tabs
from pankgraph_results.external_links import build_external_link_groups


def node(nid, name, url):
    return {'id': nid, 'labels': ['Pathway'], 'properties': {'name': name, 'data_source': 'KEGG', 'data_source_url': url}}


class ExternalLinksTest(unittest.TestCase):
    def test_seventy_links_group_and_keep_names(self):
        nodes = [node(str(i), f'Pathway {i:02}', f'https://www.kegg.jp/pathway/{i}') for i in range(70)]
        tabs = _reference_tabs(nodes, [])
        self.assertEqual(len(tabs['external_links']), 70)
        self.assertEqual(len(tabs['external_link_groups']), 1)
        self.assertEqual(len(tabs['external_link_groups'][0]['entries']), 70)
        self.assertEqual(tabs['external_link_groups'][0]['entries'][0]['label'], 'Pathway 00')

    def test_shared_url_and_edge_context(self):
        nodes = [node('a', 'Alpha', 'https://www.kegg.jp/shared'), node('b', 'Beta', 'https://www.kegg.jp/shared')]
        edge = {'id': 'e', 'start_id': 'a', 'end_id': 'b', 'type': 'REGULATES', 'properties': {'data_source': 'kegg', 'data_source_url': 'https://www.kegg.jp/edge'}}
        entries = _reference_tabs(nodes, [edge])['external_link_groups'][0]['entries']
        self.assertEqual(len(entries), 2)
        shared = next(e for e in entries if e['url'].endswith('shared'))
        self.assertEqual([e['name'] for e in shared['entities']], ['Alpha', 'Beta'])
        self.assertEqual(len(shared['provenance']), 2)
        self.assertEqual(next(e for e in entries if e['url'].endswith('edge'))['label'], 'Alpha → Beta · REGULATES')

    def test_generated_routes_missing_name_portal_and_invalid_url(self):
        nodes = [{'id': 'ENSG00000000001', 'labels': ['Gene'], 'properties': {'name': 'ADCY3'}}, {'id': 'rs12'}]
        groups = _reference_tabs(nodes, [])['external_link_groups']
        self.assertEqual([g['display_name'] for g in groups], ['dbSNP', 'Ensembl'])
        self.assertEqual(groups[0]['entries'][0]['label'], 'rs12')
        self.assertEqual(groups[1]['entries'][0]['label'], 'ADCY3')
        groups = build_external_link_groups([['Portal', 'Browse records', 'https://example.org'], ['bad', '', 'javascript:alert(1)']], [], [])
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['entries'][0]['label'], 'Browse records')

    def test_legacy_references_unchanged_and_host_aliases(self):
        n = node('a', '', 'https://www.kegg.jp/a'); n['properties']['pmid'] = '34012112'
        tabs = _reference_tabs([n], [])
        self.assertEqual(tabs['references']['pmid:34012112']['pmid'], '34012112')
        self.assertEqual(tabs['external_link_groups'][0]['entries'][0]['label'], 'a')
        groups = build_external_link_groups([['KEGG', 'A', 'https://www.kegg.jp/a'], ['kegg', 'B', 'https://www.genome.jp/b']], [], [])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]['entries']), 2)

    def test_same_url_with_distinct_unregistered_sources_keeps_both_groups(self):
        a = node('a', 'Alpha', 'https://example.org/shared')
        b = node('b', 'Beta', 'https://example.org/shared')
        a['properties']['data_source'] = 'Source A'
        b['properties']['data_source'] = 'Source B'
        tabs = _reference_tabs([a, b], [])
        self.assertEqual(len(tabs['external_links']), 1)
        self.assertEqual(len(tabs['external_link_groups']), 2)
