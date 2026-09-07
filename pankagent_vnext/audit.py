"""Content-free deployment identity and task-local provider accounting hooks."""
from contextvars import ContextVar
import hashlib
from pathlib import Path

recorder = ContextVar("pank_audit_recorder", default=None)


def provider_event(kind, payload):
    callback = recorder.get()
    if callback is not None:
        callback(kind, payload)


def deployment_identity(settings):
    root = Path(__file__).parent
    groups = {"code": list(root.glob("*.py")), "prompts": list((root / "prompts").glob("*")),
              "skills": list((root / "answer_skills").rglob("*.json"))}
    result = {"model": settings.model, "graph_version": settings.graph_version,
              "corpus_version": settings.corpus_version, "source_policy": settings.source_policy,
              "literature_api_version": settings.literature_api_version}
    for name, paths in groups.items():
        digest = hashlib.sha256()
        for path in sorted(paths):
            if path.is_file():
                digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes())
        result[name + "_sha256"] = digest.hexdigest()
    return result

from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field

class InteractionRequest(BaseModel):
    event_id: UUID
    page_id: UUID
    kind: Literal["plan_displayed", "answer_section_displayed", "graph_evidence_inspected", "resource_accessed"]
    target_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    client_timestamp: datetime
    client_elapsed_ms: float = Field(ge=0, le=86400000, allow_inf_nan=False)


