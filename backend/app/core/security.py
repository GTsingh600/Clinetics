"""Password hashing and JWT issuance.

**Why `bcrypt` directly and not `passlib`.** The Phase 0 lockfile pinned
`passlib[bcrypt]`, which is the conventional choice, but passlib is effectively
unmaintained and reads `bcrypt.__about__.__version__` during backend detection —
an attribute removed in bcrypt 4.1. Against the bcrypt 5.0 in our lockfile it
fails outright. Rather than pinning bcrypt backwards to keep an unmaintained
wrapper alive, this module calls `bcrypt` directly. Its API is three functions,
so the wrapper was buying very little.

**The 72-byte problem.** bcrypt only considers the first 72 bytes of a password;
historically implementations silently truncated, and bcrypt 5 now raises instead.
Silent truncation is the dangerous behaviour: a 100-character passphrase would be
authenticated by its first 72 bytes, and two distinct long passwords could
collide. The fix used here is the standard one — pre-hash with SHA-256 and
base64-encode, producing a fixed 44-byte input that is always within the limit
and never truncated.

Base64 rather than raw digest bytes because a raw SHA-256 digest can contain a
NUL byte, and bcrypt treats NUL as a string terminator — which would throw away
the rest of the digest and dramatically weaken the hash.

**Tokens.** Two kinds, deliberately different:

* *access*  — short-lived (30 min), sent on every request, never stored server-side.
* *refresh* — long-lived (7 days), rotated on every use, and tracked in the
  database so a stolen token can be detected and the whole family revoked.

Both carry a `jti` (unique token id) and a `typ` claim. The `typ` check matters:
without it a refresh token would be accepted as an access token, silently
granting a 7-day session to a credential meant only for the refresh endpoint.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import uuid
from typing import Any, Literal

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings

TokenType = Literal["access", "refresh"]

# bcrypt work factor. 12 is a reasonable 2020s default: roughly 250ms per hash
# on commodity hardware, which is slow enough to make offline brute force
# expensive and fast enough not to become a login-path DoS vector.
BCRYPT_ROUNDS = 12


def _prepare(password: str) -> bytes:
    """SHA-256 + base64 so the input is always exactly 44 bytes.

    See the module docstring: this avoids bcrypt's 72-byte limit without
    silently truncating, and base64 avoids NUL bytes terminating the string.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time comparison, and never raises on a malformed stored hash.

    A corrupt or legacy hash must fail closed (return False) rather than 500 —
    an exception here would leak, through response codes, which accounts have
    unusual hashes.
    """
    try:
        return bcrypt.checkpw(_prepare(password), hashed.encode())
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def _create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: dt.timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str, dt.datetime]:
    """Return (encoded_jwt, jti, expires_at)."""
    now = dt.datetime.now(dt.UTC)
    expires_at = now + expires_delta
    jti = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    encoded = jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
    return encoded, jti, expires_at


def create_access_token(user_id: int, role: str) -> tuple[str, str, dt.datetime]:
    """The role is embedded so authorization needs no database round-trip.

    The trade-off is staleness: a role changed mid-session stays in effect until
    the access token expires. Thirty minutes is the bound on that, and it is why
    the access token is short-lived.
    """
    return _create_token(
        subject=str(user_id),
        token_type="access",
        expires_delta=dt.timedelta(minutes=settings.access_token_expire_minutes),
        extra_claims={"role": role},
    )


def create_refresh_token(user_id: int, family_id: str) -> tuple[str, str, dt.datetime]:
    """`family_id` links every token descended from one login.

    Reuse of an already-rotated refresh token means the token was captured, so
    the entire family is revoked rather than just that one token.
    """
    return _create_token(
        subject=str(user_id),
        token_type="refresh",
        expires_delta=dt.timedelta(days=settings.refresh_token_expire_days),
        extra_claims={"fam": family_id},
    )


class TokenError(Exception):
    """Raised for any invalid, expired, or wrong-type token."""


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode and validate, including the token *type*.

    Checking `typ` is not optional. Without it, a refresh token — which lives
    for seven days — would be accepted wherever an access token is, handing out
    a week-long session from a credential intended only for /auth/refresh.
    """
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
    except JWTError as exc:
        raise TokenError(str(exc)) from exc

    if payload.get("typ") != expected_type:
        raise TokenError(f"expected a {expected_type} token, got {payload.get('typ')!r}")
    if "sub" not in payload:
        raise TokenError("token has no subject")
    return payload


def hash_token(token: str) -> str:
    """Fingerprint a refresh token for storage.

    The raw token is never stored, for the same reason passwords are not: a
    database leak must not hand out live sessions. SHA-256 rather than bcrypt is
    correct here — the input is a 256-bit random value, not a guessable human
    password, so there is nothing for a slow hash to defend against, and the
    refresh path would otherwise pay 250ms per request.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
