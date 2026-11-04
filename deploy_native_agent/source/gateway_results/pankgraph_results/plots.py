"""Local scientific plots from measured statistics and verified coordinates only."""
from __future__ import annotations

import math
from pathlib import Path
import threading

PLOT_VERSION = "regional-nominal-p-pip-v1"
_PLOT_LOCK = threading.Lock()  # Matplotlib global state is not thread safe.


def plot_points(rows: list[dict], coordinates: dict[str, dict]) -> tuple[list[dict], dict]:
    points = []
    omitted = {"unverified_coordinate": 0, "invalid_statistic": 0}
    for row in rows:
        coordinate = coordinates.get(str(row.get("snp")), {})
        chrom = str(coordinate.get("chrom", "")).removeprefix("chr")
        position = coordinate.get("pos")
        if (coordinate.get("verified") is not True or coordinate.get("assembly") != "GRCh38"
                or not coordinate.get("source") or chrom not in {*(str(i) for i in range(1, 23)), "X", "Y", "MT"}
                or isinstance(position, bool) or not isinstance(position, int) or position < 1):
            omitted["unverified_coordinate"] += 1
            continue
        try:
            p, pip = float(row["nominal_p"]), float(row["pip"])
        except (KeyError, ValueError, TypeError):
            omitted["invalid_statistic"] += 1
            continue
        if not (math.isfinite(p) and 0 < p <= 1 and math.isfinite(pip) and 0 <= pip <= 1):
            omitted["invalid_statistic"] += 1
            continue
        points.append({"snp": str(row["snp"]), "chrom": chrom, "pos": position,
                       "minus_log10_p": -math.log10(p), "pip": pip,
                       "coordinate_source": str(coordinate["source"])})
    coverage = {"input_rows": len(rows), "plotted_rows": len(points), "omitted": omitted,
                "assembly": "GRCh38", "coordinate_sources": sorted({p["coordinate_source"] for p in points}),
                "ld_available": False}
    return points, coverage


def render_regional_plot(rows: list[dict], coordinates: dict[str, dict], output: Path,
                         *, title: str = "Regional association and fine-mapping evidence") -> dict:
    """Blocking CPU work: callers must run this in a bounded worker thread."""
    points, coverage = plot_points(rows, coordinates)
    if not points:
        return {"status": "unavailable", "error_category": "verified_coordinates_or_statistics_missing",
                "coverage": coverage}
    if len({point["chrom"] for point in points}) != 1:
        return {"status": "unavailable", "error_category": "multiple_chromosomes_in_regional_set", "coverage": coverage}
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
    except ImportError:
        return {"status": "unavailable", "error_category": "plot_dependency_unavailable", "coverage": coverage}
    with _PLOT_LOCK:
        fig = Figure(figsize=(9, 5.4), constrained_layout=True)
        FigureCanvasAgg(fig)
        upper, lower = fig.subplots(2, 1, sharex=True)
        xs = [point["pos"] / 1_000_000 for point in points]
        upper.scatter(xs, [point["minus_log10_p"] for point in points], s=25, color="#186d9b", alpha=.85)
        lower.scatter(xs, [point["pip"] for point in points], s=25, color="#127c67", alpha=.85)
        upper.set_ylabel("−log₁₀(nominal P)")
        lower.set_ylabel("PIP")
        lower.set_ylim(-.03, 1.03)
        lower.set_xlabel(f"Chromosome {points[0]['chrom']} position (Mb; GRCh38)")
        upper.set_title(str(title)[:160], fontsize=11)
        for axis in (upper, lower):
            axis.grid(axis="y", color="#e4e8ed", linewidth=.7)
            axis.set_axisbelow(True)
            axis.spines[["top", "right"]].set_visible(False)
            axis.ticklabel_format(axis="x", style="plain", useOffset=False)
        fig.supxlabel(f"{len(points)}/{len(rows)} variants with verified coordinates and valid statistics. No LD data encoded.", fontsize=8)
        fig.savefig(output, format="png", dpi=150, metadata={"Software": PLOT_VERSION})
        fig.clear()
    return {"status": "available", "coverage": coverage, "plot_version": PLOT_VERSION,
            "media_type": "image/png", "statistics": ["nominal_p", "pip"]}
