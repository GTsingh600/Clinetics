"""CSRF protection for the cookie-based session.

Cookies are attached by the browser automatically, on *any* request to this
origin — including one triggered by a form on someone else's site. That is the
whole CSRF problem: an attacker cannot read the cookie, but they can cause it to
be sent. Moving tokens into httpOnly cookies solves XSS token theft and creates
this exposure, so both defences are needed together.

Two layers here:

1. **`SameSite`** on the cookies themselves (set in `api/v1/auth.py`). Modern
   browsers will not attach a `lax` cookie to a cross-site POST at all. This is
   the primary defence.

2. **Origin checking** in this middleware, for unsafe methods. `SameSite` relies
   on the browser being current and correct; an explicit server-side check does
   not. If a state-changing request carries an `Origin` or `Referer` that is not
   an allowed origin, it is rejected.

Why an origin check rather than the classic double-submit token: a
double-submit token must be readable by JavaScript to be echoed into a header,
which means it must NOT be httpOnly — and an XSS that can read that token can
forge requests anyway. The origin header cannot be set by page JavaScript at
all, so it is the stronger signal here, and it needs no token plumbing.

Requests with no `Origin` and no `Referer` are allowed: non-browser clients
(curl, the test suite, server-to-server calls) do not send them, and they are
not subject to CSRF because nothing is auto-attaching a cookie for them.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from fastapi import Request, Response, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


class CSRFMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Callable, allowed_origins: list[str]) -> None:
        super().__init__(app)
        self.allowed = {self._normalise(o) for o in allowed_origins}

    @staticmethod
    def _normalise(origin: str) -> str:
        parsed = urlparse(origin)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
        return origin.rstrip("/")

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method in SAFE_METHODS:
            return await call_next(request)

        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        candidate = origin or referer

        # No Origin and no Referer: not a browser-initiated cross-site request.
        if candidate is None:
            return await call_next(request)

        if self._normalise(candidate) not in self.allowed:
            log.warning(
                "CSRF: rejected %s %s from origin %r", request.method, request.url.path, candidate
            )
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "cross-origin request rejected"},
            )

        return await call_next(request)
