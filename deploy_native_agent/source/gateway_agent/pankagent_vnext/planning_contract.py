"""Compact grounded planning instructions and conservative cache identities."""
import hashlib
from .agent_schemas import module as schema_module
import json
import time
from collections import OrderedDict
from pathlib import Path

VERSION = 'grounded-planning-v14-domain-catalog'
SYSTEM = schema_module('semantics_modalities')['planning_instructions']['grounded']

from .composable_planning import GUIDANCE
SYSTEM += GUIDANCE
from .planning_prompt_catalog import GUIDANCE as DOMAIN_PLANNING_GUIDANCE
SYSTEM += DOMAIN_PLANNING_GUIDANCE

DIGEST = hashlib.sha256(Path(__file__).read_bytes() + DOMAIN_PLANNING_GUIDANCE.encode()).hexdigest()


class VerifiedCache:
    """Process-local bounded cache; contents are private and never evidence truth.

    Exact request/history hashes only. No embedding-neighbor substitution of
    filters, disease stages or entity identities. Callers revalidate all hits.
    """
    def __init__(self, limit=128, ttl=300):
        self.limit, self.ttl, self.values = limit, ttl, OrderedDict()

    @staticmethod
    def key(*parts):
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()

    def get(self, key):
        from copy import deepcopy
        entry = self.values.get(key)
        if not entry or time.monotonic() - entry[0] > self.ttl:
            self.values.pop(key, None)
            return None
        self.values.move_to_end(key)
        return deepcopy(entry[1])

    def put(self, key, value):
        from copy import deepcopy
        self.values[key] = (time.monotonic(), deepcopy(value))
        self.values.move_to_end(key)
        while len(self.values) > self.limit:
            self.values.popitem(last=False)
