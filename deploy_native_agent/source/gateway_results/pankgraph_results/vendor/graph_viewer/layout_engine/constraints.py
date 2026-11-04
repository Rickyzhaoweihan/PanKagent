from typing import Any, Dict, Iterable, Set


def apply_layer_spacing(
    coords: Dict[str, Dict[str, Any]],
    layout_metadata: Dict[str, Any],
    fixed_node_ids: Iterable[str],
    layer_spacing: float,
) -> Set[str]:
    """Apply a configurable gap while preserving lane order and membership."""
    layout = layout_metadata.get("layout") or {}
    region = layout.get("genome_region") or {}
    lanes = region.get("lanes") or []
    old_levels = sorted(
        {
            round(float(lane["y"]), 6)
            for lane in lanes
            if isinstance(lane, dict) and isinstance(lane.get("y"), (int, float))
        }
    )
    if not old_levels:
        return set()
    level_map = {
        old_y: round(index * float(layer_spacing), 3)
        for index, old_y in enumerate(old_levels)
    }

    fixed = set(fixed_node_ids)
    for node_id in fixed:
        point = coords.get(node_id)
        if not point:
            continue
        old_y = round(float(point["y"]), 6)
        if old_y in level_map:
            point["y"] = level_map[old_y]

    def update_lanes(container: Any) -> None:
        if not isinstance(container, dict):
            return
        container_lanes = container.get("lanes")
        if isinstance(container_lanes, list):
            for lane in container_lanes:
                if not isinstance(lane, dict) or not isinstance(lane.get("y"), (int, float)):
                    continue
                old_y = round(float(lane["y"]), 6)
                if old_y in level_map:
                    lane["y"] = level_map[old_y]
        groups = container.get("groups")
        if isinstance(groups, list):
            for group in groups:
                update_lanes(group)

    update_lanes(region)
    tracks = layout.get("genome_tracks")
    update_lanes(tracks)
    if isinstance(tracks, dict):
        tracks["min_y"] = min(level_map.values())
        tracks["max_y"] = max(level_map.values())
    return {node_id for node_id in fixed if node_id in coords}
