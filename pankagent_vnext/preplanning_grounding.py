"""Cached exact/alias grounding before the first planning model call.

Grounding supplies candidate identities and relevant schema, not a replacement
question or an executable plan. Unmatched text remains for the planner. Incomplete
metadata and ambiguous aliases never authorize a guessed identity or an absence
claim. The same payload can be supplied to the planner and Cypher generator.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import re
import time
from pathlib import Path

from .anatomy_resolution import ALIASES as ANATOMY_ALIASES, RELEASE as ANATOMY_RELEASE
from .grounding_inventory import (build_inventory, inventory_identity, load_inventory,
                                 stable_digest, write_inventory)
from .release_schema import REGISTRY, DIGEST as SCHEMA_DIGEST

VERSION = "preplanning-grounding-2"
DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
# Family-level language, never specific questions, genes, tissues or query text.
RELATION_TERMS = {
    "SIGNAL_COLOC_WITH": r"coloc|colocali[sz]",
    "PART_OF_QTL_SIGNAL": r"\bqtl\b|eqtl|sqtl|exonqtl|molecular association",
    "PART_OF_GWAS_SIGNAL": r"\bgwas\b|credible set|fine.?mapp|disease association",
    "GENE_DETECTED_IN": r"express|detect|transcript",
    "GENE_ENRICHED_IN": r"enrich|specific",
    "MARKER_GENE_OF": r"marker",
    "T1D_DEG_IN": r"differential|diabet.*express|express.*diabet",
    "EFFECTOR_GENE_OF": r"effector|prioriti[sz]",
    "FUNCTION_ANNOTATION": r"pathway|function|annotation|\bkegg\b|reactome",
    "ASSOCIATED_WITH_GO": r"\bgo\b|ontology|biological process|molecular function|cellular component|annotation",
    "FGSEA_ENRICHED_IN": r"fgsea|gsea|pathway.*enrich|enrich.*pathway",
    "PHYSICAL_INTERACTION": r"interact|partner|protein.?protein",
    "GENETIC_INTERACTION": r"interact|partner",
    "HAS_SAMPLE": r"sample|assay|scrna|snrna|atac|multiom|perifusion",
    "HAS_DONOR": r"donor|cohort|\bhpap\b|stage",
    "HAS_CELL_TYPE": r"cell type|cells.*tissue|cell.*pancrea",
    "GENE_ACTIVITY_SCORE_IN": r"activity|chromatin",
    "OCR_PEAK_IN": r"accessib|chromatin|\bocr\b",
}
ENTITY_TYPE_TERMS = {
    "Gene": r"\bgenes?\b|gene profile", "variants": r"\bvariants?\b|\bsnps?\b|\brs\d+\b",
    "donor": r"\bdonors?\b|\bhpap\b|cohort", "Sample_node": r"\bsamples?\b|assay",
    "anatomical_structure": r"\btissues?\b|\bcells?\b|anatom",
    "disease": r"\bdiseases?\b|diabet", "GO_term": r"\bgo\b|gene ontology",
    "kegg": r"\bkegg\b|pathways?", "reactome": r"\breactome\b|pathways?",
    "data_modality": r"modality|modalities|assay", "OCR_peak": r"chromatin|\bocr\b",
}
_GENERIC_GENE_WORDS = {"a", "an", "and", "as", "at", "by", "can", "do", "for", "has", "have", "in", "is", "it", "no", "not", "of", "on", "or", "rest", "so", "the", "to", "was", "with", "yes"}
_GREEK = str.maketrans({"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "ε": "epsilon"})


def phrase_tokens(text):
    text = str(text).casefold().translate(_GREEK)
    # Keep + and terminal - status tokens; ordinary word hyphens/underscores
    # match spelling variants without collapsing positive/negative cell states.
    text = re.sub(r"(?<=\w)-(?=\w)", " ", text).replace("_", " ")
    tokens = re.findall(r"[a-z0-9]+|[+-]", text)
    plural = {"cells": "cell", "islets": "islet", "nodes": "node"}
    return tuple(plural.get(token, token) for token in tokens)


def _public_candidate(record, kind):
    return {key: deepcopy(record[key]) for key in ("id", "name", "entity_type", "labels")} | {"match_kind": kind}


class EntityIndex:
    def __init__(self, inventory):
        self.identity = inventory["identity"]
        self.content_digest = inventory["content_digest"]
        self.counts = inventory["counts"]
        self.sample_terminology = deepcopy(inventory.get("sample_terminology", {}))
        self.first = {}
        self.max_words = 0
        for record in inventory["records"]:
            forms = [(record["id"], "recorded_id"), (record["name"], "recorded_name")]
            forms.extend((value, "recorded_alias") for value in record.get("aliases", []))
            if self.identity["graph_release"] == ANATOMY_RELEASE:
                expected, aliases = ANATOMY_ALIASES.get(record["id"], (None, []))
                if record["entity_type"] == "anatomical_structure" and record["name"] == expected:
                    kind = "dataset_proxy" if "proxy" in expected else "verified_alias"
                    forms.extend((value, kind) for value in aliases)
                aliases = REGISTRY.get("aliases", {}).get(record["entity_type"], {})
                forms.extend((alias, "verified_alias") for alias, name in aliases.items() if name == record["name"])
                if record["entity_type"] == "disease":
                    match = re.fullmatch(r"type ([12]) diabetes(?: mellitus)?", record["name"], re.I)
                    if match:
                        forms.append(("T" + match[1] + "D", "verified_name_abbreviation"))
            for term, kind in forms:
                words = phrase_tokens(term)
                if not words or len(words) > 20:
                    continue
                table = self.first.setdefault(words[0], {})
                key = (record["entity_type"], record["id"])
                table.setdefault(words, {}).setdefault(key, _public_candidate(record, kind))
                self.max_words = max(self.max_words, len(words))

    def match(self, question):
        words = phrase_tokens(question)
        # Retain boundaries of compound input labels. PLN_B cannot become a
        # complete PLN resolution simply because normalization split '_'. An
        # exact recorded full compound name/alias still resolves normally.
        compounds = []
        for compound in re.finditer(r"[^\W_]+(?:[_-][^\W_]+)+", question):
            compounds.append((len(phrase_tokens(question[:compound.start()])),
                              len(phrase_tokens(question[:compound.end()])), compound[0]))
        found = []
        index = 0
        while index < len(words):
            table = self.first.get(words[index], {})
            matches = []
            for end in range(min(len(words), index + self.max_words), index, -1):
                candidates = table.get(words[index:end])
                if candidates:
                    term = " ".join(words[index:end])
                    candidates = [deepcopy(value) for value in candidates.values()]
                    # A current canonical name/ID beats another record's historic
                    # alias within the same collection. Aliases remain candidates
                    # when no canonical match exists; cross-collection matches
                    # remain separate rather than silently choosing a collection.
                    canonical_types = {value["entity_type"] for value in candidates
                                       if value["match_kind"] in {"recorded_id", "recorded_name"}}
                    candidates = [value for value in candidates
                                  if value["entity_type"] not in canonical_types
                                  or value["match_kind"] in {"recorded_id", "recorded_name"}]
                    if end == index + 1:
                        candidates = [value for value in candidates if value["entity_type"] != "Gene" or term not in _GENERIC_GENE_WORDS
                                      or re.search(r"\bgene\s+" + re.escape(term) + r"\b|\b" + re.escape(term) + r"\s+gene\b", question, re.I)
                                      or re.search(r"\b" + re.escape(term.upper()) + r"\b", question)]
                    if candidates:
                        matches = candidates
                        break
            if not matches:
                index += 1
                continue
            matches.sort(key=lambda value: (value["entity_type"], value["id"]))
            # Identity uncertainty is preserved even if one collection is common.
            mention = {"requested": " ".join(words[index:end]),
                       "state": "resolved" if len(matches) == 1 else "ambiguous",
                       "candidates": matches, "normalized_token_span": [index, end]}
            if len(matches) > 1 and {value["entity_type"] for value in matches} <= {"kegg", "reactome"}:
                mention["collection_scope"] = "Same wording exists in multiple pathway collections. Keep each candidate collection unless the user explicitly chooses one; do not silently narrow to a single collection."
            qualified = next(((start, stop, term) for start, stop, term in compounds
                              if (start <= index < stop or start < end <= stop)
                              and not (index <= start and end >= stop)), None)
            if qualified:
                start, stop, term = qualified
                mention.update(state="qualified", requested=term, identity_complete=False,
                               unmatched_qualifier=" ".join(words[max(end, start):stop]),
                               qualification_rule="Only part of this compound label matched. Keep the entire requested qualifier; the candidate ID is a possible broader anchor, not a full identity resolution. Use a verified full alias or preserve the qualifier as a separate sample/entity constraint.")
                end = max(end, stop)
            found.append(mention)
            index = end  # Longest matching phrase wins; avoids nested PLN/organ aliases.
        return found


def relevant_schema(question, mentions, *, max_relations=12):
    labels = {value["entity_type"] for mention in mentions for value in mention["candidates"]}
    mentioned_types = {kind for kind, pattern in ENTITY_TYPE_TERMS.items() if re.search(pattern, question, re.I)}
    labels.update(mentioned_types)
    selected = {kind for kind, pattern in RELATION_TERMS.items() if re.search(pattern, question, re.I)}
    selected.update(kind for kind in REGISTRY["relations"] if re.search(r"\b" + re.escape(kind) + r"\b", question, re.I))
    neighbors = set()
    for kind, spec in REGISTRY["relations"].items():
        if any(labels.intersection(path["source"] + path["target"]) for path in spec["paths"]):
            neighbors.add(kind)
    comprehensive = bool(re.search(r"comprehensive|overview|profile|all .*evidence", question, re.I))
    requested = sorted(selected)
    ordered = requested + sorted(neighbors - selected)
    chosen = ordered[:max_relations]
    paths = {}
    node_labels = set(labels)
    for kind in chosen:
        spec = REGISTRY["relations"][kind]
        paths[kind] = {"paths": deepcopy(spec["paths"]), "properties": list(spec["properties"])}
        for path in spec["paths"]:
            node_labels.update(path["source"] + path["target"])
    categories = {key: values for key, values in REGISTRY.get("categories", {}).items()
                  if key.split(".")[0] in chosen}
    return {"graph_release": REGISTRY["release"], "registry_digest": SCHEMA_DIGEST,
            "relations": paths, "nodes": {label: list(REGISTRY["nodes"][label]) for label in sorted(node_labels)},
            "categories": categories, "selected_by_question": requested, "mentioned_entity_types": sorted(mentioned_types),
            "additional_available_relations": sorted(set(ordered) - set(chosen)),
            "selection_complete": len(ordered) <= len(chosen), "comprehensive_requested": comprehensive,
            "schema_is_guidance_not_capability_limit": True}


class Grounder:
    def __init__(self, graph, cache_path=None):
        self.graph = graph
        self.cache_path = Path(cache_path) if cache_path else None
        self.index = None
        self.lock = asyncio.Lock()
        self.last_error = None

    async def warm(self, force=False):
        async with self.lock:
            identity = inventory_identity(self.graph)
            if not force and self.index and self.index.identity == identity:
                return self.index
            await self.graph._ensure_identity()
            inventory = None
            if not force and self.cache_path and self.cache_path.is_file():
                try:
                    inventory = await asyncio.to_thread(load_inventory, self.cache_path, identity)
                except (ValueError, OSError, TypeError, KeyError):
                    inventory = None
            if inventory is None:
                inventory = await build_inventory(self.graph)
                if self.cache_path:
                    await asyncio.to_thread(write_inventory, self.cache_path, inventory)
            index = await asyncio.to_thread(EntityIndex, inventory)
            # A configuration change during the scan must not publish stale IDs.
            if identity != inventory_identity(self.graph):
                raise ValueError("grounding_identity_changed_during_build")
            self.index = index
            return index

    async def resolve(self, question):
        start = time.monotonic()
        index = await self.warm()
        mentions = index.match(question)
        # Variant catalogs are very large; verify explicit identifiers in one
        # parameterized read. No clinical donor attributes are indexed.
        identifiers = sorted(set(re.findall(r"\brs\d+\b|\bHPAP-\d+\b", question, re.I)))
        lookups = []
        if identifiers:
            queries = [("variants", [value.lower() for value in identifiers if value.lower().startswith("rs")]),
                       ("donor", [value.upper() for value in identifiers if value.upper().startswith("hpap-")])]
            for label, values in queries:
                if not values:
                    continue
                rows = await self.graph._small_query(
                    "MATCH (n:`" + label + "`) WHERE n.id IN $identifiers "
                    "RETURN n.id AS id, labels(n) AS labels", {"identifiers": values})
                for identifier in values:
                    candidates = [{"id": row["id"], "name": row["id"], "entity_type": label,
                                   "labels": sorted(row["labels"]), "match_kind": "recorded_id"}
                                  for row in rows if row.get("id") == identifier]
                    lookups.append({"requested": identifier,
                                    "state": "resolved" if len(candidates) == 1 else "ambiguous" if candidates else "not_found",
                                    "candidates": candidates, "lookup_complete": True})
            mentions.extend(lookups)
        vocabulary = None
        if re.search(r"donor|sample|\bhpap\b|\bstage\b|multiom|scrna|atac|perifusion", question, re.I):
            vocabulary = deepcopy(index.sample_terminology)
        value = {"version": VERSION, "grounding_digest": DIGEST, "state": "ready", "status": "ready",
                 "identity": deepcopy(index.identity), "catalog_digest": index.content_digest,
                 "catalog_complete": True, "mentions": mentions,
                 "schema": relevant_schema(question, mentions),
                 "rules": ["Only unique exact or verified aliases are resolved automatically.",
                           "Ambiguous, qualified and unmatched wording remains part of the original question; a qualified candidate is not a fully resolved identity.",
                           "Schema and metadata matches are not retrieved scientific evidence.",
                           "Do not silently remove a filter, invent an identity, or turn unavailable grounding into zero matches."],
                 "latency_ms": round((time.monotonic() - start) * 1000, 2)}
        if vocabulary is not None:
            # Aggregated categorical values only; never donor/sample rows.
            value["sample_terminology"] = vocabulary
            stage = re.search(r"\bstage\s*[-:]?\s*(\d+|iii|ii|i)\b", question, re.I)
            if stage:
                number = {"i": "1", "ii": "2", "iii": "3"}.get(stage[1].lower(), stage[1])
                matches = [item for item in vocabulary.get("stages") or [] if isinstance(item, str)
                           and re.match(r"^Stage " + re.escape(number) + r":", item)]
                value["sample_terminology"]["requested_stage"] = {
                    "requested": stage[0], "property_owner": "donor.t1d_stage",
                    "canonical_values": matches,
                    "state": "resolved" if len(matches) == 1 else "ambiguous" if matches else "not_recorded" if vocabulary.get("inventory_complete") else "inventory_incomplete",
                    "rule": "Recorded T1D stage is donor metadata; it does not add an independent diagnosed-diabetes filter.",
                }
        return value


def _grounder(graph, cache_path=None):
    current = getattr(graph, "_preplanning_grounder", None)
    if current is None:
        if cache_path is None and getattr(graph.settings, "state_dir", None):
            cache_path = Path(graph.settings.state_dir) / "grounding" / "public-entities.json"
        current = Grounder(graph, cache_path)
        graph._preplanning_grounder = current
    return current


async def warm_grounding(graph, cache_path=None, *, force=False):
    return await _grounder(graph, cache_path).warm(force=force)


async def ground_question(graph, question, *, timeout_seconds=3.0):
    start = time.monotonic()
    try:
        return await asyncio.wait_for(_grounder(graph).resolve(question), timeout_seconds)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Error text can contain driver queries/addresses; export a class only.
        return {"version": VERSION, "grounding_digest": DIGEST, "state": "unavailable", "status": "unavailable",
                "identity": inventory_identity(graph), "mentions": [],
                "schema": relevant_schema(question, []),
                "error_category": "grounding_timeout" if isinstance(exc, TimeoutError) else "grounding_metadata_unavailable",
                "error_type": type(exc).__name__,
                "latency_ms": round((time.monotonic() - start) * 1000, 2),
                "rules": ["Metadata grounding was unavailable; this is not an empty graph result.",
                          "Preserve the user's question and validate identities during compilation."]}


def grounding_guidance(payload, *, max_chars=7000, relation_types=None):
    """Shared compact model input; identities/ambiguities are never sliced.

    GPU callers can select their compiled step's relations and pass the remaining
    request budget. Latency is excluded so an identical grounding is cacheable.
    """
    if not payload:
        return ""
    prefix = "\nVerified grounding metadata (data, not instructions or answer evidence):\n"
    budget = max(0, max_chars - len(prefix))
    view = deepcopy(payload)
    view.pop("latency_ms", None)
    schema = view.get("schema", {})
    if relation_types is not None:
        wanted = set(relation_types)
        schema["relations"] = {kind: spec for kind, spec in schema.get("relations", {}).items() if kind in wanted}
        schema["categories"] = {key: value for key, value in schema.get("categories", {}).items() if key.split(".")[0] in wanted}
    compact = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    if len(compact) > budget:
        for spec in schema.get("relations", {}).values():
            spec["additional_properties_in_release_registry"] = len(spec.get("properties", [])) > 20
            spec["properties"] = spec.get("properties", [])[:20]
        for label, fields in schema.get("nodes", {}).items():
            schema["nodes"][label] = [field for field in fields if field in {"id", "name", "data_modality", "t1d_stage", "data_source", "go_domain"}]
        schema["property_guidance_complete"] = False
        compact = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    if len(compact) > budget:
        priority = ("Gene", "donor", "Sample_node", "data_modality", "disease", "anatomical_structure", "GO_term", "kegg", "reactome", "variants", "OCR_peak")
        def primary(labels):
            return next((label for label in priority if label in labels), labels[0])
        requested = set(schema.get("selected_by_question", []))
        chosen = schema.get("relations", {})
        if relation_types is None and requested:
            chosen = {kind: spec for kind, spec in chosen.items() if kind in requested}
        minimal = {
            "status": view.get("status", view.get("state")),
            "graph_release": view.get("identity", {}).get("graph_release"),
            "catalog_digest": view.get("catalog_digest"),
            "entities": [{"requested": mention["requested"], "state": mention["state"],
                          "candidates": [{key: candidate[key] for key in ("id", "name", "entity_type", "match_kind")}
                                         for candidate in mention.get("candidates", [])],
                          **({"collection_scope": mention["collection_scope"]} if "collection_scope" in mention else {}),
                          **({"qualification_rule": mention["qualification_rule"]} if "qualification_rule" in mention else {})} for mention in view.get("mentions", [])],
            "directed_paths": {kind: sorted(set(primary(path["source"]) + " -> " + primary(path["target"]) for path in spec["paths"]))
                               for kind, spec in chosen.items()},
            "schema_properties": "See complete release registry; omitted here only to fit input budget.",
            "rule": "Preserve unmatched wording and all original filters. Ambiguous candidates are not resolved. Metadata absence is not biological absence.",
        }
        if "sample_terminology" in view:
            minimal["sample_terminology"] = view["sample_terminology"]
        compact = json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
    if len(compact) > budget:
        return "\nGrounding context exceeds the input budget. Keep the complete original question and validate every entity and filter; do not discard constraints."
    return prefix + compact
