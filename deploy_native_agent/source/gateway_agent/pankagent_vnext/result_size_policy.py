"""Programmatic fallback for oversized formatter views, never a database LIMIT."""
import json

EXAMPLE_LIMIT = 100


def oversized_result_metadata(evidence, max_bytes):
    observed = len(json.dumps(evidence, ensure_ascii=False, separators=(',', ':'),
                              allow_nan=False).encode())
    if observed <= max_bytes:
        return None
    return {
        'trigger': 'result_exceeds_formatter_byte_budget',
        'observed_result_bytes': observed,
        'formatter_budget_bytes': max_bytes,
        'example_limit': EXAMPLE_LIMIT,
        'scope': 'formatter_input_only',
        'backend_results_preserved': True,
        'llm_instruction': 'This query result exceeded the input budget. The program reduced its '
            'example view to at most 100 records per collection; byte limits may retain fewer. '
            'Do not count these examples as the full population or interpret omitted records as absent. '
            'Use explicit backend summaries with their stated retrieval completeness for counts.',
    }
