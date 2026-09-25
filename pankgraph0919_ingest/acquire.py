"""File acquisition with explicit budgets, verified hashes and no partial imports."""
from __future__ import annotations

import hashlib
from pathlib import Path
import urllib.parse
import urllib.request
import urllib.error
import time

from .catalog import USER_AGENT, utc_now

ALLOWED_HOSTS = {"pankbase-data-v1.s3.us-west-2.amazonaws.com", "api.data.pankbase.org"}
PRIVATE_MATRIX_ALLOWLIST = {"PKBFI7107QVIN"}


class AcquisitionBlocked(Exception):
    def __init__(self, reason: str, **details):
        self.reason, self.details = reason, details
        super().__init__(reason)


def public_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname in ALLOWED_HOSTS
            and not parsed.username and not parsed.password)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not public_url(newurl):
            raise AcquisitionBlocked("unapproved_redirect_host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Downloader:
    def __init__(self, directory: Path, max_file_bytes: int, max_total_bytes: int, timeout: int = 90,
                 stage_public_matrix: bool = False):
        if max_file_bytes < 1 or max_total_bytes < 1:
            raise ValueError("Byte budgets must be positive")
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.timeout = timeout
        self.total_bytes = 0
        self.stage_public_matrix = stage_public_matrix
        self.opener = urllib.request.build_opener(SafeRedirect())

    def acquire(self, source: dict) -> dict:
        for attempt in range(3):
            try:
                return self._acquire(source)
            except urllib.error.HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
            except (TimeoutError, urllib.error.URLError):
                if attempt == 2:
                    raise
            time.sleep(1 + attempt)
        raise RuntimeError("unreachable")

    def _acquire(self, source: dict) -> dict:
        if source.get("status") != "released":
            raise AcquisitionBlocked("source_not_released")
        private_matrix = self.stage_public_matrix and source.get("accession") in PRIVATE_MATRIX_ALLOWLIST
        if source.get("controlled_access") is True or (source.get("controlled_access") is not False and not private_matrix):
            raise AcquisitionBlocked("access_controlled_or_unknown")
        if "TabularFile" not in source.get("@type", []) and not private_matrix:
            raise AcquisitionBlocked("non_tabular_download_not_enabled")
        url = source.get("file_url", "")
        if not public_url(url):
            raise AcquisitionBlocked("missing_or_unapproved_direct_url")
        remaining = self.max_total_bytes - self.total_bytes
        if remaining <= 0:
            raise AcquisitionBlocked("total_byte_budget_exhausted")
        declared = source.get("file_size")
        if isinstance(declared, int) and declared > min(self.max_file_bytes, remaining):
            raise AcquisitionBlocked("declared_size_exceeds_budget", declared_bytes=declared)
        path = self.directory / (source["accession"] + ".source")
        temporary = path.with_suffix(".partial")
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        sha, md5, size = hashlib.sha256(), hashlib.md5(), 0
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise AcquisitionBlocked("full_file_http_status", http_status=response.status)
                if response.headers.get("Content-Range"):
                    raise AcquisitionBlocked("partial_content_response")
                length = response.headers.get("Content-Length")
                if length and int(length) > min(self.max_file_bytes, remaining):
                    raise AcquisitionBlocked("content_length_exceeds_budget", declared_bytes=int(length))
                with temporary.open("wb") as output:
                    temporary.chmod(0o600)
                    while True:
                        chunk = response.read(min(1024**2, min(self.max_file_bytes, remaining) - size + 1))
                        if not chunk:
                            break
                        size += len(chunk)
                        self.total_bytes += len(chunk)
                        if size > self.max_file_bytes or size > remaining:
                            raise AcquisitionBlocked("stream_size_exceeds_budget", bytes_received=size)
                        output.write(chunk)
                        sha.update(chunk)
                        md5.update(chunk)
                if length and size != int(length):
                    raise AcquisitionBlocked("content_length_mismatch", bytes_received=size)
            expected_md5 = source.get("md5sum")
            if expected_md5 and expected_md5.lower() != md5.hexdigest():
                raise AcquisitionBlocked("catalog_md5_mismatch", computed_md5=md5.hexdigest())
            temporary.replace(path)
            return {"path": str(path), "sha256": sha.hexdigest(), "md5": md5.hexdigest(),
                    "private_staging_only": private_matrix,
                    "acquisition_access_basis": "explicit_private_public_url_staging" if private_matrix else "catalog_controlled_access_false",
                    "catalog_md5_verified": bool(expected_md5), "bytes": size,
                    "retrieved_utc": utc_now(), "url": url}
        finally:
            if temporary.exists():
                temporary.unlink()
