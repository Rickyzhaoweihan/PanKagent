#!/usr/bin/env python3
import json
import re
import sys

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from .text2cypher_utils import get_env_variable
from .schema_loader import get_schema, get_schema_hints, get_simplified_schema, get_minimal_schema_for_llm
from .cypher_validator import validate_cypher, format_validation_report


load_dotenv()

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
- Relationship names containing semicolons MUST be backtick-escaped: [`function_annotation;GO`], [`pathway_annotation;KEGG`], [`pathway_annotation;reactome`]
- DO NOT add LIMIT anywhere in your query. Result limits are applied automatically by the system.

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
MATCH (g:gene)-[deg:T1D_DEG_in]->(ct:anatomical_structure) WHERE ct.name = 'type B pancreatic cell (beta cell)' AND deg.UpOrDownRegulation = 'Upregulated in T1D'
WITH collect(DISTINCT g)+collect(DISTINCT ct) AS nodes, collect(DISTINCT deg) AS edges
RETURN nodes, edges;

Query: 'Get GO annotations for gene CFTR'
MATCH (g:gene)-[r:`function_annotation;GO`]->(go:gene_ontology) WHERE g.name = 'CFTR'
WITH collect(DISTINCT g)+collect(DISTINCT go) AS nodes, collect(DISTINCT r) AS edges
RETURN nodes, edges;

Query: 'Get KEGG pathways for gene INS'
MATCH (g:gene)-[r:`pathway_annotation;KEGG`]->(k:kegg) WHERE g.name = 'INS'
WITH collect(DISTINCT g)+collect(DISTINCT k) AS nodes, collect(DISTINCT r) AS edges
RETURN nodes, edges;

Query: 'Get genes detected in Beta Cell'
MATCH (g:gene)-[r:gene_detected_in]->(ct:anatomical_structure) WHERE r.cell_type = 'Beta'
WITH collect(DISTINCT g)+collect(DISTINCT ct) AS nodes, collect(DISTINCT r) AS edges
RETURN nodes, edges;

Query: 'Find donors with diabetes_type Diabetes (Type I)'
MATCH (d:donor) WHERE d.diabetes_type = 'Diabetes (Type I)'
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
WRONG: MATCH (g:gene)-[:function_annotation]->(fo:gene_ontology) (missing variable, wrong rel name — use [`function_annotation;GO`])
WRONG: MATCH (g:gene)-[r:DEG_in]->(ct:cell_type) (use T1D_DEG_in and anatomical_structure!)
WRONG: ... RETURN nodes, edges LIMIT 50; (DO NOT add LIMIT)
WRONG: RETURN g.name AS name, r.Log2FoldChange AS lfc (scalar returns not supported!)

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
        
        # Strip any LIMIT clause the model may have added — limits are
        # injected by the system (cypher_validator / _ensure_limit_before_collect)
        cypher = re.sub(r'\s+LIMIT\s+\d+', '', cypher, flags=re.IGNORECASE)
        
        return cypher

    def respond_with_refinement(self, user_text: str, max_iterations: int = None) -> dict:
        """
        Generate Cypher with iterative refinement using test-time scaling.
        
        Args:
            user_text: The user's natural language query
            max_iterations: Maximum refinement iterations (uses self.max_refinement_iterations if None)
            
        Returns:
            {
                'cypher': str,              # Best Cypher query
                'score': int,               # Validation score (0-100)
                'iteration': int,           # Which iteration produced the best result
                'all_attempts': list,       # All attempts with scores and validation
                'validation_report': dict   # Detailed validation of best query
            }
        """
        if max_iterations is None:
            max_iterations = self.max_refinement_iterations
        
        all_attempts = []
        best_result = None
        best_score = -1
        
        # Iteration 1: Initial generation
        cypher = self.respond(user_text)
        validation = validate_cypher(cypher)
        
        attempt = {
            'iteration': 1,
            'cypher': cypher,
            'score': validation['score'],
            'validation': validation
        }
        all_attempts.append(attempt)
        
        if validation['score'] > best_score:
            best_score = validation['score']
            best_result = attempt
        
        # Early stopping if score is excellent
        if validation['score'] >= 90:
            return {
                'cypher': cypher,
                'score': validation['score'],
                'iteration': 1,
                'all_attempts': all_attempts,
                'validation_report': validation
            }
        
        # Iterations 2-N: Refinement loop
        for iteration in range(2, max_iterations + 1):
            # Build refinement prompt with previous attempt and errors
            prev_validation = all_attempts[-1]['validation']
            
            if prev_validation['score'] >= self.min_acceptable_score and not prev_validation['errors']:
                # Good enough, stop early
                break
            
            refinement_prompt = self._build_refinement_prompt(
                user_text,
                all_attempts[-1]['cypher'],
                prev_validation
            )
            
            # Generate refined Cypher
            cypher = self._generate_with_refinement_prompt(refinement_prompt)
            validation = validate_cypher(cypher)
            
            attempt = {
                'iteration': iteration,
                'cypher': cypher,
                'score': validation['score'],
                'validation': validation
            }
            all_attempts.append(attempt)
            
            # Track best result
            if validation['score'] > best_score:
                best_score = validation['score']
                best_result = attempt
            
            # Early stopping if score is excellent
            if validation['score'] >= 90:
                break
        
        return {
            'cypher': best_result['cypher'],
            'score': best_result['score'],
            'iteration': best_result['iteration'],
            'all_attempts': all_attempts,
            'validation_report': best_result['validation']
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
        """Generate Cypher using refinement prompt."""
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