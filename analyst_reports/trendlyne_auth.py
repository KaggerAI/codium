"""
trendlyne_auth.py — Programmatic Trendlyne login + authenticated PDF download.

Trendlyne gates research-report PDFs behind a login. Linking the browser straight
at https://trendlyne.com/get-document/report/pdf/<id>/ therefore bounces the user
to the Trendlyne login page. Instead the server logs in with the credentials in
TRENDLYNE_USERNAME / TRENDLYNE_PASSWORD, keeps the resulting session cookie, and
proxies the PDF bytes back to the browser.

Login flow (verified against the live site):
  1. GET  https://trendlyne.com/visitor/loginmodal.html  -> csrfmiddlewaretoken
  2. POST https://trendlyne.com/accounts/login/          -> sets the `.trendlyne`
     session cookie. Fields: login / password / csrfmiddlewaretoken / remember.
     The recaptcha_token field is filled by JS in the browser but the server
     accepts an empty value, so no browser is needed.

Cookies harvested here are written back to trendlyne_cookies.json so the other
cookie-based consumers (pdf_summarizer_cookies, scrapling_fetcher) pick them up.
"""

import asyncio
import json
import os
import re
import sys
import threading
import time

LOGIN_MODAL_URL = "https://trendlyne.com/visitor/loginmodal.html"
LOGIN_POST_URL = "https://trendlyne.com/accounts/login/"
COOKIE_FILE = os.path.join(os.path.dirname(__file__), "trendlyne_cookies.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Some reports redirect off-site to the broker's own server; cap how long we
# wait so a dead host fails fast instead of hanging the request.
PDF_TIMEOUT = 25
LOGIN_TIMEOUT = 30

COOKIE_HELP = (
    "Refresh the Trendlyne session cookie: log into trendlyne.com in your browser, "
    "then copy the session cookie from DevTools -> Application -> Cookies into "
    "analyst_reports/trendlyne_cookies.json (or the TRENDLYNE_COOKIES env var)."
)

# Cached cookies + the time they were obtained. Trendlyne sessions are long
# lived; we only re-login when a download actually comes back unauthenticated.
_cached_cookies: dict | None = None
_last_login_at: float = 0.0
_login_lock = threading.Lock()


def _log(msg: str) -> None:
    print(f"TRENDLYNE_AUTH: {msg}", file=sys.stderr)


def _proxies() -> dict | None:
    """Residential proxy, if configured (Trendlyne blocks some cloud IPs)."""
    proxy_url = os.environ.get("RESIDENTIAL_PROXY_URL")
    return {"http": proxy_url, "https": proxy_url} if proxy_url else None


def _load_cookies_from_config() -> dict:
    """Cookies supplied out-of-band: TRENDLYNE_COOKIES env var, else JSON file."""
    env_cookies = os.getenv("TRENDLYNE_COOKIES")
    if env_cookies:
        try:
            return json.loads(env_cookies)
        except json.JSONDecodeError as e:
            _log(f"Failed to parse TRENDLYNE_COOKIES env var: {e}")

    if os.path.exists(COOKIE_FILE):
        try:
            with open(COOKIE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            _log(f"Failed to read {COOKIE_FILE}: {e}")
    return {}


def _persist_cookies(cookies: dict) -> None:
    """Write fresh cookies to disk so other cookie-based fetchers benefit too."""
    try:
        with open(COOKIE_FILE, "w") as f:
            json.dump(cookies, f, indent=2)
        _log(f"Saved {len(cookies)} cookies to trendlyne_cookies.json")
    except OSError as e:
        # Read-only filesystem (e.g. some Azure plans) is not fatal — the
        # in-memory cache still serves this process.
        _log(f"Could not persist cookies: {e}")


def _new_session():
    """A curl_cffi session that impersonates Chrome, falling back to httpx.

    Note: when curl_cffi is available we let it supply the User-Agent. Overriding
    it by hand produced a TLS-fingerprint/UA mismatch (chrome110 JA3 claiming to
    be Chrome 131), which is exactly the sort of inconsistency a WAF flags.
    """
    try:
        from curl_cffi import requests as cr
        return cr.Session(impersonate="chrome131", proxies=_proxies())
    except ImportError:
        import httpx
        proxy = os.environ.get("RESIDENTIAL_PROXY_URL")
        return httpx.Client(
            follow_redirects=True,
            timeout=60.0,
            proxy=proxy or None,
            headers={"User-Agent": USER_AGENT},
        )


def _headers(**extra) -> dict:
    """Request headers. Only set a User-Agent when curl_cffi isn't supplying a
    matching one for us — see the note in _new_session()."""
    headers = {}
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        headers["User-Agent"] = USER_AGENT
    headers.update(extra)
    return headers


def _login_sync() -> dict:
    """Log into Trendlyne and return the resulting cookie jar as a dict."""
    username = os.getenv("TRENDLYNE_USERNAME")
    password = os.getenv("TRENDLYNE_PASSWORD")
    if not username or not password:
        raise RuntimeError(
            "No Trendlyne password login available (TRENDLYNE_USERNAME / TRENDLYNE_PASSWORD "
            "are unset — expected if the account uses Google sign-in). "
            + COOKIE_HELP
        )

    session = _new_session()

    modal = session.get(
        LOGIN_MODAL_URL,
        headers=_headers(Referer="https://trendlyne.com/"),
        timeout=LOGIN_TIMEOUT,
    )
    if modal.status_code != 200:
        raise RuntimeError(f"Could not load Trendlyne login page (HTTP {modal.status_code})")

    csrf_match = re.search(
        r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', modal.text
    )
    if not csrf_match:
        raise RuntimeError("Could not find csrfmiddlewaretoken on the Trendlyne login page")
    csrf_token = csrf_match.group(1)

    resp = session.post(
        LOGIN_POST_URL,
        data={
            "csrfmiddlewaretoken": csrf_token,
            "login": username,
            "password": password,
            "remember": "on",
            "next": "/visitor/loginmodal.html",
            # Filled by reCAPTCHA v3 in the browser; the server accepts it empty.
            "recaptcha_token": "",
            "recaptcha_action": "login",
        },
        headers=_headers(
            Referer=LOGIN_MODAL_URL,
            Origin="https://trendlyne.com",
            **{"X-CSRFToken": csrf_token},
        ),
        timeout=LOGIN_TIMEOUT,
    )

    if "are not correct" in resp.text or "e-mail address and/or password" in resp.text:
        raise RuntimeError(
            "Trendlyne rejected the credentials — check TRENDLYNE_USERNAME / TRENDLYNE_PASSWORD."
        )

    cookies = {c.name: c.value for c in session.cookies.jar}
    if ".trendlyne" not in cookies:
        raise RuntimeError(
            f"Trendlyne login did not return a session cookie (HTTP {resp.status_code})"
        )

    _log(f"Logged in as {username} ({len(cookies)} cookies)")
    return cookies


def get_cookies(force_refresh: bool = False) -> dict:
    """
    Return usable Trendlyne cookies.

    Order: in-memory cache -> TRENDLYNE_COOKIES env / cookie file -> fresh login.
    `force_refresh=True` skips straight to a fresh login (used after a download
    comes back as the login page, i.e. the cached cookies expired).
    """
    global _cached_cookies, _last_login_at

    with _login_lock:
        if not force_refresh:
            if _cached_cookies:
                return _cached_cookies
            configured = _load_cookies_from_config()
            if configured:
                _cached_cookies = configured
                return configured
        else:
            # The cookie file / env var may have been refreshed by hand since we
            # cached it. Prefer that over burning a login attempt — it means a
            # re-exported cookie takes effect without restarting the server.
            configured = _load_cookies_from_config()
            if configured and configured != _cached_cookies:
                _log("Picked up refreshed cookies from config")
                _cached_cookies = configured
                return configured

            if _last_login_at and time.time() - _last_login_at < 30:
                # Another request just logged in; don't hammer the login endpoint.
                return _cached_cookies or {}

        cookies = _login_sync()
        _cached_cookies = cookies
        _last_login_at = time.time()
        _persist_cookies(cookies)
        return cookies


def _looks_like_pdf(content: bytes, content_type: str) -> bool:
    return content[:5] == b"%PDF-" or "pdf" in (content_type or "").lower()


def _fetch_pdf_sync(pdf_url: str, cookies: dict):
    """GET the PDF with the given cookies. Returns (content, content_type, final_url)."""
    session = _new_session()
    for name, value in cookies.items():
        try:
            session.cookies.set(name, str(value).strip(), domain=".trendlyne.com")
        except TypeError:  # httpx fallback has a simpler cookie API
            session.cookies.set(name, str(value).strip())

    try:
        resp = session.get(
            pdf_url,
            headers=_headers(
                Referer="https://trendlyne.com/",
                Accept="application/pdf,text/html;q=0.9,*/*;q=0.8",
            ),
            timeout=PDF_TIMEOUT,
        )
    except Exception as e:
        # Trendlyne 302s some reports to the broker's own host (e.g.
        # gcc.geojit.net for Geojit). Those hosts are frequently down, and a raw
        # curl error is meaningless to the user — name the host if we can find it.
        host_match = re.search(r'to ([A-Za-z0-9.\-]+\.[A-Za-z]{2,}) port', str(e))
        host = host_match.group(1) if host_match else "the broker's server"
        raise RuntimeError(
            f"This report is hosted off Trendlyne on {host}, which is not responding. "
            "Nothing to fix on our side — that broker's server is down or blocking us."
        ) from e

    return resp.content, resp.headers.get("content-type", ""), str(resp.url)


def download_pdf_sync(pdf_url: str) -> bytes:
    """
    Download a Trendlyne report PDF, logging in / re-logging in as needed.
    Raises RuntimeError with a human-readable message on failure.
    """
    cookies = get_cookies()
    had_cookies = bool(cookies)

    if cookies:
        content, content_type, final_url = _fetch_pdf_sync(pdf_url, cookies)
        if _looks_like_pdf(content, content_type):
            _log(f"Downloaded {len(content)} bytes from {pdf_url}")
            return content
        _log(f"Stored cookies rejected (ct={content_type}, url={final_url[:80]})")

    # Either we had no cookies at all, or they expired. A password login only
    # works for accounts that actually have a password (not Google sign-in), so
    # this may legitimately be unavailable — surface the cookie fix if so.
    try:
        cookies = get_cookies(force_refresh=True)
    except RuntimeError as login_err:
        if had_cookies:
            raise RuntimeError(
                f"The stored Trendlyne session cookie is no longer valid. {COOKIE_HELP}"
            ) from login_err
        raise

    content, content_type, final_url = _fetch_pdf_sync(pdf_url, cookies)
    if _looks_like_pdf(content, content_type):
        _log(f"Downloaded {len(content)} bytes from {pdf_url} after re-login")
        return content

    if "login" in final_url.lower():
        raise RuntimeError(f"Trendlyne still redirects to login. {COOKIE_HELP}")
    raise RuntimeError(f"Expected a PDF from Trendlyne but got content-type '{content_type}'.")


async def download_pdf(pdf_url: str) -> bytes:
    """Async wrapper around download_pdf_sync."""
    return await asyncio.to_thread(download_pdf_sync, pdf_url)


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://trendlyne.com/get-document/report/pdf/91363/"
    try:
        data = download_pdf_sync(url)
        print(f"OK: {len(data)} bytes, header={data[:8]!r}")
    except Exception as exc:
        print(f"FAILED: {exc}")
