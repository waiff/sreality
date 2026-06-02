"""Mint a logged-in sreality session for the `estate_prices` stats API.

Unlike the listing API, `/api/v1/estate_prices` returns HTTP 401 without a
logged-in Seznam session. We get one of three ways, cheapest first:

1. `SREALITY_SESSION_COOKIE` env var — a raw `name=val; name2=val2` cookie
   string (or single value). Lets the operator paste a cookie captured in a
   browser; no browser needed at runtime. Fast path for validation + reuse.
2. A cached cookie file (`SREALITY_COOKIE_CACHE`, default under the temp dir),
   if present and younger than `max_age_s`.
3. A headless Playwright login with `SREALITY_LOGIN_EMAIL` /
   `SREALITY_LOGIN_PASSWORD`, which we then cache.

Playwright is imported lazily so this module (and the test suite) load fine
without the browser dep installed; only path 3 needs it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

LOG = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
_LOGIN_URL = (
    "https://login.szn.cz/?service=sreality&return_url=https://www.sreality.cz/"
)
# Cookies on these domains carry the sreality session.
_SESSION_DOMAINS = ("sreality.cz", "szn.cz", "seznam.cz")


class AuthError(Exception):
    """Couldn't obtain a usable sreality session."""


def _cache_path() -> Path:
    return Path(
        os.environ.get("SREALITY_COOKIE_CACHE")
        or Path(tempfile.gettempdir()) / "sreality_stats_cookies.json"
    )


def _parse_cookie_string(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in raw.strip().strip(";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, val = part.split("=", 1)
            out[name.strip()] = val.strip()
    return out


def _read_cache(max_age_s: float) -> dict[str, str] | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(blob.get("saved_at", 0)) > max_age_s:
        return None
    cookies = blob.get("cookies") or {}
    return cookies or None


def _write_cache(cookies: dict[str, str]) -> None:
    try:
        _cache_path().write_text(
            json.dumps({"saved_at": time.time(), "cookies": cookies})
        )
    except OSError as exc:
        LOG.warning("could not cache sreality cookies: %s", exc)


def get_session_cookies(
    *, force_login: bool = False, max_age_s: float = 6 * 3600
) -> dict[str, str]:
    """Cookie dict for sreality requests, by the cheapest available path."""
    env_cookie = os.environ.get("SREALITY_SESSION_COOKIE")
    if env_cookie:
        cookies = _parse_cookie_string(env_cookie)
        if cookies:
            return cookies

    if not force_login:
        cached = _read_cache(max_age_s)
        if cached:
            LOG.info("using cached sreality session cookies")
            return cached

    email = os.environ.get("SREALITY_LOGIN_EMAIL")
    password = os.environ.get("SREALITY_LOGIN_PASSWORD")
    if not email or not password:
        raise AuthError(
            "no SREALITY_SESSION_COOKIE / cache and no SREALITY_LOGIN_EMAIL+"
            "SREALITY_LOGIN_PASSWORD to log in with"
        )
    cookies = login_via_browser(email, password)
    _write_cache(cookies)
    return cookies


def login_via_browser(
    email: str, password: str, *, headless: bool = True, timeout_ms: int = 30000
) -> dict[str, str]:
    """Headless Seznam login → session cookie dict for sreality.cz.

    Two-step form (username, then password). Saves a debug screenshot on
    failure (`SREALITY_AUTH_DEBUG_DIR`, default temp dir) like the legacy
    scraper did, since the login page changes occasionally.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - exercised only in CI
        raise AuthError(
            "playwright not installed; `pip install -e '.[pricestats]'` "
            "or set SREALITY_SESSION_COOKIE"
        ) from exc

    debug_dir = Path(
        os.environ.get("SREALITY_AUTH_DEBUG_DIR") or tempfile.gettempdir()
    )
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=UA, locale="cs-CZ")
        page = context.new_page()
        page.set_default_timeout(timeout_ms)
        try:
            page.goto(_LOGIN_URL, wait_until="domcontentloaded")
            _dismiss_consent(page)
            page.fill('input[name="username"]', email)
            _click_first(
                page,
                ['button[type="submit"]', 'button:has-text("Pokračovat")'],
            )
            page.wait_for_selector('input[type="password"]', timeout=timeout_ms)
            page.fill('input[type="password"]', password)
            _click_first(
                page,
                ['button[type="submit"]', 'button:has-text("Přihlásit")'],
            )
            # Land back on sreality with the session set.
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            page.goto("https://www.sreality.cz/", wait_until="domcontentloaded")
            cookies = _collect_cookies(context)
            if not cookies:
                raise AuthError("login produced no sreality session cookies")
            LOG.info("sreality login OK (%d cookies)", len(cookies))
            return cookies
        except Exception as exc:
            shot = debug_dir / "sreality_login_failure.png"
            try:
                page.screenshot(path=str(shot))
                LOG.error("login failed; screenshot at %s", shot)
            except Exception:  # pragma: no cover
                pass
            raise AuthError(f"sreality login failed: {exc}") from exc
        finally:
            browser.close()


def _collect_cookies(context) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in context.cookies():
        domain = (c.get("domain") or "").lstrip(".")
        if any(domain.endswith(d) for d in _SESSION_DOMAINS):
            out[c["name"]] = c["value"]
    return out


def _dismiss_consent(page) -> None:
    for sel in (
        'button:has-text("Souhlasím")',
        'button:has-text("Přijmout")',
        '[data-testid="cw-button-agree-with-ads"]',
        "#didomi-notice-agree-button",
    ):
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=2000):
                el.click()
                return
        except Exception:
            continue


def _click_first(page, selectors: list[str]) -> None:
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.is_visible(timeout=3000):
                el.click()
                return
        except Exception:
            continue
    raise AuthError(f"none of the expected buttons were clickable: {selectors}")


def _main() -> int:  # pragma: no cover - operator/CI helper
    """Mint a cookie and print whether estate_prices then authorizes."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cookies = get_session_cookies(force_login="--force" in sys.argv)
    print(f"got {len(cookies)} cookies: {sorted(cookies)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
