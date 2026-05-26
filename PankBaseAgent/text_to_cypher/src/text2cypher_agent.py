#!/usr/bin/env python3
import json
import re
import sys

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from .text2cypher_utils import get_env_variable
from .schema_loader import (
    get_schema,
    get_schema_hints,
    get_simplified_schema,
    get_minimal_schema_for_llm,
    get_detailed_schema_for_cypher,
)
from .cypher_validator import validate_cypher, format_validation_report, auto_fix_cypher


load_dotenv()


# Cypher clauses a valid generated query may start with.
_CYPHER_START_RE = re.compile(
    r'\b(MATCH|OPTIONAL\s+MATCH|WITH|UNWIND|CALL|MERGE|CREATE|RETURN)\b',
    re.IGNORECASE,
)


def _extract_cypher(raw: str) -> str:
    """Extract a bare Cypher query from a raw LLM response.

    The fine-tuned cypher-writer normally returns bare Cypher, but it
    occasionally drops into "explain mode" and wraps the query in prose plus a
    Markdown ```cypher ... ``` fence. This helper recovers the actual query in
    that case so verbose responses never get sent raw to Neo4j.

    Strategy (first match wins):
      1. If a fenced code block exists (```cypher ... ``` or ``` ... ```),
         take the LAST one's contents (the model often shows the final query
         last).
      2. Otherwise, if the text contains Cypher prose, slice from the first
         Cypher-start keyword (MATCH / OPTIONAL MATCH / WITH / ...) to the end.
      3. Otherwise return the stripped text unchanged.

    After slicing, trailing prose after the final ``;`` is dropped.
    """
    if not raw:
        return raw
    text = raw.strip()

    # 1. Fenced code block(s)
    fences = re.findall(r"```(?:cypher|sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fences:
        candidate = fences[-1].strip()
    else:
        # 2. Slice from the first Cypher keyword if there's leading prose
        m = _CYPHER_START_RE.search(text)
        if m and m.start() > 0:
            candidate = text[m.start():].strip()
        else:
            candidate = text

    # 3. Drop trailing prose after the final statement terminator
    if ";" in candidate:
        candidate = candidate[: candidate.rfind(";") + 1]

    return candidate.strip()


SYSTEM_RULES = """You are a Cypher query generator for PanKgraph ADA (biomedical KG).

TASK: Generate ONE simple Cypher query per step.
- Look at conversation history for previous results
- Use previous results to guide your query
- Generate focused, specific queries

INTENT CHECK (CRITICAL):
- Before generating a query, verify that any supposed gene name is an ACTUAL gene symbol (e.g., CFTR, INS, MAFA, TP53), not a common English word used as a concept.
- Common words like "diversity", "impact", "expression", "regulation" are NOT gene names.
- If the input asks about a concept (e.g., "gene diversity across cell types"), generate a query that captures that concept (e.g., T1D_DEG_in or gene_detected_in across anatomical_structure nodes).

SYNTAX:
- Relationships need variables: [r:type] not [:type]
- Always filter with properties or WHERE - never return all nodes
- Return: WITH collect(DISTINCT x)+collect(DISTINCT y) AS nodes, collect(DISTINCT r) AS edges RETURN nodes, edges;
- Disease name: ALWAYS use 'type 1 diabetes' (lowercase, never T1D)
- Ontology/pathway annotations use the UNIFIED `function_annotation` edge (no semicolon, no backtick escape). Distinguish GO vs KEGG vs Reactome by the target node label: `(go:gene_ontology)`, `(k:kegg)`, or `(rp:reactome)`. The legacy `function_annotation;GO`, `pathway_annotation;KEGG`, `pathway_annotation;reactome` edges NO LONGER EXIST.
- DO NOT add LIMIT anywhere in your query. Result limits are applied automatically by the system.

GENE ACTIVITY SCORE RULE (CRITICAL — noise filter):
- `gene_activity_score_in` is scATAC-seq-derived chromatin accessibility per cell type. The raw scores contain many false positives: genes can show "activity" in cell types where they are NOT actually expressed (ambient signal, pseudobulk artifacts).
- Whenever you generate a query that matches `gene_activity_score_in`, you MUST also require a `gene_detected_in` edge between the SAME gene and the SAME anatomical_structure. Use an `EXISTS {{ ... }}` sub-clause for this — do not add a separate MATCH (it would inflate the result set).
- Pattern to follow:
    MATCH (g:gene)-[r:gene_activity_score_in]->(ct:anatomical_structure)
    WHERE <other filters> AND EXISTS {{ (g)-[:gene_detected_in]->(ct) }}
- This filter is non-negotiable. Never report a gene_activity_score_in result for a gene that lacks a matching gene_detected_in edge in the same cell type.

NODE LABELS (use these exact labels):
- gene (NOT Gene), snv (NOT snp), OCR_peak (NOT OCR), anatomical_structure (NOT cell_type)
- disease, gene_ontology, kegg, reactome, donor, data_modality

RETURN FORMAT (CRITICAL):
- ALWAYS end queries with: WITH collect(DISTINCT ...)+ ... AS nodes, collect(DISTINCT r) AS edges RETURN nodes, edges;
- DO NOT use collect(map projections like name:val) — they silently return nothing.
- DO NOT use RETURN g.name, r.prop, ... — only RETURN nodes, edges works.
- All node/edge properties are automatically included in the returned objects.

GOOD EXAMPLES:
Query: 'Find gene with name INS'
MATCH (g:gene) WHERE g.name = 'INS'
WITH collect(DISTINCT g) AS nodes, [] AS edges
RETURN nodes, edges;

Query: 'Get SNPs with QTL relationships to gene MAFA'
MATCH (sn:snv)-[r:part_of_QTL_signal]->(g:gene) WHERE g.name = 'MAFA'
WITH collect(DISTINCT sn)+collect(DISTINCT g) AS nodes, collect(DISTINCT r) AS edges
RETURN nodes, edges;

Query: 'Get upregulated genes in Beta Cell in T1D'
MATCH (g:gene)-[deg:T1D_DEG_in]->(ct:anatomical_structure) WHERE ct.name = 'beta cell' AND deg.UpOrDownRegulation = 'Upregulated in T1D'
WITH collect(DISTINCT g)+collect(DISTINCT ct) AS nodes, collect(DISTINCT deg) AS edges
RETURN nodes, edges;

Query: 'Get GO annotations for gene CFTR'
MATCH (g:gene)-[r:function_annotation]->(go:gene_ontology) WHERE g.name = 'CFTR'
WITH collect(DISTINCT g)+collect(DISTINCT go) AS nodes, collect(DISTINCT r) AS edges
RETURN nodes, edges;

Query: 'Get KEGG pathways for gene INS'
MATCH (g:gene)-[r:function_annotation]->(k:kegg) WHERE g.name = 'INS'
WITH collect(DISTINCT g)+collect(DISTINCT k) AS nodes, collect(DISTINCT r) AS edges
RETURN nodes, edges;

Query: 'Get Reactome pathways for gene CFTR'
MATCH (g:gene)-[r:function_annotation]->(rp:reactome) WHERE g.name = 'CFTR'
WITH collect(DISTINCT g)+collect(DISTINCT rp) AS nodes, collect(DISTINCT r) AS edges
RETURN nodes, edges;

Query: 'Get genes detected in Beta Cell'
MATCH (g:gene)-[r:gene_detected_in]->(ct:anatomical_structure) WHERE r.cell_type = 'Beta'
WITH collect(DISTINCT g)+collect(DISTINCT ct) AS nodes, collect(DISTINCT r) AS edges
RETURN nodes, edges;

Query: 'Get gene activity scores in Beta Cell'  (note the mandatory EXISTS detected_in filter)
MATCH (g:gene)-[r:gene_activity_score_in]->(ct:anatomical_structure)
WHERE ct.name = 'beta cell' AND EXISTS {{ (g)-[:gene_detected_in]->(ct) }}
WITH collect(DISTINCT g)+collect(DISTINCT ct) AS nodes, collect(DISTINCT r) AS edges
RETURN nodes, edges;

Query: 'Gene activity score for CFTR in Alpha Cell'
MATCH (g:gene)-[r:gene_activity_score_in]->(ct:anatomical_structure)
WHERE g.name = 'CFTR' AND ct.name = 'alpha cell' AND EXISTS {{ (g)-[:gene_detected_in]->(ct) }}
WITH collect(DISTINCT g)+collect(DISTINCT ct) AS nodes, collect(DISTINCT r) AS edges
RETURN nodes, edges;

Query: 'Find donors with diabetes_type Diabetes (Type I)'
MATCH (d:donor) WHERE d.diabetes_type = 'Diabetes (Type I)'
WITH collect(DISTINCT d) AS nodes, [] AS edges
RETURN nodes, edges;

STRING-TYPED PROPERTIES THAT LOOK NUMERIC OR BOOLEAN (CRITICAL):

`chr` (chromosome) on gene / OCR_peak / variant nodes:
- Stored as a String: '1'..'22', 'X', 'Y', 'MT'. Comparison with an unquoted int silently returns 0 rows.
- Correct: `WHERE g.chr = '1'`  (quote the literal)
- For range/numeric queries: `WHERE toInteger(g.chr) < 23`  (safe only on autosomes; X/Y/MT will fail the cast — usually fine)

`strand` on gene nodes:
- Stored as String: '-1' or '1'.  Correct: `WHERE g.strand = '1'`.

`credibleset` on part_of_QTL_signal edges:
- Stored as String like '1', '2'.  Correct: `WHERE r.credibleset = '1'`.

Boolean variant flags on `(:variants)`/`(:sequence_variant)` and subtype nodes
(`in_3prime_UTR`, `in_3prime_gene_region`, `in_5prime_UTR`, `in_5prime_gene_region`,
`in_intron`, `in_acceptor_splice_site`, `in_donor_splice_site`,
`non_synonymous_missense`, `non_synonymous_nonsense`, `non_synonymous_frameshift`):
- Stored as MIXED-CASE strings: 'TRUE', 'True', 'true', 'FALSE', 'False', 'false' all co-exist in the same property.
- Do NOT write `WHERE v.in_intron = true` (a Cypher boolean — returns 0 rows).
- Do NOT write `WHERE v.in_intron = 'TRUE'` (only catches one casing).
- Correct: `WHERE toLower(v.in_intron) = 'true'`  (catches every casing).

Date strings like '4/7/25' (variant data_version, donor creation_date) are in
D/M/YY format, ambiguous. Treat as opaque strings — equality only.

DONOR NUMERIC PROPERTIES STORED AS STRINGS (CRITICAL):
- The following donor properties LOOK numeric but are actually String:
  - `age` — format `"18 years"`, `"58 years"` (extract integer with `split(d.age, ' ')[0]`)
  - `diabetes_duration` — format `"5 years"`, `"3 years"` (same pattern)
  - `bmi` — format `"21.3"`, `"27.24"` (use `toFloat(d.bmi)`)
  - `hba1c_percentage` — format `"12.4"`, `"5.8"` (use `toFloat(...)`)
  - `c_peptide_ng_ml` — format `"0.09"`, `"3.75"` (use `toFloat(...)`)
- NEVER write `WHERE d.age < 40` — string-vs-number comparison returns 0 rows.
  Correct: `WHERE toInteger(split(d.age, ' ')[0]) < 40`
- Same goes for `>`, `>=`, `<=`, `=`, `<>` on any of these properties.
- `hospital_stay_hours` IS an Int and can be compared directly.

Query: 'Find female donors with age less than 40'
MATCH (d:donor) WHERE d.gender = 'Female' AND toInteger(split(d.age, ' ')[0]) < 40
WITH collect(DISTINCT d) AS nodes, [] AS edges
RETURN nodes, edges;

Query: 'Find T1D donors with BMI over 25'
MATCH (d:donor) WHERE d.diabetes_type = 'Diabetes (Type I)' AND toFloat(d.bmi) > 25
WITH collect(DISTINCT d) AS nodes, [] AS edges
RETURN nodes, edges;

Query: 'Find donors with aab_state containing GADA positive'
MATCH (d:donor) WHERE d.aab_state CONTAINS 'GADA positive'
WITH collect(DISTINCT d) AS nodes, [] AS edges
RETURN nodes, edges;

Query: 'Find donors with hla_status DR3/DR4'
MATCH (d:donor) WHERE d.hla_status = 'DR3/DR4'
WITH collect(DISTINCT d) AS nodes, [] AS edges
RETURN nodes, edges;

Query: 'Is CFTR an effector gene for T1D?'
MATCH (g:gene)-[r:effector_gene_of]->(d:disease)
WHERE g.name = 'CFTR'
WITH collect(DISTINCT g)+collect(DISTINCT d) AS nodes, collect(DISTINCT r) AS edges
RETURN nodes, edges;

BAD EXAMPLES (DO NOT DO):
WRONG: MATCH (sn:snp)-[r:part_of_QTL_signal]->(g:gene) (use snv not snp!)
WRONG: MATCH (g:gene)-[:function_annotation]->(fo:gene_ontology) (missing relationship variable — must be `[r:function_annotation]`)
WRONG: MATCH (g:gene)-[r:`function_annotation;GO`]->(...) (legacy edge name — use the unified `function_annotation` without backticks)
WRONG: MATCH (g:gene)-[r:`pathway_annotation;KEGG`]->(...) (legacy edge name — use `function_annotation` with target `(k:kegg)`)
WRONG: MATCH (g:gene)-[r:`pathway_annotation;reactome`]->(...) (legacy edge name — use `function_annotation` with target `(rp:reactome)`)
WRONG: MATCH (g:gene)-[r:DEG_in]->(ct:cell_type) (use T1D_DEG_in and anatomical_structure!)
WRONG: ... RETURN nodes, edges LIMIT 50; (DO NOT add LIMIT)
WRONG: RETURN g.name AS name, r.Log2FoldChange AS lfc (scalar returns not supported!)
WRONG: MATCH (g:gene)-[r:gene_activity_score_in]->(ct:anatomical_structure) WHERE ct.name = 'Beta Cell' (MISSING the mandatory EXISTS gene_detected_in filter — would return noise scores for genes not actually expressed in Beta cells)

Schema:
"""


def make_llm(provider: str = "local"):
    """Return a Chat instance for the specified provider."""
    provider = "local"
    if provider == "openai":
        return ChatOpenAI(
            base_url=get_env_variable("OPENAI_API_BASE_URL"),  # e.g. "https://api.openai.com/v1"
            api_key=get_env_variable("OPENAI_API_KEY"),
            model=get_env_variable("OPENAI_API_MODEL"),
            temperature=float(get_env_variable("OPENAI_API_TEMP", 0))
        )

    elif provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=get_env_variable("GOOGLE_MODEL"),
            google_api_key=get_env_variable("GOOGLE_API_KEY"),
            temperature=0
        )

    elif provider == "local":
        # self-hosted vLLM instance – port is configurable via VLLM_PORT env var
        import os
        vllm_port = os.environ.get("VLLM_PORT", "8002")
        return ChatOpenAI(
            base_url=f"http://localhost:{vllm_port}/v1",
            api_key="EMPTY",  # vLLM ignores this field but LangChain requires it
            model="cypher-writer",
            temperature=0
        )

    else:
        raise ValueError(f"Unknown provider: {provider}")
    

class Text2CypherAgent:
    """Single‑LLM agent that remembers conversation context + schema."""

    def __init__(self, provider: str = "local",
                 enable_refinement: bool = True,
                 max_refinement_iterations: int = 5,
                 min_acceptable_score: int = 90):
        self.provider = provider
        self.enable_refinement = enable_refinement
        self.max_refinement_iterations = max_refinement_iterations
        self.min_acceptable_score = min_acceptable_score
        
        # Use ultra-minimal schema optimized for small models (9B with 8k context)
        self.minimal_schema = get_minimal_schema_for_llm()
        
        system_prompt = SYSTEM_RULES + self.minimal_schema

        self.llm = make_llm(provider)
        
        # Use the exact format the model was trained on
        self.prompt = ChatPromptTemplate.from_messages([
            ("human", system_prompt + "\nQuestion: {user_input}\n\n")
        ])

        self.chain = self.prompt | self.llm

    # def respond(self, user_text: str) -> str:
    #     result = self.chain.invoke(
    #         {"user_input": user_text},
    #         config={"configurable": {"session_id": self.session_id}}
    #     )
    #     return result.content.strip().strip("` ")
    def respond(self, user_text: str) -> str:
        result = self.chain.invoke({"user_input": user_text})
        cypher = result.content.strip().strip("` ")

        # Remove common prefixes that models add
        if cypher.lower().startswith("cypher"):
            cypher = cypher[6:].strip()

        # Remove "Query: '...'" prefix if the model echoed the input
        query_prefix_match = re.match(r'^Query:\s*[\'"].*?[\'"]\s*\n?', cypher, re.IGNORECASE)
        if query_prefix_match:
            cypher = cypher[query_prefix_match.end():].strip()

        # Recover bare Cypher if the model wrapped it in prose / a ```cypher fence
        cypher = _extract_cypher(cypher)

        # Strip any LIMIT clause the model may have added — limits are
        # injected by the system (cypher_validator / _ensure_limit_before_collect)
        cypher = re.sub(r'\s+LIMIT\s+\d+', '', cypher, flags=re.IGNORECASE)

        return cypher

    def respond_with_refinement(self, user_text: str, max_iterations: int = None) -> dict:
        """
        Generate Cypher using a 2-stage lazy-schema flow.

        Stage 1 — initial generation against the rich minimal schema (existing
        ``self.respond``). After the draft comes back, lazily pull the
        detailed schema slice for ONLY the entities the LLM picked
        (``get_detailed_schema_for_cypher``), then run deterministic schema-
        slice-driven fixes via ``auto_fix_cypher`` (enum value coercion,
        cell-type canonicalisation, property-name case, etc.).

        Stage 2 — if the auto-fixed draft still scores below the threshold,
        do a SINGLE refinement LLM call. The refinement prompt now carries
        the entity-specific detailed slice (full property names + valid enum
        values), not the generic minimal schema. Result is auto-fixed again
        before validation.

        Replaces the previous up-to-5-iteration loop. Caps LLM calls at 2.

        ``max_iterations`` is accepted for backwards compatibility but no
        longer controls the loop count — it's logged and ignored.

        Returns the same dict shape as before for caller compatibility:
            {
                'cypher': str,              # Best Cypher query
                'score': int,               # Validation score (0-100)
                'iteration': int,           # Which attempt produced the best result
                'all_attempts': list,       # All attempts with scores and validation
                'validation_report': dict   # Detailed validation of best query
            }
        """
        all_attempts = []

        # --- Stage 1: initial draft + lazy detailed slice + auto-fix ---
        draft = self.respond(user_text)
        detailed_text, enum_map = get_detailed_schema_for_cypher(draft)
        fixed1, fixes1 = auto_fix_cypher(draft, schema_slice=enum_map)
        validation1 = validate_cypher(fixed1)

        attempt1 = {
            'iteration': 1,
            'cypher': fixed1,
            'score': validation1['score'],
            'validation': validation1,
            'raw_draft': draft,
            'fixes_applied': fixes1,
        }
        all_attempts.append(attempt1)

        if validation1['score'] >= 90:
            return {
                'cypher': fixed1,
                'score': validation1['score'],
                'iteration': 1,
                'all_attempts': all_attempts,
                'validation_report': validation1,
            }

        # --- Stage 2: single refinement LLM call with the detailed slice ---
        refinement_prompt = self._build_refinement_prompt_v2(
            user_text, fixed1, validation1, detailed_text,
        )
        refined = self._generate_with_refinement_prompt_v2(refinement_prompt)
        fixed2, fixes2 = auto_fix_cypher(refined, schema_slice=enum_map)
        validation2 = validate_cypher(fixed2)

        attempt2 = {
            'iteration': 2,
            'cypher': fixed2,
            'score': validation2['score'],
            'validation': validation2,
            'raw_draft': refined,
            'fixes_applied': fixes2,
        }
        all_attempts.append(attempt2)

        # Return whichever attempt scored better (tie -> later attempt)
        best = attempt2 if validation2['score'] >= validation1['score'] else attempt1
        return {
            'cypher': best['cypher'],
            'score': best['score'],
            'iteration': best['iteration'],
            'all_attempts': all_attempts,
            'validation_report': best['validation'],
        }
    
    def _build_refinement_prompt(self, original_question: str, previous_cypher: str, 
                                  validation: dict) -> str:
        """Build a focused refinement prompt with only essential error feedback."""
        
        # Build concise error list
        errors_text = "\n".join(f"  - {error}" for error in validation['errors'])
        if not errors_text:
            errors_text = "  - Low score but no specific errors detected"
        
        # Build compact prompt focusing on what's wrong
        prompt = (
            f"Previous attempt (Score: {validation['score']}/100):\n"
            f"{previous_cypher}\n\n"
            f"Fix these errors:\n{errors_text}\n\n"
            f"Question: {original_question}\n\n"
            f"Generate corrected Cypher query."
        )
        
        with open('log.txt', 'a') as log_file:
            log_file.write(f"Refinement prompt:\n{prompt}\n")
        return prompt
    
    def _generate_with_refinement_prompt(self, refinement_prompt: str) -> str:
        """[Deprecated] Generate Cypher using legacy refinement prompt with minimal schema only.

        Retained for backward compatibility; the active path is
        ``_generate_with_refinement_prompt_v2`` which carries the entity-specific
        detailed schema slice instead.
        """
        # Build a simplified prompt for refinement
        schema_section = f"Schema:\n{self.minimal_schema}\n\n"
        full_prompt = schema_section + refinement_prompt + "\n\n"

        result = self.llm.invoke(full_prompt)
        cypher = result.content.strip().strip("` ")

        # Remove common prefixes that models add
        if cypher.lower().startswith("cypher"):
            cypher = cypher[6:].strip()

        # Remove "Query: '...'" prefix if the model echoed the input
        query_prefix_match = re.match(r'^Query:\s*[\'"].*?[\'"]\s*\n?', cypher, re.IGNORECASE)
        if query_prefix_match:
            cypher = cypher[query_prefix_match.end():].strip()

        # Recover bare Cypher if the model wrapped it in prose / a ```cypher fence
        cypher = _extract_cypher(cypher)

        return cypher

    # ------------------------------------------------------------------
    # 2-stage refinement (lazy detailed-schema slice)
    # ------------------------------------------------------------------

    def _build_refinement_prompt_v2(
        self,
        original_question: str,
        previous_cypher: str,
        validation: dict,
        detailed_schema_text: str,
    ) -> str:
        """Refinement prompt carrying the entity-specific detailed slice.

        The LLM sees: the previous (auto-fixed) Cypher, the remaining
        validation errors, the original question, AND a focused schema slice
        listing every property + valid enum value for ONLY the entities the
        previous attempt referenced. This is the key payload the legacy
        ``_build_refinement_prompt`` did not carry.
        """
        errors_text = "\n".join(f"  - {error}" for error in validation.get('errors', []))
        if not errors_text:
            errors_text = "  - Low score but no specific errors detected"

        prompt = (
            f"Previous attempt (Score: {validation.get('score', 0)}/100):\n"
            f"{previous_cypher}\n\n"
            f"Fix these errors:\n{errors_text}\n\n"
            f"Detailed schema for the entities in your previous attempt:\n"
            f"{detailed_schema_text}\n\n"
            f"Question: {original_question}\n\n"
            f"Generate corrected Cypher query."
        )

        with open('log.txt', 'a') as log_file:
            log_file.write(f"Refinement prompt v2:\n{prompt}\n")
        return prompt

    def _generate_with_refinement_prompt_v2(self, refinement_prompt: str) -> str:
        """Generate Cypher using the v2 refinement prompt.

        Unlike the legacy ``_generate_with_refinement_prompt`` which re-emits
        ``self.minimal_schema``, v2 expects the refinement prompt itself to
        already carry the entity-specific detailed slice (built by
        ``_build_refinement_prompt_v2``). We only prepend SYSTEM_RULES so the
        LLM still knows the syntax/format constraints.
        """
        full_prompt = SYSTEM_RULES + "\n" + refinement_prompt + "\n\n"

        result = self.llm.invoke(full_prompt)
        cypher = result.content.strip().strip("` ")

        if cypher.lower().startswith("cypher"):
            cypher = cypher[6:].strip()
        query_prefix_match = re.match(r'^Query:\s*[\'"].*?[\'"]\s*\n?', cypher, re.IGNORECASE)
        if query_prefix_match:
            cypher = cypher[query_prefix_match.end():].strip()
        # Recover bare Cypher if the model wrapped it in prose / a ```cypher fence
        cypher = _extract_cypher(cypher)
        # Strip any LIMIT the model may have added — injected by the validator
        cypher = re.sub(r'\s+LIMIT\s+\d+', '', cypher, flags=re.IGNORECASE)

        return cypher

    def get_history(self) -> list[dict[str, str]]:
        """Return chat history as list of {role, content} dicts."""
        return []

    def clear_history(self) -> None:
        """Clear the shared history."""
        return None

if __name__ == "__main__":
    try:
        agent = Text2CypherAgent()
    except EnvironmentError as e:
        sys.exit(1)

    try:
        while True:
            txt = input("You> ").strip()
            if not txt:
                continue
            print(agent.respond(txt) + "\n")
    except (KeyboardInterrupt, EOFError):
        pass