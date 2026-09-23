"""Verified fixed-path planning and compilation for two to four node roles.

The contract is deliberately smaller than general Cypher.  A bounded path is
one connected, acyclic sequence with explicit node and relationship roles.  It
never permits variable-length traversal, branching, OPTIONAL patterns, or a
model-generated substitute when deterministic compilation fails.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST


VERSION = "bounded-path-v1"
CHAIN_FACTS_VERSION = "bounded-path-chain-facts-v1"
MAX_PATH_RECORDS = 2000
PATH_RECORD_OVERFETCH = MAX_PATH_RECORDS + 1
_ROLE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_INTERACTIONS = {"PHYSICAL_INTERACTION", "GENETIC_INTERACTION"}


PATH_SPEC_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "version": {"type": "string", "enum": [VERSION]},
        "nodes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "role": {"type": "string"},
                "entity_types": {"type": "array", "items": {
                    "type": "string", "enum": sorted(REGISTRY["nodes"])}},
                "distinct_from": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["role", "entity_types"],
        }},
        "edges": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "role": {"type": "string"},
                "from": {"type": "string"},
                "to": {"type": "string"},
                "types_any": {"type": "array", "items": {
                    "type": "string", "enum": sorted(REGISTRY["relations"])}},
                "direction": {"type": "string", "enum": ["out", "in", "either"]},
            },
            "required": ["role", "from", "to", "types_any", "direction"],
        }},
    },
    "required": ["version", "nodes", "edges"],
}


class BoundedPathError(ValueError):
    """A recognized fixed-path request cannot be represented safely."""


def _type_pairs(edge, left_types, right_types):
    """Return release-supported semantic endpoint pairs for one path edge."""
    result = set()
    for kind in edge["types_any"]:
        kind_pairs = set()
        for path in REGISTRY["relations"][kind]["paths"]:
            source, target = set(path["source"]), set(path["target"])
            if edge["direction"] in {"out", "either"}:
                kind_pairs.update((left, right) for left in left_types & source
                                  for right in right_types & target)
            if edge["direction"] in {"in", "either"}:
                kind_pairs.update((left, right) for left in left_types & target
                                  for right in right_types & source)
        if not kind_pairs:
            return set()
        result.update(kind_pairs)
    return result


def annotation_edge_role(step):
    """Return the unique FUNCTION_ANNOTATION edge role, or ``None``."""
    spec = step.get("path_spec") or {}
    roles = [edge.get("role") for edge in spec.get("edges", [])
             if edge.get("types_any") == ["FUNCTION_ANNOTATION"]]
    return roles[0] if len(roles) == 1 else None


def plan_issue(step):
    """Return one value-free structural error for an optional path contract."""
    spec = step.get("path_spec")
    if spec is None:
        return None
    if not isinstance(spec, dict) or set(spec) != {"version", "nodes", "edges"}:
        return "malformed_path_spec"
    if spec.get("version") != VERSION:
        return "unsupported_path_version"
    nodes, edges = spec.get("nodes"), spec.get("edges")
    if not isinstance(nodes, list) or not 2 <= len(nodes) <= 4:
        return "path_node_depth_out_of_range"
    if not isinstance(edges, list) or len(edges) != len(nodes) - 1:
        return "path_edge_count_mismatch"
    if step.get("depends_on") and not step.get("input_bindings"):
        return "bounded_path_dependencies_unsupported"

    node_roles = []
    domains = {}
    for index, node in enumerate(nodes):
        if (not isinstance(node, dict)
                or set(node) - {"role", "entity_types", "distinct_from"}):
            return "malformed_path_node"
        role, kinds = node.get("role"), node.get("entity_types")
        if not isinstance(role, str) or not _ROLE.fullmatch(role):
            return "invalid_path_role"
        if role in node_roles:
            return "duplicate_path_role"
        if (not isinstance(kinds, list) or not kinds
                or len(set(kinds)) != len(kinds)
                or any(kind not in REGISTRY["nodes"] for kind in kinds)):
            return "invalid_path_node_types"
        distinct = node.get("distinct_from", [])
        if (not isinstance(distinct, list) or len(set(distinct)) != len(distinct)
                or any(other not in node_roles for other in distinct)):
            return "invalid_path_distinct_role"
        node_roles.append(role)
        domains[role] = set(kinds)

    edge_roles = []
    for index, edge in enumerate(edges):
        if (not isinstance(edge, dict)
                or set(edge) != {"role", "from", "to", "types_any", "direction"}):
            return "malformed_path_edge"
        role, kinds = edge.get("role"), edge.get("types_any")
        if not isinstance(role, str) or not _ROLE.fullmatch(role):
            return "invalid_path_role"
        if role in node_roles or role in edge_roles:
            return "duplicate_path_role"
        if edge.get("from") != node_roles[index] or edge.get("to") != node_roles[index + 1]:
            return "path_edges_not_in_node_order"
        if (not isinstance(kinds, list) or not kinds
                or len(set(kinds)) != len(kinds)
                or any(kind not in REGISTRY["relations"] for kind in kinds)):
            return "invalid_path_relation_types"
        if edge.get("direction") not in {"out", "in", "either"}:
            return "invalid_path_direction"
        if edge["direction"] == "either" and not set(kinds) <= _INTERACTIONS:
            return "undirected_noninteraction_path"
        edge_roles.append(role)

    # Arc consistency across the whole path prevents adjacent edges from using
    # mutually incompatible interpretations of a multi-typed intermediate role.
    for _ in range(len(nodes) + 1):
        changed = False
        for edge in edges:
            pairs = _type_pairs(edge, domains[edge["from"]], domains[edge["to"]])
            if not pairs:
                return "path_schema_endpoint_mismatch"
            left = {pair[0] for pair in pairs}
            right = {pair[1] for pair in pairs}
            narrowed_left = domains[edge["from"]] & left
            narrowed_right = domains[edge["to"]] & right
            if not narrowed_left or not narrowed_right:
                return "path_schema_endpoint_mismatch"
            changed |= narrowed_left != domains[edge["from"]]
            changed |= narrowed_right != domains[edge["to"]]
            domains[edge["from"]] = narrowed_left
            domains[edge["to"]] = narrowed_right
        if not changed:
            break

    declared = {kind for edge in edges for kind in edge["types_any"]}
    relations = step.get("relation_types")
    if (not isinstance(relations, list)
            or any(not isinstance(kind, str) or kind not in REGISTRY["relations"]
                   for kind in relations)
            or len(set(relations)) != len(relations)
            or set(relations) != declared):
        return "path_relation_contract_mismatch"
    # Preserve the existing colocalization evidence boundary.  A recorded
    # Gene->disease colocalization must not be made conditional on separately
    # indexed QTL/GWAS membership edges inside one path.
    if ("SIGNAL_COLOC_WITH" in declared
            and declared & {"PART_OF_GWAS_SIGNAL", "PART_OF_QTL_SIGNAL"}):
        return "coloc_requires_independent_evidence_checks"
    if step.get("evidence_combination") != "cooccurrence":
        return "bounded_path_requires_cooccurrence"
    if not step.get("complete", True):
        return "bounded_path_requires_complete_scope"

    role_kinds = {node["role"]: "node" for node in nodes}
    role_kinds.update({edge["role"]: "edge" for edge in edges})
    explicit_node_owners = {}
    for constraint in step.get("constraints") or []:
        if not isinstance(constraint, dict):
            return "malformed_path_constraint"
        role = constraint.get("owner_role")
        if role not in role_kinds:
            return "missing_path_constraint_owner_role"
        relationship = constraint.get("relationship_type")
        if role_kinds[role] == "node":
            if (constraint.get("owner_kind") == "relationship" or relationship
                    or constraint.get("owner_kind") not in {None, "node"}):
                return "path_constraint_owner_mismatch"
            node = next(item for item in nodes if item["role"] == role)
            entity = constraint.get("entity_type")
            if entity is not None and entity not in domains[role]:
                return "path_constraint_owner_mismatch"
            if (entity is not None and role in explicit_node_owners
                    and explicit_node_owners[role] != entity):
                return "path_constraint_owner_mismatch"
            if entity is not None:
                explicit_node_owners[role] = entity
            supported = set.intersection(*(set(REGISTRY["nodes"][kind])
                                           for kind in ([entity] if entity else node["entity_types"])))
            if constraint.get("property") not in supported:
                return "path_constraint_owner_mismatch"
        else:
            if (constraint.get("entity_type") is not None
                    or constraint.get("owner_kind") not in {None, "relationship"}):
                return "path_constraint_owner_mismatch"
            edge = next(item for item in edges if item["role"] == role)
            if relationship and relationship not in edge["types_any"]:
                return "path_constraint_owner_mismatch"

    anchor = node_roles[0]
    dependency_anchor = any(b.get("target_role") == anchor and b.get("entity_type") in domains[anchor]
                            for b in step.get("input_bindings", []))
    if not dependency_anchor and not any(c.get("owner_role") == anchor and c.get("entity_type") in domains[anchor]
               and c.get("property") in {"id", "name", "hgnc_symbol"}
               and c.get("operator", "=") == "="
               for c in step.get("constraints") or [] if isinstance(c, dict)):
        return "missing_path_anchor_identity"
    return None


def _node_pattern(index, node, explicit_type=None):
    labels = [explicit_type] if explicit_type else node["entity_types"]
    return f"(n{index}:`{labels[0]}`)" if len(labels) == 1 else f"(n{index})"


def _node_type_filter(index, node):
    """Return an explicit label union for roles with more than one type.

    A bare node would let an unrelated endpoint satisfy a multi-type role.  A
    WHERE label predicate keeps the compiled topology portable across the two
    pathway labels without relying on endpoint inference in the validator.
    """
    labels = node["entity_types"]
    if len(labels) <= 1:
        return None
    return "(" + " OR ".join(f"n{index}:`{label}`" for label in labels) + ")"


def compile_query(step):
    """Compile one validated path without a model fallback."""
    issue = plan_issue(step)
    if issue:
        raise BoundedPathError(issue)
    if step.get("graph_version") != REGISTRY["release"]:
        raise BoundedPathError("path_graph_release_mismatch")
    if any(step.get(key) for key in ("ranking", "ranking_issue", "semantic_issues",
                                     "anatomy_scope_issue", "coloc_scope_issue")):
        raise BoundedPathError("path_scope_unresolved")

    from .query_templates import (_OPERATORS, _parameter_binding,
                                  _resolved_entity, _value)
    spec = step["path_spec"]
    node_by_role = {node["role"]: (index, node)
                    for index, node in enumerate(spec["nodes"])}
    edge_by_role = {edge["role"]: (index, edge)
                    for index, edge in enumerate(spec["edges"])}
    explicit_node_types = {
        constraint["owner_role"]: constraint["entity_type"]
        for constraint in step.get("constraints") or []
        if constraint.get("owner_role") in node_by_role
        and isinstance(constraint.get("entity_type"), str)
    }

    patterns = []
    for index, edge in enumerate(spec["edges"]):
        left_node, right_node = spec["nodes"][index:index + 2]
        left = _node_pattern(index, left_node, explicit_node_types.get(left_node["role"]))
        right = _node_pattern(index + 1, right_node,
                              explicit_node_types.get(right_node["role"]))
        types = "|".join(f"`{kind}`" for kind in edge["types_any"])
        relationship = f"[r{index}:{types}]"
        if edge["direction"] == "out":
            pattern = f"{left}-{relationship}->{right}"
        elif edge["direction"] == "in":
            pattern = f"{left}<-{relationship}-{right}"
        else:
            pattern = f"{left}-{relationship}-{right}"
        patterns.append("MATCH " + pattern)

    filters = [value for index, node in enumerate(spec["nodes"])
               if (value := _node_type_filter(index, node))]
    for right_index, right in enumerate(spec["nodes"]):
        for left_index, _left in enumerate(spec["nodes"][:right_index]):
            filters.append(f"n{right_index} <> n{left_index}")
    parameters, bindings = {}, {}
    anchor_resolved = False
    try:
        for index, constraint in enumerate(step.get("constraints") or []):
            role = constraint["owner_role"]
            prop, operator, value = (constraint.get("property", ""),
                                     constraint.get("operator", "="),
                                     constraint.get("value"))
            if operator not in _OPERATORS or operator in {">", ">=", "<", "<="}:
                raise BoundedPathError("unsupported_path_constraint_operator")
            resolved = None
            if role in node_by_role:
                node_index, node = node_by_role[role]
                resolved = _resolved_entity(step, index, constraint)
                owner = constraint.get("entity_type")
                if resolved is not None:
                    if resolved["entity_type"] not in node["entity_types"]:
                        raise BoundedPathError("path_constraint_owner_mismatch")
                    owner, prop, value = resolved["entity_type"], "id", resolved["id"]
                    anchor_resolved |= role == spec["nodes"][0]["role"]
                owner_domains = [owner] if owner else node["entity_types"]
                allowed = set.intersection(*(set(REGISTRY["nodes"][kind])
                                             for kind in owner_domains))
                if (owner is not None and owner not in node["entity_types"]) or prop not in allowed:
                    raise BoundedPathError("unsupported_path_node_property")
                variable = f"n{node_index}"
                if owner is not None and len(node["entity_types"]) > 1:
                    label_filter = f"{variable}:`{owner}`"
                    if label_filter not in filters:
                        filters.append(label_filter)
                binding_owner = owner or "|".join(sorted(node["entity_types"]))
            else:
                edge_index, edge = edge_by_role[role]
                relationship = constraint.get("relationship_type")
                if relationship and (len(edge["types_any"]) != 1
                                     or relationship != edge["types_any"][0]):
                    raise BoundedPathError("ambiguous_path_edge_property")
                allowed = set.intersection(*(set(REGISTRY["relations"][kind]["properties"])
                                             for kind in edge["types_any"]))
                if prop not in allowed:
                    raise BoundedPathError("unsupported_path_edge_property")
                variable = f"r{edge_index}"
                binding_owner = relationship or "|".join(sorted(edge["types_any"]))
            parameter = "path_" + str(index)
            parameters[parameter] = _value(value, operator)
            bindings[parameter] = _parameter_binding(
                step, index, constraint, binding_owner, prop, operator, resolved)
            bindings[parameter]["owner_role"] = role
            cypher_operator = "<>" if operator == "!=" else operator
            filters.append(f"{variable}.`{prop}` {cypher_operator} ${parameter}")
    except BoundedPathError:
        raise
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise BoundedPathError(str(exc) or "path_constraint_compilation_failed") from exc

    for index, dependency in enumerate(step.get('depends_on', [])):
        binding = next((b for b in step.get('input_bindings', []) if b['step_id'] == dependency), None)
        trusted = (step.get('_path_dependency_parameters') or {}).get('dep_' + str(index))
        if binding is None or not trusted or trusted.get('graph_version') != step.get('graph_version'):
            raise BoundedPathError('unverified_path_dependency')
        role_index, node = node_by_role[binding['target_role']]
        if binding['entity_type'] not in node['entity_types'] or not trusted.get('ids'):
            raise BoundedPathError('invalid_path_dependency_type')
        name = 'dep_' + str(index)
        parameters[name] = trusted['ids']
        bindings[name] = {'owner_role': binding['target_role'], 'proof_source': 'dependency_evidence',
                         'graph_release': step['graph_version'], 'operator': 'IN', 'property': 'id'}
        filters.append(f'n{role_index}.`id` IN ${name}')
        anchor_resolved |= role_index == 0
    if not anchor_resolved:
        raise BoundedPathError("unresolved_path_anchor")
    if not filters:
        raise BoundedPathError("missing_path_filters")

    node_projection = []
    edge_projection = []
    for index in range(len(spec["edges"])):
        node_projection.append(f"n{index}")
        edge_projection.append(f"r{index}")
    node_projection.append(f"n{len(spec['nodes']) - 1}")
    parameters["path_record_overfetch"] = PATH_RECORD_OVERFETCH
    bindings["path_record_overfetch"] = {
        "owner_role": "bounded_path_transport",
        "proof_source": "bounded_path_contract",
        "proof_kind": "deterministic_materialization_guard",
        "graph_release": step.get("graph_version"),
    }
    cypher = "\n".join(patterns) + "\nWHERE " + " AND ".join(filters)
    # One aggregate row avoids the global max_rows boundary for legitimate
    # paths with >1000 matches.  A deterministic one-record overfetch detects
    # incompleteness without allowing an unbounded server/client collection.
    projected = node_projection + edge_projection
    # Relationship element IDs are used only as an in-query tie breaker.  They
    # are never returned or treated as durable evidence; the public edge
    # fingerprint below remains the evidence identity.  Avoid ordering by a
    # complete relationship property map because the generic privacy guard
    # correctly forbids that shape when a donor role is present.
    order = ([f"n{index}.`id`" for index in range(len(spec["nodes"]))]
             + [value for index in range(len(spec["edges"]))
                for value in (f"type(r{index})", f"elementId(r{index})")])
    cypher += ("\nWITH DISTINCT " + ", ".join(projected)
               + "\nORDER BY " + ", ".join(order)
               + "\nLIMIT $path_record_overfetch"
               + "\nRETURN collect({nodes: [" + ", ".join(node_projection)
               + "], edges: [" + ", ".join(edge_projection)
               + "]}) AS path_records")
    return {
        "cypher": cypher,
        "parameters": parameters,
        "parameter_bindings": bindings,
        "template_id": "bounded_path_records",
        "version": VERSION,
        "sha256": DIGEST,
        "schema_sha256": SCHEMA_DIGEST,
        "endpoint_coverage": {
            "path_version": VERSION,
            "node_roles": [node["role"] for node in spec["nodes"]],
            "edge_roles": [edge["role"] for edge in spec["edges"]],
            "node_depth": len(spec["nodes"]),
            "all_requested_paths_covered": True,
            "scope_basis": "verified_fixed_path",
        },
    }


def topology_errors(tokens, step, parameters, tokenize):
    """Require the executed query to be exactly the deterministic fixed path."""
    if not step.get("path_spec"):
        return []
    try:
        expected = compile_query(step)
        expected_tokens = tokenize(expected["cypher"])
    except (BoundedPathError, ValueError, TypeError, KeyError) as exc:
        return ["unsupported_bounded_path_spec:" + (str(exc) or "compile_failed")]
    signature = lambda values: [(token.kind, token.value) for token in values]
    errors = []
    if signature(tokens) != signature(expected_tokens):
        errors.append("bounded_path_query_not_deterministic")
    if parameters != expected["parameters"]:
        errors.append("bounded_path_parameter_mismatch")
    return errors


def extract_path_records(step, rows, nodes):
    """Convert deterministic query rows to ordered, role-preserving evidence."""
    if not step.get("path_spec"):
        return []
    spec = step["path_spec"]
    node_index = {str(node["id"]): node for node in nodes
                  if isinstance(node, dict) and node.get("id") is not None}
    records, seen = [], set()
    if not rows:
        return []
    if (len(rows) != 1 or not isinstance(rows[0], dict)
            or not isinstance(rows[0].get("path_records"), list)):
        raise BoundedPathError("invalid_path_result_row")
    for row in rows[0]["path_records"]:
        if not isinstance(row, dict):
            raise BoundedPathError("invalid_path_result_row")
        if (set(row) != {"nodes", "edges"}
                or not isinstance(row["nodes"], list)
                or not isinstance(row["edges"], list)
                or len(row["nodes"]) != len(spec["nodes"])
                or len(row["edges"]) != len(spec["edges"])):
            raise BoundedPathError("invalid_path_result_shape")
        ordered_nodes, ordered_edges = [], []
        node_ids = []
        for index, node in enumerate(spec["nodes"]):
            values = row.get("nodes")
            wrapper = values[index] if isinstance(values, list) and index < len(values) else None
            identifier = wrapper.get("node_id") if isinstance(wrapper, dict) else None
            if not isinstance(identifier, str) or identifier not in node_index:
                raise BoundedPathError("invalid_path_result_node")
            labels = sorted(node_index[identifier].get("labels") or [])
            if not set(labels) & set(node["entity_types"]):
                raise BoundedPathError("invalid_path_result_node_type")
            node_ids.append(identifier)
            ordered_nodes.append({"role": node["role"], "id": identifier,
                                  "labels": labels})
        if len(set(node_ids)) != len(node_ids):
            raise BoundedPathError("cyclic_path_result")
        for index, edge in enumerate(spec["edges"]):
            values = row.get("edges")
            wrapper = values[index] if isinstance(values, list) and index < len(values) else None
            value = wrapper.get("edge") if isinstance(wrapper, dict) else None
            fingerprint = wrapper.get("fingerprint") if isinstance(wrapper, dict) else None
            if (not isinstance(value, list) or len(value) != 3
                    or not all(isinstance(item, str) for item in value)
                    or not isinstance(fingerprint, str) or not fingerprint):
                raise BoundedPathError("invalid_path_result_edge")
            start, kind, end = value
            if kind not in edge["types_any"]:
                raise BoundedPathError("invalid_path_result_edge_type")
            left, right = node_ids[index], node_ids[index + 1]
            valid = ((start, end) == (left, right) if edge["direction"] == "out"
                     else (start, end) == (right, left) if edge["direction"] == "in"
                     else {start, end} == {left, right})
            if not valid:
                raise BoundedPathError("invalid_path_result_topology")
            ordered_edges.append({"role": edge["role"], "type": kind,
                                  "fingerprint": fingerprint,
                                  "start_id": start, "end_id": end})
        record = {"nodes": ordered_nodes, "edges": ordered_edges}
        key = json.dumps(record, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            records.append(record)
    def canonical_key(record):
        node_key = tuple((node["role"], node["id"], tuple(node["labels"]))
                         for node in record["nodes"])
        edge_key = tuple((edge["role"], edge["type"], edge["fingerprint"],
                          edge["start_id"], edge["end_id"])
                         for edge in record["edges"])
        return node_key, edge_key
    return sorted(records, key=canonical_key)


def derive_chain_facts(step, path_records, nodes, *, status, truncated):
    """Build a role-ordered answer ledger solely from verified path records."""
    spec = step.get("path_spec") or {}
    node_index = {str(node.get("id")): node for node in nodes
                  if isinstance(node, dict) and node.get("id") is not None}
    role_order = [node["role"] for node in spec.get("nodes", [])]
    edge_order = [edge["role"] for edge in spec.get("edges", [])]
    facts = []
    for record in path_records:
        item = {}
        for node in record["nodes"]:
            source = node_index.get(node["id"], {})
            props = source.get("properties") or {}
            item[node["role"]] = {
                "id": node["id"],
                "name": props.get("name") or props.get("hgnc_symbol") or node["id"],
                "labels": list(node["labels"]),
            }
        for edge in record["edges"]:
            item[edge["role"]] = {
                "type": edge["type"], "fingerprint": edge["fingerprint"],
                "start_id": edge["start_id"], "end_id": edge["end_id"],
            }
        facts.append(item)
    kind = ("partner_annotation" if role_order == ["focus", "partner", "process"]
            and edge_order == ["interaction", "annotation"]
            else "direct_annotation" if role_order == ["focus", "process"]
            and edge_order == ["annotation"]
            else "interaction_partner" if role_order == ["focus", "partner"]
            and edge_order == ["interaction"] else "bounded_path")
    complete = status in {"complete", "empty"} and not truncated
    return {
        "version": CHAIN_FACTS_VERSION,
        "path_version": VERSION,
        "kind": kind,
        "node_role_order": role_order,
        "edge_role_order": edge_order,
        "record_count": len(facts),
        "complete_for_requested_scope": complete,
        "truncated": bool(truncated),
        "records": facts,
    }


def _resolved_gene(grounding):
    candidates = {}
    surfaces = []
    for mention in grounding.get("mentions", []):
        values = mention.get("candidates") or []
        if (mention.get("state") == "resolved" and len(values) == 1
                and values[0].get("entity_type") == "Gene"):
            candidate = values[0]
            candidates[candidate["id"]] = candidate
            surfaces.append((str(mention.get("requested", "")), candidate["id"]))
    if len(candidates) != 1:
        return None
    identifier, candidate = next(iter(candidates.items()))
    mentions = sum(bool(surface) for surface, value in surfaces if value == identifier)
    return candidate, mentions


def _hla_grammar(question, surface="HLA-DRA"):
    if not isinstance(question, str):
        return None
    surface = re.escape(surface)
    screenshot = (
        r"\s*what\s+pathways?\s*\(\s*(?:kegg\s*/\s*reactome|reactome\s*/\s*kegg)\s*\)\s*"
        r"(?:is|are)\s+" + surface + r"\s+annotated\s+to\s*,?\s+and\s+"
        r"what\s+genes?\s+physically\s+or\s+genetically\s+interact\s+with\s+" + surface +
        r"\s*,?\s*in\s+the\s+context\s+of\s+antigen\s+presentation\s+and\s+"
        r"(?:type\s*1\s+diabetes|t1d)\s*[?]\s*")
    stored = (r"\s*what\s+pathways\s+and\s+interaction\s+partners\s+connect\s+"
              + surface + r"\s+to\s+antigen\s+presentation\s+in\s+"
              r"(?:type\s*1\s+diabetes|t1d)\s*[?]\s*")
    if re.fullmatch(screenshot, question, re.I):
        return "screenshot"
    if re.fullmatch(stored, question, re.I):
        return "stored"
    return None


def is_hla_path_request(question):
    """Recognize only the two reviewed HLA-DRA request grammars."""
    return _hla_grammar(question) is not None


def compile_hla_path_plan(question, grounding, history=None):
    """Compile the reviewed HLA pathway/interaction wording into three checks.

    This recognizes the production wording by its complete grammatical shape;
    it does not use semantic similarity or hard-code an answer.
    """
    if (history or not isinstance(question, str)
            or not grounding or grounding.get("status") != "ready"
            or grounding.get("identity", {}).get("graph_release") != REGISTRY["release"]):
        return None
    resolved = _resolved_gene(grounding)
    if resolved is None:
        return None
    gene, mention_count = resolved
    if gene.get("name") != "HLA-DRA":
        return None
    grammar = _hla_grammar(question, gene["name"])
    if grammar is None or grammar == "screenshot" and mention_count < 2:
        return None
    context_ids = sorted({candidate.get("id")
                          for mention in grounding.get("mentions", [])
                          for candidate in mention.get("candidates") or []
                          if mention.get("state") == "resolved"
                          and candidate.get("entity_type") != "Gene"
                          and isinstance(candidate.get("id"), str)})

    identity = {"property": "id", "operator": "=", "value": gene["id"],
                "entity_type": "Gene", "owner_role": "focus"}
    request = {"source": "user_request", "question": question,
               "revision_instruction": ""}
    direct = {
        "id": "direct_annotations",
        "question": f"Show all recorded KEGG and Reactome pathway annotations for {gene['name']}.",
        "relation_types": ["FUNCTION_ANNOTATION"], "depends_on": [],
        "constraints": [deepcopy(identity)], "complete": True,
        "evidence_combination": "cooccurrence", "semantic_request": deepcopy(request),
        "path_spec": {"version": VERSION,
            "nodes": [{"role": "focus", "entity_types": ["Gene"]},
                      {"role": "process", "entity_types": ["kegg", "reactome"]}],
            "edges": [{"role": "annotation", "from": "focus", "to": "process",
                       "types_any": ["FUNCTION_ANNOTATION"], "direction": "out"}]},
    }
    interactions = {
        "id": "interaction_partners",
        "question": f"Show every gene that physically or genetically interacts with {gene['name']}.",
        "relation_types": ["PHYSICAL_INTERACTION", "GENETIC_INTERACTION"],
        "depends_on": [], "constraints": [deepcopy(identity)], "complete": True,
        "evidence_combination": "cooccurrence", "semantic_request": deepcopy(request),
        "path_spec": {"version": VERSION,
            "nodes": [{"role": "focus", "entity_types": ["Gene"]},
                      {"role": "partner", "entity_types": ["Gene"],
                       "distinct_from": ["focus"]}],
            "edges": [{"role": "interaction", "from": "focus", "to": "partner",
                       "types_any": ["PHYSICAL_INTERACTION", "GENETIC_INTERACTION"],
                       "direction": "either"}]},
    }
    chain = {
        "id": "partner_annotations",
        "question": (f"Show KEGG and Reactome pathway annotations of genes that physically or "
                     f"genetically interact with {gene['name']}."),
        "relation_types": ["PHYSICAL_INTERACTION", "GENETIC_INTERACTION",
                           "FUNCTION_ANNOTATION"],
        "depends_on": [], "constraints": [deepcopy(identity)], "complete": True,
        "evidence_combination": "cooccurrence", "semantic_request": deepcopy(request),
        "path_spec": {"version": VERSION,
            "nodes": [{"role": "focus", "entity_types": ["Gene"]},
                      {"role": "partner", "entity_types": ["Gene"],
                       "distinct_from": ["focus"]},
                      {"role": "process", "entity_types": ["kegg", "reactome"]}],
            "edges": [{"role": "interaction", "from": "focus", "to": "partner",
                       "types_any": ["PHYSICAL_INTERACTION", "GENETIC_INTERACTION"],
                       "direction": "either"},
                      {"role": "annotation", "from": "partner", "to": "process",
                       "types_any": ["FUNCTION_ANNOTATION"], "direction": "out"}]},
    }
    return {"interpreted_question": question,
            "steps": [direct, interactions, chain], "clarification": None,
            "planning_route": {"kind": "verified_bounded_path", "version": VERSION,
                               "digest": DIGEST, "claude_calls": 0,
                               "grammar": grammar,
                               "context_only_entity_ids": context_ids,
                               "rule": "Complete reviewed HLA path wording with grounded identity."}}


DIGEST = hashlib.sha256(Path(__file__).read_bytes() + SCHEMA_DIGEST.encode()).hexdigest()
