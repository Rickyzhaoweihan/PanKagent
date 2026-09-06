"""Allowlisted public objects; never discover resources by listing an S3 bucket."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import quote

REGISTRY_VERSION = "2026-09-06.1"
PUBLIC_ORIGIN = "https://pank-s3-to-share.s3.us-east-1.amazonaws.com"
TSV_COLUMNS = ("snp", "pip", "nominal_p", "effect_allele", "other_allele", "slope", "lbf")


@dataclass(frozen=True)
class ResourceSource:
    id: str
    prefix: str
    aliases: tuple[str, ...]
    kind: str = "QTL"
    verified: bool = True
    assembly: str = "GRCh38"

    def object_key(self, credible_set: str) -> str:
        # A graph property is a filename component, never a path or a URL.
        if not isinstance(credible_set, str) or not re.fullmatch(r"[A-Za-z0-9_:.+-]{1,240}", credible_set):
            raise ValueError("invalid_credible_set")
        if ".." in credible_set or credible_set.endswith(".txt"):
            raise ValueError("invalid_credible_set")
        return f"{self.prefix}/{credible_set}.txt"

    def url(self, credible_set: str) -> str:
        return PUBLIC_ORIGIN + "/" + quote(self.object_key(credible_set), safe="/")


SOURCES = (
    ResourceSource("gtex_eqtl", "1_eQTL-gtex-susie", ("GTEx; SusieR",)),
    ResourceSource("inspire_eqtl", "1_eQTL-inspire-susie", ("INSPIRE; SusieR",)),
    ResourceSource("gtex_sqtl", "1_sQTL-gtex-susie", ("splicing; GTEx",)),
    ResourceSource("inspire_exonqtl", "1_exonQTL-inspire-susie", ("exon; INSPIRE",)),
    # The prefix comes from the legacy contract; availability is not established.
    ResourceSource("t1d_gwas", "1_t1d-susie", ("T1D; SusieR", "T1D"), "GWAS", False),
)


def source_for(data_source: str, registry: tuple[ResourceSource, ...] = SOURCES) -> ResourceSource | None:
    normalized = str(data_source).strip().casefold()
    return next((source for source in registry if normalized in {
        source.id.casefold(), source.prefix.casefold(), *(alias.casefold() for alias in source.aliases)
    }), None)


def registry_snapshot(registry: tuple[ResourceSource, ...] = SOURCES) -> dict:
    return {"version": REGISTRY_VERSION, "origin": PUBLIC_ORIGIN, "discovery": "exact_key_only",
            "sources": [{"id": source.id, "prefix": source.prefix, "kind": source.kind,
                         "verification": "verified_prefix" if source.verified else "unverified_prefix"}
                        for source in registry]}
