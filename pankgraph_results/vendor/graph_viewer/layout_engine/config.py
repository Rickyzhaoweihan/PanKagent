import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class LayoutConfig:
    layer_spacing: float = 78.0
    node_gap: float = 16.0
    edge_clearance: float = 8.0
    edge_spacing: float = 10.0
    label_clearance: float = 12.0
    label_padding: float = 4.0
    max_iterations: int = 6
    time_budget_ms: float = 750.0

    def fingerprint(self, layout_mode: str, track_policy: str = "") -> str:
        payload = {"layout_mode": layout_mode, "track_policy": track_policy, **asdict(self)}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:16]


_PUBLIC_RANGES = {
    "layer_spacing": (40.0, 240.0),
    "node_gap": (0.0, 80.0),
    "edge_clearance": (0.0, 40.0),
    "edge_spacing": (2.0, 40.0),
    "label_clearance": (4.0, 48.0),
    "label_padding": (0.0, 20.0),
}


def parse_layout_config(value: Any) -> LayoutConfig:
    if value is None:
        return LayoutConfig()
    if not isinstance(value, dict):
        raise ValueError("layout_options must be an object.")

    unknown = sorted(set(value) - set(_PUBLIC_RANGES))
    if unknown:
        raise ValueError(f"Unknown layout option(s): {', '.join(unknown)}.")

    parsed: Dict[str, float] = {}
    for name, (minimum, maximum) in _PUBLIC_RANGES.items():
        if name not in value:
            continue
        raw = value[name]
        if isinstance(raw, bool):
            raise ValueError(f"layout_options.{name} must be numeric.")
        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"layout_options.{name} must be numeric.") from None
        if not minimum <= number <= maximum:
            raise ValueError(
                f"layout_options.{name} must be between {minimum:g} and {maximum:g}."
            )
        parsed[name] = number
    return LayoutConfig(**parsed)
