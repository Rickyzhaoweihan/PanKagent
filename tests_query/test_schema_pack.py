import hashlib
import json

from pankagent_query.schema_pack import compile_pack


def test_frozen_schema_preserves_shared_types_and_all_relation_semantics(tmp_path):
    manifest = {'sql': {'metric': {'snapshot_id': 'source-snapshot'}}}
    raw = json.dumps(manifest).encode()
    (tmp_path/'inputs.json').write_bytes(raw)

    def node(labels, rrid_type):
        return {'label_sets': [';'.join(labels)], 'required_labels': labels,
                'count': 1, 'sources': ['source'], 'source_urls': ['https://example.test'],
                'licenses': ['unknown'], 'data_versions': ['source-version'],
                'files': {'file': {'header': [':ID', ':LABEL', 'id:string', 'rrid:'+rrid_type]}}}

    def edge(end):
        return {'relationship_type': 'LINK', 'start_type': 'A', 'end_type': end,
                'count': 1, 'sources': ['source'], 'source_urls': ['https://example.test'],
                'licenses': ['unknown'], 'data_versions': ['source-version'],
                'files': {'file': {'header': [':START_ID', ':END_ID', ':TYPE', 'id:string']}}}

    data = {'input_manifest_sha256': hashlib.sha256(raw).hexdigest(),
            'release_id': 'fixture', 'node_count': 2, 'edge_count': 2,
            'nodes': {'A': node(['Shared', 'A'], 'string'),
                      'B': node(['Shared', 'B'], 'string[]')},
            'edges': {'first': edge('A'), 'second': edge('B')}}
    viewer = {'release_id': 'fixture', 'profiles': {
        'node:A': {'kind': 'node', 'id': 'A', 'tables': ['metric'],
                   'match': {'labels_all': ['A']}},
        'edge:first': {'kind': 'edge', 'title': 'First meaning'},
        'edge:second': {'kind': 'edge', 'title': 'Second meaning'}},
        'tables': {'metric': {'all_fields': ['value'], 'filters': ['group']}},
        'table_bindings': {'metric': {'registered': True}}}
    (tmp_path/'data_schema.json').write_text(json.dumps(data))
    (tmp_path/'viewer.schema.json').write_text(json.dumps(viewer))
    pack = compile_pack(tmp_path, viewer)
    assert pack['property_types']['node']['Shared']['rrid'] == ['string', 'string[]']
    assert pack['node_variants']['B']['property_types']['rrid'] == 'string[]'
    assert pack['relations']['LINK']['description'] == 'First meaning; Second meaning'
    assert pack['source_snapshots']['metric'] == 'source-snapshot'
    assert pack['source_metadata']['node']['A']['licenses'] == ['unknown']
    assert pack['linked_evidence']['profiles']['node:A']['table_ids'] == ['metric']
