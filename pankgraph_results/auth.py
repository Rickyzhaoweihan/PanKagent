"""Shared demo Basic authentication without a new frontend login page."""
import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return "pbkdf2_sha256$250000$" + salt + "$" + hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 250000).hex()


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256" or not 100000 <= int(rounds) <= 1000000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


class DemoAuthentication:
    def __init__(self, app, settings):
        self.app, self.settings = app, settings
        self.cache = {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or self.settings.testing:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        # Health and metrics are never accepted through this exception when a
        # reverse proxy identifies an external client.
        local_operator = scope.get("client", ("",))[0] in {"127.0.0.1", "::1"} and b"x-forwarded-for" not in headers
        if local_operator and scope["path"] in {"/health/live", "/health/ready", "/health/components", "/metrics"}:
            return await self.app(scope, receive, send)
        if not self.settings.password_hash:
            return await JSONResponse({"detail": "Demo authentication is not configured."}, status_code=503)(scope, receive, send)
        raw = headers.get(b"authorization", b"")
        key = hashlib.sha256(raw).digest()
        allowed = self.cache.get(key, 0) > time.monotonic()
        if not allowed:
            try:
                if len(raw) > 4096 or not raw.startswith(b"Basic "):
                    raise ValueError()
                username, password = base64.b64decode(raw[6:], validate=True).decode().split(":", 1)
                allowed = hmac.compare_digest(username, self.settings.basic_user) and await asyncio.to_thread(verify_password, password, self.settings.password_hash)
                if allowed:
                    if len(self.cache) >= 128:
                        self.cache.clear()
                    self.cache[key] = time.monotonic() + 300
            except (ValueError, UnicodeError):
                allowed = False
        if not allowed:
            return await JSONResponse({"detail": "Demo login required."}, status_code=401, headers={"WWW-Authenticate": 'Basic realm="PanKgraph demo", charset="UTF-8"', "Cache-Control": "no-store"})(scope, receive, send)
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            # Native Basic auth is ambient browser authority. Deny cross-site
            # mutations even though no permissive CORS policy is configured.
            origin = headers.get(b"origin", b"").decode("latin1")
            host = headers.get(b"host", b"").decode("latin1")
            if headers.get(b"sec-fetch-site") == b"cross-site" or (origin and urlsplit(origin).netloc != host):
                return await JSONResponse({"detail": "Cross-site request denied."}, status_code=403)(scope, receive, send)
        return await self.app(scope, receive, send)
