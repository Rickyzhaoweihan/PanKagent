import copy
import re

from pankagent_vnext.coloc_records import derive_colocalization_records


def _node(identifier, label):
    return {"id": identifier, "labels": [label], "properties": {"id": identifier}}


def _edge(qtl_signal, qtl_lead, dataset, h4):
    return {
        "start_id": "ENSG00000225190",
        "end_id": "MONDO_0005147",
        "type": "SIGNAL_COLOC_WITH",
        "properties": {
            "gwas_signal_id": "MAPT__credibleSet1__selected",
            "qtl_signal_id": qtl_signal,
            "gwas_lead_vars": "rs35327136",
            "qtl_lead_vars": qtl_lead,
            "coloc_dataset": dataset,
            "data_source": "HIRN_T1D_QTL_GWAS",
            "data_version": "v1.0",
            "gwas_locus_name": "MAPT",
            "qtl_locus_name": "PLEKHM1",
            "pp_h4_abf": h4,
        },
    }


def _evidence(*, partial=False):
    return {
        "status": "partial" if partial else "complete",
        "truncated": partial,
        "graph_version": "PanKgraph_08_04",
        "retrieval_execution": {"completed": True, "cursor_exhausted": not partial},
        "nodes": [
            _node("MONDO_0005147", "disease"),
            _node("ENSG00000225190", "Gene"),
        ],
        # Intentionally reverse the desired stable QTL-signal order.
        "edges": [
            _edge("PLEKHM1__exon__credibleSet3", "rs62064652",
                  "t1d_exonQTL-inspire_coloc", 0.991),
            _edge("PLEKHM1__credibleSet1", "rs62065450",
                  "t1d_eQTL-inspire_coloc", 0.984),
        ],
    }


def test_plekhm1_records_link_two_qtls_to_one_shared_gwas_without_invented_tissue():
    result = derive_colocalization_records(_evidence())

    assert result["version"] == "colocalization-records-v1"
    assert result["completeness"]["state"] == "complete"
    assert [row["recorded_qtl_signal_id"]["value"] for row in result["records"]] == [
        "PLEKHM1__credibleSet1", "PLEKHM1__exon__credibleSet3"
    ]
    assert [row["qtl_lead_variant_ids"] for row in result["records"]] == [
        ["rs62065450"], ["rs62064652"]
    ]
    assert {row["recorded_gwas_signal_id"]["value"] for row in result["records"]} == {
        "MAPT__credibleSet1__selected"
    }
    assert {row["gwas_lead_variant_ids"][0] for row in result["records"]} == {
        "rs35327136"
    }
    assert {row["recorded_gwas_locus_name"]["value"] for row in result["records"]} == {
        "MAPT"
    }
    assert {row["recorded_qtl_locus_name"]["value"] for row in result["records"]} == {
        "PLEKHM1"
    }
    assert [row["recorded_pp_h4_abf"]["value"] for row in result["records"]] == [
        0.984, 0.991
    ]
    assert all(row["endpoint_verification"]["typed_endpoints_verified"]
               for row in result["records"])
    assert all(row["typed_endpoints_verified"] for row in result["records"])
    assert all(row["recorded_tissue_context"] == {
        "state": "unavailable",
        "value": None,
        "reason": "no_registered_tissue_property_on_SIGNAL_COLOC_WITH",
        "registered_schema_release": "PanKgraph_08_04",
        "evidence_graph_version": "PanKgraph_08_04",
        "registered_tissue_properties": [],
        "inferred_from_coloc_dataset": False,
    } for row in result["records"])
    assert "rs112550936" not in repr(result)

    counts = result["counts"]
    assert counts["record_count"] == 2
    assert counts["gwas_signal_reference_count"] == 2
    assert counts["distinct_recorded_gwas_signal_count"] == 1
    assert counts["qtl_signal_reference_count"] == 2
    assert counts["distinct_recorded_qtl_signal_count"] == 2
    assert counts["distinct_recorded_signal_pair_count"] == 2
    assert counts["tissue_context_recorded_count"] == 0
    assert counts["tissue_context_unavailable_count"] == 2


def test_order_links_and_hashes_are_stable_across_input_order():
    evidence = _evidence()
    reordered = copy.deepcopy(evidence)
    reordered["edges"].reverse()
    reordered["nodes"].reverse()

    first = derive_colocalization_records(evidence)
    second = derive_colocalization_records(reordered)
    assert first == second
    assert len(set(first["record_links"])) == 2
    for record, link in zip(first["records"], first["record_links"]):
        assert link == record["record_link"]
        assert link.endswith(record["record_sha256"])
        assert re.fullmatch(r"[0-9a-f]{64}", record["record_sha256"])


def test_partial_retrieval_is_disclosed_and_nonfinite_or_oversized_values_are_bounded():
    evidence = _evidence(partial=True)
    evidence["edges"][0]["properties"]["pp_h4_abf"] = float("nan")
    evidence["edges"][0]["properties"]["data_source"] = "x" * 513
    result = derive_colocalization_records(evidence)

    assert result["completeness"]["state"] == "partial"
    assert not result["completeness"]["complete_for_executed_scope"]
    assert "evidence_status:partial" in result["completeness"]["partial_reasons"]
    assert "retrieval_cursor_not_exhausted" in result["completeness"]["partial_reasons"]
    row = next(row for row in result["records"]
               if row["recorded_qtl_signal_id"]["value"] == "PLEKHM1__exon__credibleSet3")
    assert row["recorded_pp_h4_abf"] == {"state": "unsupported_value", "value": None}
    assert row["recorded_source"] == {"state": "unsupported_value", "value": None}
    assert "nan" not in repr(result).lower()


def test_nested_requested_scope_incompleteness_is_not_misreported_as_complete():
    evidence = _evidence()
    evidence["evidence_coverage"] = {
        "query_scope": {"complete_for_requested_scope": False}
    }
    result = derive_colocalization_records(evidence)
    assert result["completeness"]["state"] == "partial"
    assert "requested_scope_not_complete" in result["completeness"]["partial_reasons"]
