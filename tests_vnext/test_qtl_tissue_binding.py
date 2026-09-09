import asyncio
from copy import deepcopy
import time
from types import SimpleNamespace

import pytest
from pankagent_vnext.graph import GraphAdapter, validate_cypher
from pankagent_vnext.query_templates import compile_query

RELEASE = 'PanKgraph_08_04'


class ResolvedGraph(GraphAdapter):
    def __init__(self, tissue_state='resolved', release=RELEASE):
        self.settings = SimpleNamespace(graph_version=release)
        self.identity_verified = True
        self.identity_check_time = time.monotonic()
        self.release_relations = {'PART_OF_QTL_SIGNAL', 'GENE_ENRICHED_IN', 'HAS_SAMPLE'}
        self.tissue_state = tissue_state

    async def _resolve_constraint(self, constraint, index, step):
        kind = constraint['entity_type']
        value = {'constraint_index': index, 'requested': deepcopy(constraint),
                 'state': 'resolved', 'graph_version': self.settings.graph_version,
                 'entity_type': kind, 'labels': [kind]}
        if kind == 'Gene':
            value.update(id='ENSG00000001084', name='GCLC')
        else:
            value.update(id='UBERON_0001264', name='pancreas', state=self.tissue_state)
        return value


async def emit(*args):
    pass


def source(prop='id', value='UBERON_0001264', kinds=None):
    return {'id': 's1', 'question': 'Show GCLC QTL in pancreas', 'complete': True,
            'relation_types': kinds or ['PART_OF_QTL_SIGNAL'],
            'constraints': [
                {'entity_type': 'Gene', 'property': 'id', 'operator': '=', 'value': 'ENSG00000001084'},
                {'entity_type': 'anatomical_structure', 'property': prop, 'operator': '=', 'value': value},
                {'entity_type': None, 'owner_kind': 'relationship', 'relationship_type': 'PART_OF_QTL_SIGNAL',
                 'property': 'pip', 'operator': '>=', 'value': '0.1'}]}


@pytest.mark.parametrize('prop,value', [('id', 'UBERON_0001264'), ('name', 'pancreas')])
def test_resolved_tissue_becomes_exact_qtl_property_and_preserves_provenance(prop, value):
    async def check():
        graph = ResolvedGraph()
        raw = source(prop, value)
        original = deepcopy(raw)
        prepared = await graph._prepare_step(raw, emit)
        c = prepared['constraints'][1]
        assert c == {'entity_type': None, 'property': 'tissue_id', 'operator': '=', 'value': 'UBERON_0001264',
                     'owner_kind': 'relationship', 'relationship_type': 'PART_OF_QTL_SIGNAL'}
        assert prepared['constraints'][0] == original['constraints'][0]
        assert prepared['constraints'][2] == original['constraints'][2]
        tissue = prepared['resolved_entities'][1]
        assert tissue['state'] == 'literal_predicate' and tissue['requested'] == c
        assert tissue['original_requested'] == original['constraints'][1]
        assert tissue['id'] == 'UBERON_0001264' and tissue['name'] == 'pancreas'
        assert prepared['schema_bindings'][-1]['canonical_binding'] == c
        assert prepared['schema_bindings'][-1]['resolved_tissue']['id'] == 'UBERON_0001264'
        assert prepared['entity_resolution']['state'] == 'resolved'
        assert graph._resolution_verified(prepared)
        assert raw == original
        query = ("MATCH (v:variants)-[r:PART_OF_QTL_SIGNAL]->(g:Gene) "
                 "WHERE g.id='ENSG00000001084' AND r.tissue_id='UBERON_0001264' AND r.pip>=0.1 RETURN v,r,g")
        assert validate_cypher(query, prepared) == []
        assert validate_cypher(query.replace('r.tissue_id', 'g.tissue_id'), prepared)
        template = compile_query(prepared)
        assert template and template['parameters']['template_1'] == 'UBERON_0001264'
        assert 'r.`tissue_id` = $template_1' in template['cypher']
        assert validate_cypher(template['cypher'], prepared, template['parameters']) == []
    asyncio.run(check())


def test_reprepare_retains_canonical_tissue_filter_and_does_not_duplicate_binding():
    async def check():
        graph = ResolvedGraph()
        first = await graph._prepare_step(source(), emit)
        second = await graph._prepare_step(first, emit)
        assert second['constraints'] == first['constraints']
        assert second['schema_bindings'] == first['schema_bindings']
        assert compile_query(second)
    asyncio.run(check())


@pytest.mark.parametrize('kinds', [['GENE_ENRICHED_IN'], ['PART_OF_QTL_SIGNAL', 'HAS_SAMPLE']])
def test_other_evidence_and_multi_relationship_steps_keep_anatomy_owner(kinds):
    async def check():
        raw = source(kinds=kinds)
        raw['constraints'] = raw['constraints'][:2]
        prepared = await ResolvedGraph()._prepare_step(raw, emit)
        assert prepared['constraints'][1]['entity_type'] == 'anatomical_structure'
        assert prepared['constraints'][1]['property'] == 'id'
        assert not any(b.get('kind') == 'verified_qtl_tissue_property' for b in prepared.get('schema_bindings', []))
    asyncio.run(check())


@pytest.mark.parametrize('state', ['ambiguous', 'not_found'])
def test_unresolved_tissue_is_not_substituted(state):
    async def check():
        prepared = await ResolvedGraph(tissue_state=state)._prepare_step(source(), emit)
        assert prepared['constraints'][1]['entity_type'] == 'anatomical_structure'
        assert prepared['entity_resolution']['state'] == 'needs_clarification'
    asyncio.run(check())


def test_wrong_release_disables_tissue_remapping():
    async def check():
        prepared = await ResolvedGraph(release='other-release')._prepare_step(source(), emit)
        assert prepared['constraints'][1]['entity_type'] == 'anatomical_structure'
    asyncio.run(check())


@pytest.mark.parametrize('value,expected', [('pancreas', 'Pancreas'), ('ISLET', 'Islet')])
def test_generic_tissue_category_and_duplicate_gene_identity_compile_without_inference(value, expected):
    async def check():
        raw = source()
        raw['constraints'] = [
            {'entity_type': 'Gene', 'property': 'name', 'operator': '=', 'value': 'GCLC'},
            {'entity_type': 'Gene', 'property': 'id', 'operator': '=', 'value': 'ENSG00000001084'},
            {'entity_type': None, 'property': 'tissue', 'operator': '=', 'value': value}]
        original = deepcopy(raw)
        graph = ResolvedGraph()
        prepared = await graph._prepare_step(raw, emit)
        assert len(prepared['constraints']) == 2
        assert prepared['constraints'][0]['property'] == 'id'
        assert prepared['constraints'][0]['value'] == 'ENSG00000001084'
        tissue = prepared['constraints'][1]
        assert tissue == {'entity_type': None, 'property': 'tissue_name', 'operator': '=', 'value': expected,
                          'owner_kind': 'relationship', 'relationship_type': 'PART_OF_QTL_SIGNAL'}
        assert len(prepared['resolved_entities']) == 1
        assert prepared['resolved_entities'][0]['constraint_index'] == 0
        identity_binding = next(b for b in prepared['schema_bindings'] if b['kind'] == 'verified_same_qtl_gene_identity')
        assert identity_binding['requested'] == original['constraints'][:2]
        assert raw == original
        template = compile_query(prepared)
        assert template is not None
        assert validate_cypher(template['cypher'], prepared, template['parameters']) == []
        query = ("MATCH (v:variants)-[r:PART_OF_QTL_SIGNAL]->(g:Gene) "
                 f"WHERE g.name='GCLC' AND r.tissue_name='{expected}' RETURN v,r,g")
        assert validate_cypher(query, prepared) == []
        assert validate_cypher(query.replace('r.tissue_name', 'g.tissue_name'), prepared)
        again = await graph._prepare_step(prepared, emit)
        assert again['constraints'] == prepared['constraints']
        assert again['schema_bindings'] == prepared['schema_bindings']
    asyncio.run(check())


@pytest.mark.parametrize('value', ['pancreatic organ', 'other', 'Pancreas or Islet'])
def test_unverified_generic_tissue_never_substitutes_recorded_category(value):
    async def check():
        raw = source()
        raw['constraints'] = [{'entity_type': None, 'property': 'tissue', 'operator': '=', 'value': value}]
        prepared = await ResolvedGraph()._prepare_step(raw, emit)
        assert prepared['constraints'] == raw['constraints']
        assert compile_query(prepared) is None
    asyncio.run(check())


def test_different_gene_ids_and_explicit_wrong_tissue_owner_are_not_deduplicated_or_remapped():
    class DistinctGraph(ResolvedGraph):
        async def _resolve_constraint(self, constraint, index, step):
            result = await super()._resolve_constraint(constraint, index, step)
            if constraint['value'] == 'OTHER':
                result.update(id='ENSG_OTHER', name='OTHER')
            return result
    async def check():
        raw = source()
        raw['constraints'] = [
            {'entity_type': 'Gene', 'property': 'name', 'operator': '=', 'value': 'GCLC'},
            {'entity_type': 'Gene', 'property': 'name', 'operator': '=', 'value': 'OTHER'},
            {'entity_type': 'Gene', 'property': 'tissue', 'operator': '=', 'value': 'pancreas'}]
        prepared = await DistinctGraph()._prepare_step(raw, emit)
        assert prepared['constraints'] == raw['constraints']
    asyncio.run(check())
