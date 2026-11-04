"""Compile pinned interpretation guidance from evidence schema, without inference.

Files are read and verified at startup only. Request values never become routing
instructions; the bounded cache contains schema signatures and trusted text.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import time


ROUTER_VERSION = "1.0.0"
BUNDLE = Path(__file__).parent / "answer_skills"
CLINICAL_KEYS = frozenset({"t1d_stage", "diabetes_type", "derived_diabetes_status", "aab_state", "aab_status", "aab_count", "gada", "ia2", "iaa", "znt8"})
FEATURE_FIELDS = frozenset({"feature", "feature_name", "trait", "trait_name"})
METADATA_FIELDS = frozenset({"trait_meta", "trace_meta", "clinical_metadata", "functional_metadata"})


@dataclass(frozen=True)
class RoutedSkills:
    guidance: str
    profile: dict


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AnswerSkillRouter:
    def __init__(self, bundle: Path = BUNDLE, *, max_chars: int = 18_000, cache_size: int = 128):
        if max_chars < 1000 or cache_size < 1:
            raise ValueError("invalid_answer_router_limits")
        self.max_chars, self.cache_size = max_chars, cache_size
        self._cache = OrderedDict()
        bundle = Path(bundle).resolve()
        manifest_bytes = (bundle / "manifest.json").read_bytes()
        self.manifest = json.loads(manifest_bytes)
        m = self.manifest
        if m.get("version") != 1 or not re.fullmatch(r"[0-9a-f]{40}", m.get("source", {}).get("commit", "")):
            raise ValueError("invalid_answer_skill_manifest")
        self.bundle_hash = hashlib.sha256(manifest_bytes).hexdigest()
        blobs = {}
        for relative, expected in m["sha256"].items():
            path = (bundle / relative).resolve()
            if not path.is_relative_to(bundle) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("invalid_answer_skill_path")
            blob = path.read_bytes()
            if hashlib.sha256(blob).hexdigest() != expected:
                raise ValueError("answer_skill_checksum_mismatch")
            blobs[relative] = blob
        self.files = {key: json.loads(blobs[path]) for key, path in m["files"].items()}
        if set(self.files) != {"schema", "functional", "general"}:
            raise ValueError("invalid_answer_skill_files")
        self.aliases = {}
        for kind in ("nodes", "edges"):
            aliases = {}
            for canonical, names in m["aliases"][kind].items():
                for name in [canonical, *names]:
                    if not isinstance(name, str) or not name.strip():
                        raise ValueError("invalid_answer_skill_alias")
                    key = name.strip().casefold()
                    if key in aliases and aliases[key] != canonical:
                        raise ValueError("ambiguous_answer_skill_alias")
                    aliases[key] = canonical
            self.aliases[kind] = aliases
        self.rules = []
        ids = set()
        for rule in m["rules"]:
            if rule["id"] in ids or rule["kind"] not in {"node", "edge", "composite"}:
                raise ValueError("invalid_answer_skill_rule")
            ids.add(rule["id"])
            match = rule["match"]
            if set(match) - {"nodes_any", "nodes_all", "edges_any", "edges_all", "min_edge_types"}:
                raise ValueError("unsupported_answer_skill_predicate")
            if not any(match.get(key) for key in ("nodes_any", "nodes_all", "edges_any", "edges_all")):
                raise ValueError("unbounded_answer_skill_rule")
            for kind in ("nodes", "edges"):
                for mode in ("any", "all"):
                    values = match.get(f"{kind}_{mode}", [])
                    if not isinstance(values, list) or not set(values) <= set(m["aliases"][kind]):
                        raise ValueError("unknown_answer_skill_type")
            if "min_edge_types" in match and (type(match["min_edge_types"]) is not int or not 1 <= match["min_edge_types"] <= len(match.get("edges_any", []))):
                raise ValueError("invalid_answer_skill_threshold")
            source = rule["source"]
            guidance = self.files[source["file"]][source["section"]][source["key"]]
            if not isinstance(guidance, str) or not guidance.strip():
                raise ValueError("invalid_answer_skill_text")
            self.rules.append((rule, guidance))
        self.features = {entry["feature"]: entry for entry in self.files["functional"]["feature_dictionary"]}
        if len(self.features) != len(self.files["functional"]["feature_dictionary"]):
            raise ValueError("duplicate_answer_skill_feature")

    def _scan(self, evidence):
        steps = list(evidence.values()) if isinstance(evidence, Mapping) else evidence
        found = {"nodes": set(), "edges": set()}
        unknown = {"nodes": set(), "edges": set()}
        features, clinical = set(), set()

        def label(kind, value):
            if not isinstance(value, str):
                return
            canonical = self.aliases[kind].get(value.strip().casefold())
            if canonical:
                found[kind].add(canonical)
            elif kind == "nodes" and ";" in value:
                # Legacy CSV multi-label strings are sets, not substring matches.
                for token in value.split(";"):
                    if token.strip():
                        label(kind, token)
            else:
                # Only schema strings enter the profile, never arbitrary property values.
                unknown[kind].add(value[:128])

        def properties(data, nested=True):
            if not isinstance(data, Mapping):
                return
            for key, value in data.items():
                if key in self.features:
                    features.add(key)
                lowered = str(key).casefold()
                if lowered in CLINICAL_KEYS:
                    clinical.add(lowered)
                if lowered in FEATURE_FIELDS and isinstance(value, str) and value in self.features:
                    features.add(value)
                if nested and lowered in METADATA_FIELDS:
                    properties(value, False)

        for step in steps:
            if not isinstance(step, Mapping):
                continue
            for node in step.get("nodes", []):
                labels = node.get("labels", [])
                for value in ([labels] if isinstance(labels, str) else labels):
                    label("nodes", value)
                properties(node.get("properties", {}))
            for edge in step.get("edges", []):
                label("edges", edge.get("type"))
                properties(edge.get("properties", {}))
            for row in step.get("rows", []):
                properties(row)
        return found, features, clinical, unknown

    @staticmethod
    def _matches(match, found):
        for kind in ("nodes", "edges"):
            any_types, all_types = set(match.get(f"{kind}_any", [])), set(match.get(f"{kind}_all", []))
            if any_types and not any_types & found[kind]:
                return False
            if not all_types <= found[kind]:
                return False
        minimum = match.get("min_edge_types", 0)
        return len(set(match.get("edges_any", [])) & found["edges"]) >= minimum

    def _compile(self, found, features, clinical):
        chunks = []
        if clinical:
            chunks.append(("clinical.recorded_t1d_stage", "clinical", self.files["general"], {"file": "general"}))
        if features:
            functional = self.files["functional"]
            chunks.append(("functional.measurement_rules", "functional", {
                "core_rules": functional["core_rules"], "global_terms": functional["global_terms"]
            }, {"file": "functional", "section": "core_rules/global_terms"}))
            for feature in sorted(features):
                chunks.append(("functional.feature:" + feature, "functional", self.features[feature],
                               {"file": "functional", "section": "feature_dictionary", "key": feature}))
        # Interpretation cautions take precedence over generic node descriptions.
        matches = [(rule, text) for rule, text in self.rules if self._matches(rule["match"], found)]
        matches.sort(key=lambda item: ({"edge": 0, "composite": 1, "node": 2}[item[0]["kind"]], item[0]["id"]))
        for rule, text in matches:
            chunks.append((rule["id"], rule["kind"], text, rule["source"]))
        selected, omitted, sources, text_parts = [], [], {}, []
        used = 0
        for rule_id, kind, content, source in chunks:
            identity = _json(source)
            if identity in sources:
                selected.append({"id": rule_id, "kind": kind, "source": source, "shared_guidance_with": sources[identity]})
                continue
            text = content if isinstance(content, str) else _json(content)
            block = f"\n[{rule_id}]\n{text}\n"
            if used + len(block) > self.max_chars:
                omitted.append(rule_id)
                continue
            used += len(block)
            sources[identity] = rule_id
            text_parts.append(block)
            selected.append({"id": rule_id, "kind": kind, "source": source})
        return "".join(text_parts), {"selected_rules": selected, "omitted_rules": omitted, "guidance_chars": used}

    def select(self, evidence) -> RoutedSkills:
        start = time.perf_counter()
        found, features, clinical, unknown = self._scan(evidence)
        scanned = time.perf_counter()
        signature = _json({"nodes": sorted(found["nodes"]), "edges": sorted(found["edges"]),
                           "features": sorted(features), "clinical_fields": sorted(clinical)})
        cache_hit = signature in self._cache
        if cache_hit:
            guidance, details = self._cache.pop(signature)
        else:
            guidance, details = self._compile(found, features, clinical)
        self._cache[signature] = guidance, details
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        profile = deepcopy(details)
        profile.update({"router_version": ROUTER_VERSION, "bundle_version": self.manifest["bundle_version"],
                        "source_commit": self.manifest["source"]["commit"], "bundle_sha256": self.bundle_hash,
                        "profile_id": hashlib.sha256((ROUTER_VERSION + self.bundle_hash + str(self.max_chars) + signature).encode()).hexdigest()[:24],
                        "matched_schema": {kind: sorted(values) for kind, values in found.items()},
                        "functional_features": sorted(features), "clinical_fields": sorted(clinical),
                        "unknown_schema": {kind: sorted(values)[:64] for kind, values in unknown.items()},
                        "unknown_schema_counts": {kind: len(values) for kind, values in unknown.items()},
                        "cache_hit": cache_hit})
        end = time.perf_counter()
        profile["timing_ms"] = {"scan": round((scanned-start)*1000, 4), "compile": round((end-scanned)*1000, 4), "total": round((end-start)*1000, 4)}
        return RoutedSkills(guidance, profile)


def followup_questions(evidence):
    """Use the bundled interpretation rules and actual returned schema only."""
    if isinstance(evidence, dict) and 'steps' in evidence:
        steps = evidence['steps']
    elif isinstance(evidence, dict):
        steps = list(evidence.values())
    else:
        steps = list(evidence or [])
    usable = [s for s in steps if s.get('status') in {'complete', 'partial'}]
    kinds = {e.get('type', '') for s in usable for e in s.get('edges', [])}
    genes = sorted({str(n.get('properties', {}).get('name')) for s in usable for n in s.get('nodes', [])
                    if 'Gene' in n.get('labels', []) and n.get('properties', {}).get('name')})
    subject = ', '.join(genes[:2]) or 'these results'
    questions = []
    for rule in _followup_rules():
        if kinds.intersection(rule['relations']):
            questions.append(rule['question'].replace('{subject}', subject))
    return questions[:3]


from functools import lru_cache

@lru_cache(maxsize=1)
def _followup_rules():
    return json.loads((BUNDLE / 'manifest.json').read_text()).get('followup_rules', [])
