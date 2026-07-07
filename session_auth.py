"""HttpOnly session cookie helpers — portal-isolated POS vs Admin auth."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request, Response
from jose import JWTError

from config import settings
from security import decode_access_token


def portal_cookie_name(portal: str) -> str:
    return settings.admin_session_cookie_name if portal == "admin" else settings.session_cookie_name


def iter_auth_tokens(
    request: Request,
    header_token: str | None,
    *,
    portal: str | None = None,
) -> Iterator[str]:
    """Yield candidate JWTs: Authorization header first, then portal cookie(s)."""
    seen: set[str] = set()

    if header_token and header_token not in seen:
        seen.add(header_token)
        yield header_token

    if portal == "admin":
        cookie_names = [settings.admin_session_cookie_name]
    elif portal == "pos":
        cookie_names = [settings.session_cookie_name]
    else:
        cookie_names = [settings.session_cookie_name, settings.admin_session_cookie_name]

    for name in cookie_names:
        value = request.cookies.get(name)
        if value and value not in seen:
            seen.add(value)
            yield value


def token_valid_for_portal(request: Request, raw: str, portal: str) -> bool:
    """JWT must cryptographically validate and belong to the requested portal."""
    try:
        payload = decode_access_token(raw)
    except JWTError:
        return False

    if request.cookies.get(portal_cookie_name(portal)) == raw:
        return True

    token_portal = payload.get("portal")
    if portal == "admin":
        return token_portal == "admin"
    return token_portal in (None, "pos")


def extract_bearer_token(
    request: Request,
    header_token: str | None,
    *,
    portal: str | None = None,
) -> str | None:
    """Return the first portal-valid JWT from header or portal cookies."""
    if portal in ("pos", "admin"):
        for raw in iter_auth_tokens(request, header_token, portal=portal):
            if token_valid_for_portal(request, raw, portal):
                return raw
        return None

    for raw in iter_auth_tokens(request, header_token, portal=None):
        for candidate in ("pos", "admin"):
            if token_valid_for_portal(request, raw, candidate):
                return raw
    return None


def portal_cookie_token(request: Request, portal: str) -> str | None:
    """Return JWT from the HttpOnly session cookie only — no Authorization header bypass."""
    raw = request.cookies.get(portal_cookie_name(portal))
    if not raw:
        return None
    if token_valid_for_portal(request, raw, portal):
        return raw
    return None


def set_session_cookie(response: Response, token: str, *, portal: str = "pos") -> None:
    """Session cookie — no max_age or expires; destroyed when the browser session ends."""
    name = portal_cookie_name(portal)
    response.set_cookie(
        key=name,
        value=token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response, *, portal: str | None = None) -> None:
    if portal == "admin":
        names = [settings.admin_session_cookie_name]
    elif portal == "pos":
        names = [settings.session_cookie_name]
    else:
        names = [settings.session_cookie_name, settings.admin_session_cookie_name]
    for name in names:
        response.delete_cookie(key=name, path="/")
