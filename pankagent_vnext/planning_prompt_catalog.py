"""Shared, inspectable domain prompt catalog for both planning routes.

These hints never bypass runtime entity, inventory or request-scope validation.
"""
import hashlib
import json
from pathlib import Path
from .coloc_planning_guidance import GUIDANCE as COLOC_GUIDANCE

ROOT = Path(__file__).parent / 'prompts' / 'planning'
MODALITIES = json.loads((ROOT / 'modalities.json').read_text())
labels = [entry['label'] for entry in MODALITIES]
if len(labels) != len(set(labels)) or any(
    not isinstance(entry['label'], str) or not entry['label']
    or not isinstance(entry['aliases'], list)
    or not all(isinstance(alias, str) and alias for alias in entry['aliases'])
    for entry in MODALITIES
):
    raise ValueError('invalid_planner_modality_catalog')
MODULES = (
    ('colocalization', COLOC_GUIDANCE),
    ('donor_samples', (ROOT / 'donor_samples.md').read_text()),
    ('sample_modalities', 'Recorded sample modality terminology hints:\n' + json.dumps(MODALITIES)),
)
GUIDANCE = '\n'.join(f'\nPlanning module: {name}\n{text}' for name, text in MODULES)
DIGEST = hashlib.sha256(GUIDANCE.encode()).hexdigest()
