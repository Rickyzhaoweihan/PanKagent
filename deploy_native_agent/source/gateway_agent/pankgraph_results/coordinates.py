"""Bounded access to the existing shared dbSNP reader; no index replication."""
import asyncio
import csv
import io
import json
import re
from pathlib import Path


class CoordinateLookup:
    def __init__(self, command="dbsnp-query"):
        self.command = command
        self.cache = {}
        self.last_error = None
        self.metadata = None

    async def _run(self, *args):
        process = await asyncio.create_subprocess_exec(self.command, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), 5)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode or len(output) > 8 * 1024 * 1024:
            raise RuntimeError("coordinate_lookup_failed")
        return output

    async def __call__(self, ids):
        ids = list(dict.fromkeys(str(v) for v in ids if re.fullmatch(r"rs\d+", str(v))))[:5000]
        missing = [value for value in ids if value not in self.cache]
        for offset in range(0, len(missing), 100):
            group = missing[offset:offset + 100]
            try:
                if self.metadata is None:
                    metadata = json.loads(await self._run("metadata"))
                    # RefSeq assembly accession is fixed by the shared-service
                    # contract: GCF_000001405.40 is GRCh38.p14, dbSNP build 157.
                    if str(metadata.get("dbsnp_build")) != "157" or Path(metadata.get("vcf", "")).name != "GCF_000001405.40.gz" or not metadata.get("indexed"):
                        raise RuntimeError("coordinate_release_mismatch")
                    self.metadata = {"assembly": "GRCh38.p14", "dbsnp_build": "157", "accession": "GCF_000001405.40"}
                output = await self._run("rsid", *group, "--format", "csv")
                found = {}
                ambiguous = set()
                for row in csv.DictReader(io.StringIO(output.decode())):
                    rid = row.get("id", row.get("rsid", row.get(":ID")))
                    chrom = row.get("chr:String", row.get("chromosome", row.get("chr", row.get("chrom"))))
                    assembly = row.get("genome_assembly", row.get("assembly", "GRCh38.p14"))
                    # The shared contract supplies BED start coordinates. Only
                    # explicitly identified coordinate columns are interpreted.
                    start = row.get("start_loc:Int", row.get("start", row.get("start:long")))
                    pos = int(start) + 1 if start not in (None, "") else int(row["pos"]) if row.get("pos") else None
                    if rid in group and chrom and pos and assembly in {"GRCh38", "GRCh38.p14", "hg38"}:
                        value = {"chrom": str(chrom).removeprefix("chr"), "pos": pos, "assembly": "GRCh38", "source": "dbSNP157_GRCh38.p14", "verified": True}
                        if rid in ambiguous or (rid in found and found[rid] != value):
                            ambiguous.add(rid)
                            found[rid] = None
                        else:
                            found[rid] = value
                self.cache.update(found)
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                break
        if len(self.cache) > 10000:
            self.cache = {key: self.cache[key] for key in ids if key in self.cache}
        return {rid: self.cache[rid] for rid in ids if self.cache.get(rid)}
