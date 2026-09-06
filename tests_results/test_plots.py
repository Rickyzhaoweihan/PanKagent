import importlib.util
from pathlib import Path

import pytest

from pankgraph_results.plots import plot_points, render_regional_plot


ROWS = [{"snp": "rs100", "nominal_p": .001, "pip": .8}, {"snp": "rs101", "nominal_p": .1, "pip": .2}]
COORDINATES = {snp: {"chrom": "chr1", "pos": pos, "assembly": "GRCh38", "verified": True, "source": "local_dbsnp_vcf"}
               for snp, pos in (("rs100", 100100), ("rs101", 100200))}


def test_plot_uses_actual_nominal_p_and_pip_only():
    points, coverage = plot_points(ROWS, COORDINATES)
    assert points[0]["minus_log10_p"] == 3
    assert points[0]["pip"] == .8
    assert coverage["plotted_rows"] == 2 and not coverage["ld_available"]


@pytest.mark.parametrize("change", [{"assembly": "GRCh37"}, {"verified": False}, {"source": ""}, {"pos": -1}, {"chrom": "unknown"}])
def test_unverified_or_wrong_assembly_coordinates_are_excluded(change):
    coordinates = {"rs100": {**COORDINATES["rs100"], **change}}
    points, coverage = plot_points(ROWS, coordinates)
    assert not points and coverage["omitted"]["unverified_coordinate"] == 2


def test_zero_p_is_not_silently_clamped_to_invented_significance():
    points, coverage = plot_points([{**ROWS[0], "nominal_p": 0}], COORDINATES)
    assert not points and coverage["omitted"]["invalid_statistic"] == 1


def test_no_fake_empty_plot_or_mixed_chromosome_regional_plot(tmp_path):
    result = render_regional_plot(ROWS, {}, tmp_path / "missing.png")
    assert result["status"] == "unavailable" and not (tmp_path / "missing.png").exists()
    coordinates = {**COORDINATES, "rs101": {**COORDINATES["rs101"], "chrom": "2"}}
    result = render_regional_plot(ROWS, coordinates, tmp_path / "mixed.png")
    assert result["error_category"] == "multiple_chromosomes_in_regional_set"


@pytest.mark.skipif(importlib.util.find_spec("matplotlib") is None, reason="Optional plotting dependency unavailable")
def test_real_png_is_exported_with_truthful_coordinate_coverage(tmp_path):
    output = tmp_path / "plot.png"
    result = render_regional_plot(ROWS, COORDINATES, output)
    assert result["status"] == "available"
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert result["statistics"] == ["nominal_p", "pip"]
    assert result["coverage"]["plotted_rows"] == 2
