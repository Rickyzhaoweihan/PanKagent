"""Shared, inspectable domain prompt catalog for both planning routes.

These hints never bypass runtime entity, inventory or request-scope validation.
"""
import hashlib
import json
from pathlib import Path
from .agent_schemas import module as schema_module

ROOT = Path(__file__).parent / 'prompts' / 'planning'
MODALITIES = schema_module('semantics_modalities')['modalities']
labels = [entry['label'] for entry in MODALITIES]
if len(labels) != len(set(labels)) or any(
    not isinstance(entry['label'], str) or not entry['label']
    or not isinstance(entry['aliases'], list)
    or not all(isinstance(alias, str) and alias for alias in entry['aliases'])
    for entry in MODALITIES
):
    raise ValueError('invalid_planner_modality_catalog')
MODULES = tuple((item['id'], item['text']) for item in schema_module('semantics_modalities')['prompt_modules']) + (
    ('sample_modalities', 'Recorded sample modality terminology hints:\n' + json.dumps(MODALITIES)),
)

GUIDANCE = '\n'.join(f'\nPlanning module: {name}\n{text}' for name, text in MODULES)
DIGEST = hashlib.sha256(GUIDANCE.encode()).hexdigest()
