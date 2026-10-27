"""Loopback desktop access boundary; no platform credentials are involved."""
from __future__ import annotations

import secrets
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

COOKIE_NAME = "jobkit_session"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LocalAccessMiddleware(BaseHTTPMiddleware):
    """Browser CSRF/DNS-rebinding protection, not isolation from local malware.

    A loopback navigation bootstraps an HttpOnly process-scoped session. The
    desktop and its tool frame use the same origin, including custom ports.
    Untrusted origins are rejected even if they supply a valid cookie.
    """

    def __init__(self, app):
        super().__init__(app)
        self._session = secrets.token_urlsafe(32)

    async def dispatch(self, request, call_next):
        try:
            host = urlsplit("//" + request.headers.get("host", ""))
            valid_host = (host.hostname in LOOPBACK_HOSTS and host.username is None
                          and host.password is None and host.path == "" and not host.query
                          and not host.fragment and host.port != 0)
        except ValueError:
            valid_host = False
        if not valid_host:
            return self._denied(403, "LOCAL_HOST_REQUIRED")

        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.headers['host']}"
        if origin is not None and origin != expected:
            return self._denied(403, "UNTRUSTED_ORIGIN")
        if request.headers.get("sec-fetch-site") == "cross-site":
            return self._denied(403, "CROSS_SITE_REQUEST")

        bootstrap = request.url.path in {"/", "/workspace", "/desktop", "/controlled-chat"} and request.method in {"GET", "HEAD"}
        # /health 与 /version 只暴露进程身份与构建指纹（文件名+哈希+版本号），
        # 不含路径、正文或凭据；与 /health 同级放行，其余接口仍要会话 cookie。
        public_identity = request.url.path in {"/health", "/version"} and request.method in {"GET", "HEAD"}
        supplied = request.cookies.get(COOKIE_NAME, "")
        if not (bootstrap or public_identity) and not secrets.compare_digest(supplied.encode(), self._session.encode()):
            return self._denied(401, "LOCAL_SESSION_REQUIRED")

        if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/runtime/"):
            from sandbox import KernelError, require_registered
            try:
                require_registered("legacy.http")
            except KernelError as exc:
                return JSONResponse({"ok": False, "code": exc.code, "error": str(exc),
                                     "trace_id": getattr(exc, "trace_id", None)}, status_code=exc.status)

        response = await call_next(request)
        if bootstrap and response.status_code == 200:
            response.set_cookie(COOKIE_NAME, self._session, httponly=True,
                                samesite="strict", secure=request.url.scheme == "https")
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        return response

    @staticmethod
    def _denied(status, code):
        return JSONResponse({"ok": False, "error": "请从本机应用窗口重新打开。", "code": code},
                            status_code=status, headers={"Cache-Control": "no-store"})
