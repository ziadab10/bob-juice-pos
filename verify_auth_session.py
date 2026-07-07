"""Verify session-cookie auth: no Max-Age/Expires, middleware redirects without cookie."""

from __future__ import annotations

import asyncio
import inspect
import sys

from fastapi import Response
from httpx import ASGITransport, AsyncClient

from auth import PROTECTED_HTML_ROUTES, login_redirect_url
from main import app
from session_auth import set_session_cookie


def test_set_session_cookie_has_no_expiry() -> None:
    """set_session_cookie must not pass max_age or expires to Starlette."""
    response = Response()
    set_session_cookie(response, "test-token", portal="admin")
    raw_header = response.headers.get("set-cookie", "")
    lower = raw_header.lower()
    assert "max-age" not in lower, f"Set-Cookie must not contain Max-Age: {raw_header}"
    assert "expires=" not in lower, f"Set-Cookie must not contain Expires: {raw_header}"
    assert "httponly" in lower, f"Set-Cookie must be HttpOnly: {raw_header}"


def test_set_session_cookie_source_has_no_expiry_kwargs() -> None:
    """Static check: set_cookie call omits max_age/expires kwargs."""
    import ast

    source = inspect.getsource(set_session_cookie)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "set_cookie":
            kw_names = {kw.arg for kw in node.keywords if kw.arg}
            assert "max_age" not in kw_names
            assert "expires" not in kw_names
            return
    raise AssertionError("set_cookie call not found in set_session_cookie")


async def test_login_set_cookie_is_session_only() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/auth/login",
            data={"username": "admin", "password": "Admin@Bob2026!"},
            headers={"X-Bob-Portal": "admin"},
        )
        assert res.status_code == 200, res.text
        cookie_header = res.headers.get("set-cookie", "")
        lower = cookie_header.lower()
        assert "max-age" not in lower, f"Login cookie must be session-only: {cookie_header}"
        assert "expires=" not in lower, f"Login cookie must be session-only: {cookie_header}"


async def test_middleware_redirects_without_cookie() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        for path, portal in PROTECTED_HTML_ROUTES.items():
            res = await client.get(path)
            assert res.status_code == 302, f"{path} should redirect (got {res.status_code})"
            location = res.headers.get("location", "")
            expected = login_redirect_url(portal)
            assert location == expected, f"{path} should redirect to {expected}, got {location}"


async def test_session_endpoint_cookie_only() -> None:
    """Session probe rejects Authorization header when cookie is absent."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            data={"username": "admin", "password": "Admin@Bob2026!"},
            headers={"X-Bob-Portal": "admin"},
        )
        token = login.json()["access_token"]

        async with AsyncClient(transport=transport, base_url="http://test") as bare:
            no_cookie = await bare.get(
                "/api/auth/session?portal=admin",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert no_cookie.json()["authenticated"] is False

        with_cookie = await client.get("/api/auth/session?portal=admin")
        assert with_cookie.json()["authenticated"] is True


async def main() -> int:
    test_set_session_cookie_has_no_expiry()
    test_set_session_cookie_source_has_no_expiry_kwargs()
    await test_login_set_cookie_is_session_only()
    await test_middleware_redirects_without_cookie()
    await test_session_endpoint_cookie_only()
    print("All auth/session verification checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
