"""Shared metadata names do not give non-donor records donor semantics."""
import pytest

from pankagent_vnext.graph import validate_cypher
from pankagent_vnext.release_schema import REGISTRY
from test_cohort_scope import output_tissue_step


def metadata_step():
    return {'id': 'metadata', 'question': 'Show recorded samples and their metadata.',
            'relation_types': [], 'constraints': [], 'complete': True,
            'graph_version': REGISTRY['release']}


def classification_errors(query):
    return [issue for issue in validate_cypher(query, metadata_step())
            if issue.startswith('unrequested_donor_classification_projection:')]


def test_requested_tissue_endpoints_can_include_their_own_recorded_provenance():
    query = ('MATCH (t:anatomical_structure)-[:HAS_SAMPLE]->(s:Sample_node) '
             'RETURN DISTINCT t.id,t.name,t.category,t.description,t.data_source,'
             't.data_version,t.data_source_url')
    assert validate_cypher(query, output_tissue_step()) == []


@pytest.mark.parametrize('label', ['anatomical_structure', 'Sample_node'])
@pytest.mark.parametrize('projection', [
    'RETURN DISTINCT n.data_source AS source',
    'WITH n AS record RETURN record.data_source AS source',
    'WITH DISTINCT n AS record WITH record AS next_record RETURN next_record.data_source AS source',
])
def test_verified_non_donor_owner_preserves_shared_source_property(label, projection):
    assert classification_errors(f'MATCH (n:{label}) {projection}') == []


@pytest.mark.parametrize('query', [
    'MATCH (d:donor) RETURN d.data_source AS source',
    'MATCH (d:donor) WITH d AS x RETURN x.data_source AS source',
    'MATCH (d:donor) WITH head(collect(d)) AS x RETURN x.data_source AS source',
    'MATCH (d:donor) WITH coalesce(d,d) AS x RETURN x.data_source AS source',
    'MATCH (n) RETURN n.data_source AS source',
    'MATCH (n) WITH n AS x RETURN x.data_source AS source',
    'MATCH (n:UnknownOwner) RETURN n.data_source AS source',
    'MATCH (x:Sample_node),(d:donor) WITH head(collect(d)) AS x RETURN x.data_source AS source',
    'MATCH (x:Sample_node),(d:donor) WITH collect(d) AS records UNWIND records AS x RETURN x.data_source AS source',
    'MATCH (x:Sample_node),(u) WITH u AS x RETURN x.data_source AS source',
    'MATCH (x:Sample_node),(u) WITH head(collect(u)) AS x WITH x AS y RETURN y.data_source AS source',
])
def test_donor_derived_and_untyped_projections_remain_guarded(query):
    assert 'unrequested_donor_classification_projection:data_source' in classification_errors(query)


def test_property_must_belong_to_the_verified_non_donor_owner():
    assert 'unrequested_donor_classification_projection:diabetes_type' in classification_errors(
        'MATCH (n:Sample_node) RETURN n.diabetes_type AS diagnosis')


@pytest.mark.parametrize('projection', [
    'RETURN r.data_source AS source',
    'WITH r AS edge_record RETURN edge_record.data_source AS source',
])
def test_verified_relationship_provenance_has_its_own_schema_owner(projection):
    assert classification_errors(
        f'MATCH (g:Gene)-[r:GENE_DETECTED_IN]->(a:anatomical_structure) {projection}') == []


def test_untyped_relationship_is_not_an_owner_proof():
    assert 'unrequested_donor_classification_projection:data_source' in classification_errors(
        'MATCH (g)-[r]->(a) RETURN r.data_source AS source')
