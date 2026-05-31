
import json
import os
import traceback
import threading
from typing import Tuple, List
from _thread import start_new_thread
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor, as_completed
from multi_thread_workers import LEAF_THREADS
import time
import sys
import re

from performance_monitor import instrument_module_functions
from stream_events import emit

# ---------------------------------------------------------------------------
# Thread-local storage for cypher queries / Neo4j results.
# Each planner candidate thread gets its own isolated lists so parallel
# test-time-scaling candidates don't corrupt each other's data.
# ---------------------------------------------------------------------------

_tls = threading.local()


def _ensure_tls():
    if not hasattr(_tls, "cypher_queries"):
        _tls.cypher_queries = []
    if not hasattr(_tls, "planning_data"):
        _tls.planning_data = []
    if not hasattr(_tls, "neo4j_results"):
        _tls.neo4j_results = []


def reset_cypher_queries():
    """Reset the cypher queries list for the current thread."""
    _tls.cypher_queries = []
    _tls.planning_data = []
    _tls.neo4j_results = []


def add_cypher_query(cypher_query: str, returned_data: bool = True, neo4j_result: dict = None):
    _ensure_tls()
    if cypher_query and cypher_query.strip():
        _tls.cypher_queries.append({
            'query': cypher_query.strip(),
            'returned_data': returned_data
        })
        if neo4j_result is not None:
            _tls.neo4j_results.append({
                'query': cypher_query.strip(),
                'result': neo4j_result
            })


def get_all_cypher_queries() -> List[str]:
    _ensure_tls()
    return [q['query'] for q in _tls.cypher_queries]


def get_queries_with_data() -> List[str]:
    _ensure_tls()
    return [q['query'] for q in _tls.cypher_queries if q['returned_data']]


def get_neo4j_results() -> List[dict]:
    _ensure_tls()
    return _tls.neo4j_results


# ============================================================================
# HALLUCINATION CHECKER
# ============================================================================

def extract_ids_from_text(text: str) -> dict:
    """
    Extract GO terms and PubMed IDs from text.
    
    Returns:
        dict with 'go_terms' and 'pubmed_ids' lists
    """
    # Extract GO terms (GO:XXXXXXX format)
    go_pattern = r'GO[_:]?\d{7}'
    go_terms = list(set(re.findall(go_pattern, text, re.IGNORECASE)))
    # Normalize to GO_XXXXXXX format
    go_terms = [g.upper().replace(':', '_') for g in go_terms]
    
    # Extract PubMed IDs (various formats)
    # Pattern 1: [PubMed ID: 12345678] or PubMed ID: 12345678
    pubmed_pattern1 = r'PubMed\s*(?:ID)?[:\s]*(\d{6,9})'
    # Pattern 2: PMID: 12345678 or PMID12345678
    pubmed_pattern2 = r'PMID[:\s]*(\d{6,9})'
    # Pattern 3: Just cited numbers in brackets that look like PubMed IDs
    pubmed_pattern3 = r'\[(\d{7,8})\]'
    
    pubmed_ids = set()
    pubmed_ids.update(re.findall(pubmed_pattern1, text, re.IGNORECASE))
    pubmed_ids.update(re.findall(pubmed_pattern2, text, re.IGNORECASE))
    pubmed_ids.update(re.findall(pubmed_pattern3, text))
    
    return {
        'go_terms': go_terms,
        'pubmed_ids': list(pubmed_ids)
    }


def extract_ids_from_retrieved_data(neo4j_results: List[dict], raw_agent_output: str = "") -> dict:
    """
    Extract all GO terms and PubMed IDs from the retrieved data.
    
    Args:
        neo4j_results: List of Neo4j query results
        raw_agent_output: Raw output from sub-agents (includes HIRN literature results)
    
    Returns:
        dict with 'go_terms' and 'pubmed_ids' sets
    """
    valid_go_terms = set()
    valid_pubmed_ids = set()
    
    # Extract from Neo4j results
    for result in neo4j_results:
        result_str = str(result)
        
        # Find GO terms in Neo4j data
        go_matches = re.findall(r'GO[_:]?\d{7}', result_str, re.IGNORECASE)
        for g in go_matches:
            valid_go_terms.add(g.upper().replace(':', '_'))
    
    # Extract from raw agent output (HIRN literature passages)
    if raw_agent_output:
        # PubMed IDs - multiple patterns to catch all formats
        # Pattern 1: [PubMed ID: 12345678] or PubMed ID: 12345678
        pubmed_matches = re.findall(r'PubMed\s*(?:ID)?[:\s]*(\d{6,9})', raw_agent_output, re.IGNORECASE)
        valid_pubmed_ids.update(pubmed_matches)
        
        # Pattern 2: PMID: 12345678 or PMID12345678
        pubmed_matches2 = re.findall(r'PMID[:\s]*(\d{6,9})', raw_agent_output, re.IGNORECASE)
        valid_pubmed_ids.update(pubmed_matches2)
        
        # Pattern 3: [12345678] (just bracketed numbers that look like PubMed IDs)
        pubmed_matches3 = re.findall(r'\[(\d{7,8})\]', raw_agent_output)
        valid_pubmed_ids.update(pubmed_matches3)
        
        # Pattern 4: Any 7-8 digit number near "pubmed" context
        pubmed_context = re.findall(r'(?:pubmed|pmid|literature|citation|reference)[^\d]*(\d{7,8})', raw_agent_output, re.IGNORECASE)
        valid_pubmed_ids.update(pubmed_context)
        
        # Also extract GO terms from agent output (HIRN might mention them)
        go_matches = re.findall(r'GO[_:]?\d{7}', raw_agent_output, re.IGNORECASE)
        for g in go_matches:
            valid_go_terms.add(g.upper().replace(':', '_'))
    
    return {
        'go_terms': valid_go_terms,
        'pubmed_ids': valid_pubmed_ids
    }


def check_hallucination(summary: str, neo4j_results: List[dict], raw_agent_output: str = "") -> dict:
    """
    Check if GO terms and PubMed IDs in the summary actually exist in retrieved data.
    
    Args:
        summary: The final summary text to check
        neo4j_results: List of Neo4j query results
        raw_agent_output: Raw output from sub-agents
    
    Returns:
        dict with:
            - 'summary_ids': IDs found in summary
            - 'valid_ids': IDs found in retrieved data
            - 'hallucinated_go_terms': GO terms in summary but not in data
            - 'hallucinated_pubmed_ids': PubMed IDs in summary but not in data
            - 'is_clean': True if no hallucinations detected
            - 'report': Human-readable report
    """
    # Debug: emit what we're checking
    found_pubmed = []
    if raw_agent_output:
        found_pubmed = re.findall(r'PubMed[^0-9]*(\d{6,9})', raw_agent_output, re.IGNORECASE)
    emit("hallucination_check_start", {
        "raw_agent_output_length": len(raw_agent_output) if raw_agent_output else 0,
        "pubmed_ids_in_output": found_pubmed,
    })
    
    # Extract IDs from summary
    summary_ids = extract_ids_from_text(summary)
    
    # Extract valid IDs from retrieved data
    valid_ids = extract_ids_from_retrieved_data(neo4j_results, raw_agent_output)
    
    # Find hallucinations
    hallucinated_go = [g for g in summary_ids['go_terms'] if g not in valid_ids['go_terms']]
    hallucinated_pubmed = [p for p in summary_ids['pubmed_ids'] if p not in valid_ids['pubmed_ids']]
    
    is_clean = len(hallucinated_go) == 0 and len(hallucinated_pubmed) == 0
    
    # Build report
    report_lines = ["=" * 60, "HALLUCINATION CHECK REPORT", "=" * 60]
    
    report_lines.append(f"\n📊 Summary contains:")
    report_lines.append(f"   - {len(summary_ids['go_terms'])} GO terms: {summary_ids['go_terms'][:5]}{'...' if len(summary_ids['go_terms']) > 5 else ''}")
    report_lines.append(f"   - {len(summary_ids['pubmed_ids'])} PubMed IDs: {summary_ids['pubmed_ids'][:5]}{'...' if len(summary_ids['pubmed_ids']) > 5 else ''}")
    
    report_lines.append(f"\n📚 Retrieved data contains:")
    report_lines.append(f"   - {len(valid_ids['go_terms'])} GO terms")
    report_lines.append(f"   - {len(valid_ids['pubmed_ids'])} PubMed IDs")
    
    if is_clean:
        report_lines.append(f"\n✅ NO HALLUCINATIONS DETECTED")
    else:
        report_lines.append(f"\n⚠️  HALLUCINATIONS DETECTED:")
        if hallucinated_go:
            report_lines.append(f"   ❌ Fake GO terms: {hallucinated_go}")
        if hallucinated_pubmed:
            report_lines.append(f"   ❌ Fake PubMed IDs: {hallucinated_pubmed}")
    
    report_lines.append("=" * 60)
    
    return {
        'summary_ids': summary_ids,
        'valid_ids': {
            'go_terms': list(valid_ids['go_terms']),
            'pubmed_ids': list(valid_ids['pubmed_ids'])
        },
        'hallucinated_go_terms': hallucinated_go,
        'hallucinated_pubmed_ids': hallucinated_pubmed,
        'is_clean': is_clean,
        'report': '\n'.join(report_lines)
    }

def remove_hallucinated_ids(summary: str, fake_go_terms: list, fake_pubmed_ids: list) -> str:
    """
    Remove hallucinated GO terms and PubMed IDs from the summary text.
    
    Args:
        summary: The summary text to clean
        fake_go_terms: List of GO term IDs to remove (e.g., ['GO_0005789'])
        fake_pubmed_ids: List of PubMed IDs to remove (e.g., ['34012112'])
    
    Returns:
        Cleaned summary with fake IDs removed
    """
    cleaned = summary
    
    # Remove fake PubMed IDs - various citation formats
    for pmid in fake_pubmed_ids:
        # [PubMed ID: 34012112]
        cleaned = re.sub(r'\s*\[PubMed\s*ID:\s*' + re.escape(pmid) + r'\]', '', cleaned)
        # [PMID: 34012112]
        cleaned = re.sub(r'\s*\[PMID:\s*' + re.escape(pmid) + r'\]', '', cleaned)
        # (PubMed ID: 34012112)
        cleaned = re.sub(r'\s*\(PubMed\s*ID:\s*' + re.escape(pmid) + r'\)', '', cleaned)
        # Standalone references like "PubMed ID: 34012112"
        cleaned = re.sub(r'\s*PubMed\s*ID:\s*' + re.escape(pmid) + r'\b', '', cleaned)
    
    # Remove fake GO terms - various formats
    for go_term in fake_go_terms:
        # Normalize to both formats: GO_0005789 and GO:0005789
        go_id = go_term.replace('_', ':') if '_' in go_term else go_term
        go_id_underscore = go_term.replace(':', '_') if ':' in go_term else go_term
        
        for go_variant in [go_id, go_id_underscore]:
            # Remove "term name (GO:XXXXXXX)" - the whole parenthetical
            cleaned = re.sub(r'\s*\(' + re.escape(go_variant) + r'\)', '', cleaned)
            # Remove "term name (GO_XXXXXXX)" 
            cleaned = re.sub(r'\s*\(' + re.escape(go_variant) + r'\)', '', cleaned)
            
            # Remove entire "term name (GO:XXXXXXX)," or "term name (GO:XXXXXXX)." patterns
            # This catches "chloride channel activity (GO:0005254), " 
            cleaned = re.sub(
                r'[^,.\n]*?' + re.escape(go_variant) + r'\)\s*,?\s*',
                '', cleaned
            )
    
    # Clean up artifacts: double spaces, trailing commas, empty parentheses
    cleaned = re.sub(r'\s{2,}', ' ', cleaned)  # Multiple spaces -> single
    cleaned = re.sub(r',\s*,', ',', cleaned)  # Double commas
    cleaned = re.sub(r',\s*\.', '.', cleaned)  # Comma before period
    cleaned = re.sub(r'\(\s*\)', '', cleaned)  # Empty parentheses
    cleaned = re.sub(r':\s*\.', '.', cleaned)  # Colon followed by period
    cleaned = re.sub(r':\s*$', '.', cleaned, flags=re.MULTILINE)  # Trailing colon
    cleaned = re.sub(r'including\s*[.,]', 'including:', cleaned)  # "including ," -> "including:"
    cleaned = re.sub(r'\s+([.,])', r'\1', cleaned)  # Space before punctuation
    
    return cleaned.strip()


def compress_neo4j_results(neo4j_results: List[dict]) -> List[dict]:
    """
    Compress raw Neo4j results into a compact format for the FormatAgent.

    Delegates to the canonical implementation in
    skills/format-agent/scripts/compress_neo4j.py so there is a single
    source of truth.
    """
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        'compress_neo4j_skill',
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     'skills', 'format-agent', 'scripts', 'compress_neo4j.py'))
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    return _mod.compress_neo4j_results(neo4j_results)


def add_planning_data(planning: dict):
    """Add planning data from PankBaseAgent"""
    _ensure_tls()
    if planning:
        _tls.planning_data.append(planning)

def get_all_planning_data() -> List[dict]:
    """Get all planning data for the current thread"""
    _ensure_tls()
    return _tls.planning_data

def process_document(content: str, metadata: dict = None) -> dict:
    result = {
        'abstract': None,
        'title': None,
        'pubmedid': None,
        'score': metadata.get('score', 0) if metadata else 0
    }
    lines = content.split('\n')
    for line in lines:
        if line.startswith('abstract: '):
            result['abstract'] = line.replace('abstract: ', '', 1)
        elif line.startswith('title: '):
            result['title'] = line.replace('title: ', '', 1)
        elif line.startswith('pubmedid: '):
            result['pubmedid'] = line.replace('pubmedid: ', '', 1)
            
    return result


def semantic_search(query: str, limit: int = 10) -> list:
    results = abstract_store.similarity_search(query, k=limit)
    results = [process_document(result.page_content, result.metadata) for result in results]
    return results


def run_cypher_2(command: str, parameters = None, timeout: int = 60):
    config = {"timeout": timeout}
    return driver_2.execute_query(command, parameters, **config)



__all__ = ['run_functions', 'reset_cypher_queries', 'add_cypher_query', 'get_all_cypher_queries', 'get_queries_with_data', 'get_neo4j_results', 'remove_hallucinated_ids', 'compress_neo4j_results']


def run_functions(functions: list[dict]) -> str:
    # Bounded leaf-level fan-out via a ThreadPoolExecutor instead of one raw
    # thread per function with a sleep() busy-wait. The leading "N." index on
    # each result preserves output ordering after as_completed.
    if not functions:
        return ''
    results = []

    def _call(i):
        name = functions[i]['name']
        input = functions[i]['input']
        func = eval(name)
        return func(input, i + 1)

    workers = max(1, min(len(functions), LEAF_THREADS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fut_to_i = {pool.submit(_call, i): i for i in range(len(functions))}
        for fut in as_completed(fut_to_i):
            i = fut_to_i[fut]
            try:
                result = fut.result()
            except Exception:
                result = (f"{i + 1}. {functions[i]['name']}: \n"
                          f"Status: error\nError: {traceback.format_exc()[:1000]}\n\n")
            # Handle tuple return (from pankbase_chat_one_round which returns (result, planning_data))
            if isinstance(result, tuple):
                result = result[0]  # Take just the text result
            results.append(result)
    results.sort(key=lambda x: int(x.split('.')[0]))
    return ''.join(results)



def test_a():
    a = 0
    b = 1
    c = b / a


def test_b():
    x = 4
    test_a()
    y = 5


def test_c():
    v = 8
    try:
        test_b()
    except:
        error_msg = traceback.format_exc()
    u = 10


if __name__ == "__main__":
    # Test Pankbase API functionality
    pass

def template_chat_one_round(input: str, index: int) -> str:
	q = Queue()
	start_new_thread(_Template_Tool_Call_one_round, (input, q))
	result = f'{index}. TemplateToolAgent chat_one_round: {str([input])[1:-1]}\n'
	try:
		success, res = q.get(timeout=100)
	except Empty:
		result += 'Status: timeout\n'
		result += 'Error: Cannot get response from TemplateToolAgent in 100 seconds\n\n'
		return result
	if (success == False):
		result += 'Status: error\n'
		result += f'Error: {str([res])[1:-1]}\n\n'
		return result
	result += 'Status: success\n'
	# No truncation - GPT-4o has 128K token context window
	result += f'Result: {res}\n\n'
	return result

def _Template_Tool_Call_one_round(input: str, q: Queue) -> str:
	try:
		sys.path.append('TemplateToolAgent')
		from TemplateToolAgent.ai_assistant import chat_one_round_ToolCall as tool_chat
		_, text = tool_chat([], input)
		q.put((True, text))
	except Exception:
		err_msg = traceback.format_exc()
		if (len(err_msg) > 2000):
			first = err_msg[:1000]
			second = err_msg[-1000:]
			err_msg = first + '  ... Middle part hidden due to length limit ...  ' + second
		q.put((False, err_msg))

def pankbase_chat_one_round(input: str, index: int) -> str:
	q = Queue()
	start_new_thread(_pankbase_chat_one_round, (input, q))
	result = f'{index}. PankBaseAgent chat_one_round: {str([input])[1:-1]}\n'
	planning_data = {}
	try:
		success, res, cypher_queries_str, planning_data = q.get(timeout=180)  # 3 min timeout for full pipeline
	except Empty:
		result += 'Status: timeout\n'
		result += 'Error: Cannot get response from PankBaseAgent in 180 seconds\n\n'
		return result, planning_data
	if (success == False):
		result += 'Status: error\n'
		result += f'Error: {str([res])[1:-1]}\n\n'
		return result, planning_data
	
	# Store planning data in global variable
	add_planning_data(planning_data)
	
	result += 'Status: success\n'
	result += f'Result: {res}\n\n'
	return result, planning_data

def _pankbase_chat_one_round(input: str, q: Queue) -> None:
	try:
		from PankBaseAgent.ai_assistant import chat_one_round_pankbase as pankbase_chat
		_messages, response_json, planning_data = pankbase_chat([], input)
		
		# Parse the response to extract cypher queries and store Neo4j results
		try:
			resp = json.loads(response_json) if isinstance(response_json, str) else response_json
			raw_results = resp.get('raw_results', [])
			queries_executed = resp.get('queries_executed', [])
			
			# Store each query and its result in the global trackers
			for item in raw_results:
				cypher = item.get('query', '')
				neo4j_result = item.get('result', {})
				if cypher:
					# Check if result has actual data
					has_data = True
					if isinstance(neo4j_result, dict):
						# New API format: {"records": [...], "keys": [...]}
						if "records" in neo4j_result:
							records = neo4j_result.get("records", [])
							if not records:
								has_data = False
							else:
								all_empty = all(
									not rec.get("nodes") and not rec.get("edges")
									for rec in records
									if isinstance(rec, dict)
								)
								if all_empty:
									has_data = False
						# Old API format: {"results": "...string..."}
						else:
							results_value = neo4j_result.get('results', '')
							if isinstance(results_value, str):
								norm = results_value.strip().lower()
								if norm == 'no results' or not norm:
									has_data = False
								elif 'nodes, edges' in norm and ('[], []' in norm or '[][]' in norm.replace(' ', '')):
									has_data = False
					add_cypher_query(cypher, returned_data=has_data, neo4j_result=neo4j_result)
		except (json.JSONDecodeError, TypeError):
			pass
		
		q.put((True, response_json, '', planning_data))
	except Exception:
		err_msg = traceback.format_exc()
		if (len(err_msg) > 2000):
			first = err_msg[:1000]
			second = err_msg[-1000:]
			err_msg = first + '  ... Middle part hidden due to length limit ...  ' + second
		q.put((False, err_msg, '', {}))

def hirn_chat_one_round(input: str, index: int) -> str:
	"""
	HIRN Literature Retrieve skill — searches HIRN publications for relevant passages.
	Searches ~1,160 HIRN publications, fetches full text from PMC,
	and returns relevant passages with PubMed IDs and citations.
	"""
	q = Queue()
	start_new_thread(_hirn_chat_one_round, (input, q))
	result = f'{index}. HIRN_literature chat_one_round: {str([input])[1:-1]}\n'
	try:
		success, res = q.get(timeout=30)
	except Empty:
		result += 'Status: timeout\n'
		result += 'Error: Cannot get response from HIRN Literature skill in 30 seconds\n\n'
		emit("hirn_result", {"status": "timeout", "query": input[:200]})
		return result
	if (success == False):
		result += 'Status: error\n'
		result += f'Error: {str([res])[1:-1]}\n\n'
		emit("hirn_result", {"status": "error", "query": input[:200], "error": str(res)[:500]})
		return result
	result += 'Status: success\n'
	result += f'Result: {res}\n\n'
	emit("hirn_result", {"status": "success", "query": input[:200], "result_length": len(res)})
	return result

def _hirn_chat_one_round(input: str, q: Queue) -> None:
	"""
	Run the HIRN Literature Retrieve skill pipeline:
	1. Fetch HIRN publication index
	2. Search titles by keyword
	3. Resolve PMCIDs
	4. Fetch full text from PMC
	5. Chunk and BM25-rank passages
	6. Return top results as JSON with PubMed IDs
	"""
	try:
		import json as _json
		import os as _os

		# Add the HIRN skill scripts to path
		hirn_skill_dir = _os.path.join(
			_os.path.dirname(_os.path.abspath(__file__)),
			'hirn_publication_retrieval', 'skills', 'hirn-literature-retrieve'
		)
		if hirn_skill_dir not in sys.path:
			sys.path.insert(0, hirn_skill_dir)

		from scripts.scrape_hirn import fetch_hirn_publications, search_publications
		from scripts.resolve_ids import resolve_pmcids
		from scripts.fetch_fulltext import fetch_fulltext
		from scripts.chunk_text import chunk_passages
		from scripts.search_chunks import search_chunks

		query = input.strip()
		emit("hirn_search_start", {"query": query[:200]})

		# Use the skill's data/cache directory
		cache_dir = _os.path.join(hirn_skill_dir, 'data', 'cache')
		_os.makedirs(cache_dir, exist_ok=True)

		# Step 1: Fetch HIRN publication index
		pubs = fetch_hirn_publications(cache_dir=cache_dir)
		emit("hirn_publications_loaded", {"count": len(pubs)})

		# Step 2: Search titles
		matches = search_publications(pubs, query=query, max_results=10)
		emit("hirn_matches_found", {"count": len(matches)})

		if not matches:
			result = _json.dumps({
				'source': 'hirn',
				'query_used': query,
				'publications_searched': len(pubs),
				'matches_found': 0,
				'raw_passages': [],
				'note': 'No HIRN publications matched the query'
			})
			q.put((True, result))
			return

		# Step 3: Resolve PMCIDs
		pmids = [p['pmid'] for p in matches if p.get('pmid')]
		pmcid_map = resolve_pmcids(pmids, cache_dir=cache_dir) if pmids else {}
		emit("hirn_pmcids_resolved", {
			"resolved": len([v for v in pmcid_map.values() if v]),
			"total_pmids": len(pmids),
		})

		# Step 4 & 5: Fetch full text and search chunks for each article
		all_results = []
		for pub in matches:
			pmid = pub.get('pmid', '')
			pmcid = pmcid_map.get(pmid)

			if not pmcid:
				# No full text available — include metadata only
				all_results.append({
					'pmid': pmid,
					'pmcid': None,
					'article_title': pub.get('title', ''),
					'doi': pub.get('doi', ''),
					'authors': pub.get('authors', ''),
					'consortia': pub.get('consortia', []),
					'text': f"Title: {pub.get('title', '')}",
					'section': 'TITLE_ONLY',
					'score': 0.0,
					'note': 'Full text not available in PMC Open Access'
				})
				continue

			ft = fetch_fulltext(pmcid, cache_dir=cache_dir)
			if not ft.get('success'):
				all_results.append({
					'pmid': pmid,
					'pmcid': pmcid,
					'article_title': pub.get('title', ''),
					'doi': pub.get('doi', ''),
					'authors': pub.get('authors', ''),
					'text': f"Title: {pub.get('title', '')}",
					'section': 'TITLE_ONLY',
					'score': 0.0,
					'note': f"Full text fetch failed: {ft.get('error', {}).get('message', 'unknown')}"
				})
				continue

			chunks = chunk_passages(ft['passages'])
			hits = search_chunks(chunks, query=query, top_k=3)
			for h in hits:
				h['pmid'] = pmid
				h['pmcid'] = pmcid
				h['article_title'] = pub.get('title', '')
				h['doi'] = pub.get('doi', '')
				h['authors'] = pub.get('authors', '')
				h['consortia'] = pub.get('consortia', [])
			all_results.extend(hits)

		# Sort all chunks by score
		all_results.sort(key=lambda x: x.get('score', 0), reverse=True)

		# Take top 15 results
		top_results = all_results[:15]

		emit("hirn_chunks_ready", {"count": len(top_results)})

		result = _json.dumps({
			'source': 'hirn',
			'query_used': query,
			'publications_searched': len(pubs),
			'matches_found': len(matches),
			'passages_returned': len(top_results),
			'raw_passages': top_results
		})
		q.put((True, result))

	except Exception:
		err_msg = traceback.format_exc()
		if (len(err_msg) > 2000):
			first = err_msg[:1000]
			second = err_msg[-1000:]
			err_msg = first + '  ... Middle part hidden due to length limit ...  ' + second
		q.put((False, err_msg))


instrument_module_functions(globals(), include_private=True)
