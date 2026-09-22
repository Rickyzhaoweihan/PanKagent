"""Compatibility checks use real preparation and sanitized parent contracts."""
import asyncio
from copy import deepcopy

import pytest

from pankagent_vnext.plan_constraints import related_context_step
from pankagent_vnext.planning_requirements import requirements_issue
from pankagent_vnext.revision_context import parent_context
from test_planning_requirements import field, step, plan, grounding, history, GENE, NEW_GENE, GO
from test_qtl_tissue_binding import ResolvedGraph, source, emit


def test_actual_qtl_literal_predicate_parent_provenance_accepts_name_id_equivalence():
    async def check():
        prepared = await ResolvedGraph()._prepare_step(source(), emit)
        assert prepared['resolved_entities'][1]['state'] == 'literal_predicate'
        assert prepared['constraints'][1]['property'] == 'tissue_id'
        h = history(plan(prepared), 'Use NFYA instead of GCLC.')
        clean = h[0]['revision_context']['parent_plan']['steps'][0]
        assert all(entity['entity_type'] != 'anatomical_structure' for entity in clean['resolved_entities'])
        assert clean['schema_bindings'][0]['resolved_tissue']['id'] == 'UBERON_0001264'
        proposed = plan(step(field('Gene', 'name', 'NFYA'),
            field(None, 'tissue_name', 'Pancreas', relationship_type='PART_OF_QTL_SIGNAL'),
            field(None, 'pip', '0.1', '>=', relationship_type='PART_OF_QTL_SIGNAL'), relation='PART_OF_QTL_SIGNAL'))
        before = deepcopy((h, proposed))
        assert requirements_issue('Use NFYA instead of GCLC.', grounding(None), proposed, h) is None
        assert (h, proposed) == before
        proposed['steps'][0]['constraints'][1]['value'] = 'Islet'
        assert requirements_issue('Use NFYA instead of GCLC.', grounding(None), proposed, h).startswith('revision_constraint_not_preserved:')
    asyncio.run(check())


@pytest.mark.parametrize('mutation', ['stale_release', 'wrong_id', 'wrong_relation', 'unverified_label'])
def test_only_release_verified_qtl_tissue_provenance_authorizes_alias_equivalence(mutation):
    async def check():
        prepared = await ResolvedGraph()._prepare_step(source(), emit)
        h = history(plan(prepared), 'Use NFYA instead of GCLC.')
        binding = h[0]['revision_context']['parent_plan']['steps'][0]['schema_bindings'][0]
        if mutation == 'stale_release':
            binding['graph_version'] = 'other-release'
        elif mutation == 'wrong_id':
            binding['resolved_tissue']['id'] = 'UBERON_OTHER'
        elif mutation == 'wrong_relation':
            binding['canonical_binding']['relationship_type'] = 'GENE_ENRICHED_IN'
        else:
            binding['resolved_tissue']['labels'] = []
        proposed = plan(step(field('Gene', 'name', 'NFYA'),
            field(None, 'tissue_name', 'Pancreas', relationship_type='PART_OF_QTL_SIGNAL'),
            field(None, 'pip', '0.1', '>=', relationship_type='PART_OF_QTL_SIGNAL'), relation='PART_OF_QTL_SIGNAL'))
        assert requirements_issue('Use NFYA instead of GCLC.', grounding(None), proposed, h).startswith('revision_constraint_not_preserved:')
    asyncio.run(check())


def test_singleton_in_can_preserve_same_identity_and_domain_during_replacement():
    h = history(plan(step(GENE, GO)), 'Replace GLIS3 with CFTR.')
    proposed = plan(step(field('Gene', 'id', '["ENSG00000001626"]', 'IN'),
        field('GO_term', 'go_domain', '["biological_process"]', 'IN')))
    assert requirements_issue('Replace GLIS3 with CFTR.', grounding(), proposed, h) is None
    proposed['steps'][0]['constraints'][1]['value'] = '["biological_process", "molecular_function"]'
    assert requirements_issue('Replace GLIS3 with CFTR.', grounding(), proposed, h).startswith('revision_constraint_not_preserved:')


@pytest.mark.parametrize('instruction', [
    'Use CFTR instead of GLIS3 and keep all other filters.',
    'Replace GLIS3 with CFTR and retain all other constraints.',
    'Please use CFTR instead of GLIS3, and preserve the remaining filters.',
])
def test_and_preservation_clause_cannot_bypass_unchanged_filter_check(instruction):
    h = history(plan(step(GENE, GO)), instruction)
    assert requirements_issue(instruction, grounding(None), plan(step(NEW_GENE, GO)), h) is None
    assert requirements_issue(instruction, grounding(None), plan(step(NEW_GENE)), h).startswith('revision_constraint_not_preserved:')


@pytest.mark.parametrize('instruction', [
    'Use CFTR instead of GLIS3 and keep the tissue but remove the GO filter.',
    'Replace GLIS3 with CFTR and keep all other filters while adding molecular function.',
])
def test_a_real_scope_change_stays_an_ordinary_revision(instruction):
    h = history(plan(step(GENE, GO)), instruction)
    constraints = [field('GO_term', 'go_domain', '["biological_process", "molecular_function"]', 'IN')] if 'adding' in instruction else []
    assert requirements_issue(instruction, grounding(None), plan(step(NEW_GENE, *constraints)), h) is None


class CellGraph(ResolvedGraph):
    async def _resolve_constraint(self, constraint, index, current):
        kind = constraint['entity_type']
        names = {'ENSG00000107249': 'GLIS3', 'ENSG00000001626': 'CFTR', 'CL_0002079': 'ductal cell'}
        return {'constraint_index': index, 'requested': deepcopy(constraint), 'state': 'resolved',
            'graph_version': self.settings.graph_version, 'entity_type': kind, 'labels': [kind],
            'id': constraint['value'], 'name': names[constraint['value']]}


async def _parent_with_generated_helper():
    graph = CellGraph()
    cell = field('anatomical_structure', 'id', 'CL_0002079')
    primary = step(GENE, cell, relation='GENE_ENRICHED_IN')
    primary['question'] = 'Is GLIS3 enriched in ductal cell?'
    primary = await graph._prepare_step(primary, emit)
    generated = related_context_step(plan(primary))
    assert generated and generated['purpose'] == 'context'
    helper = await graph._prepare_step(generated, emit)
    proposed = plan(step(NEW_GENE, cell, relation='GENE_ENRICHED_IN'))
    proposed['steps'][0]['question'] = 'Is CFTR enriched in ductal cell?'
    return graph, plan(primary, helper), proposed


def test_actual_generated_helper_proof_survives_parent_context_and_is_regenerated():
    async def check():
        graph, parent, proposed = await _parent_with_generated_helper()
        h = history(parent, 'Replace GLIS3 with CFTR.')
        helper = h[0]['revision_context']['parent_plan']['steps'][1]
        assert helper['application_generated_context'] == {'kind': 'related_context_step', 'version': 1, 'source_step_id': 's1'}
        assert requirements_issue('Replace GLIS3 with CFTR.', grounding(None), proposed, h) is None
        prepared = await graph.prepare_plan(proposed, emit)
        assert len(prepared['steps']) == 2
        regenerated = prepared['steps'][1]
        assert regenerated['purpose'] == 'context' and regenerated['context_for'] == 's1'
        assert regenerated['constraints'][0]['value'] == NEW_GENE['value']
        assert regenerated['complete'] is False
    asyncio.run(check())


@pytest.mark.parametrize('mutation', ['new_filter', 'changed_question', 'wrong_parent', 'complete_scope', 'different_category'])
def test_modified_or_user_context_is_not_discarded_as_an_application_helper(mutation):
    async def check():
        graph, parent, proposed = await _parent_with_generated_helper()
        helper = parent['steps'][1]
        if mutation == 'new_filter':
            helper['constraints'].append(field(None, 'condition', 'ND', relationship_type='GENE_DETECTED_IN'))
        elif mutation == 'changed_question':
            helper['question'] += ' Also restrict to beta cells.'
        elif mutation == 'wrong_parent':
            helper['context_for'] = 'missing'
        elif mutation == 'complete_scope':
            helper['complete'] = True
        else:
            helper['relation_types'] = ['MARKER_GENE_OF']
        h = history(parent, 'Replace GLIS3 with CFTR.')
        clean = h[0]['revision_context']['parent_plan']['steps'][1]
        assert 'application_generated_context' not in clean
        assert requirements_issue('Replace GLIS3 with CFTR.', grounding(None), proposed, h) == 'revision_checks_not_preserved:identity_replacement_only'
    asyncio.run(check())
