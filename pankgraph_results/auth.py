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
        # The standalone operator dashboard enforces its own independent credentials.
        # Only this exact namespace is delegated; it never reaches regular app data.
        if any(scope["path"] == prefix or scope["path"].startswith(prefix + "/")
               for prefix in ("/pankgraph/health", "/health-dashboard")):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        # Health and metrics are never accepted through this exception when a
        # reverse proxy identifies an external client.
        local_operator = scope.get("client", ("",))[0] in {"127.0.0.1", "::1"} and b"x-forwarded-for" not in headers
        if local_operator and scope["path"] in {"/health/live", "/health/ready", "/health/components", "/metrics"}:
            return await self.app(scope, receive, send)
        # Dev may disable the additional agent API login independently of site access.
        agent_api = scope["path"].startswith("/api/agent/")
        password_required = not (agent_api and not getattr(self.settings, "agent_api_basic_auth", True))
        if password_required and not self.settings.password_hash:
            return await JSONResponse({"detail": "Demo authentication is not configured."}, status_code=503)(scope, receive, send)
        raw = headers.get(b"authorization", b"")
        key = hashlib.sha256(raw).digest()
        allowed = not password_required or self.cache.get(key, 0) > time.monotonic()
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
            response_headers = {"Cache-Control": "no-store"}
            # PrefixMiddleware has removed the public prefix. A background
            # access probe must return promptly so the UI can show its sign-in
            # link; only top-level navigation should open the native prompt.
            if scope["method"] != "GET" or scope["path"] != "/api/access":
                response_headers["WWW-Authenticate"] = 'Basic realm="PanKgraph demo", charset="UTF-8"'
            return await JSONResponse({"detail": "Demo login required."}, status_code=401, headers=response_headers)(scope, receive, send)
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            # Native Basic auth is ambient browser authority. Deny cross-site
            # mutations even though no permissive CORS policy is configured.
            origin = headers.get(b"origin", b"").decode("latin1")
            host = headers.get(b"host", b"").decode("latin1")
            # Amplify keeps the dev browser Origin while the upstream Host may
            # name jieliu3. This single protected opt-in is not a CORS policy.
            trusted_origin = getattr(self.settings, "trusted_browser_origin", "")
            if headers.get(b"sec-fetch-site") == b"cross-site" or (origin and urlsplit(origin).netloc != host and origin != trusted_origin):
                return await JSONResponse({"detail": "Cross-site request denied."}, status_code=403)(scope, receive, send)
        return await self.app(scope, receive, send)
