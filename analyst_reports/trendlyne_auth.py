"""
trendlyne_auth.py — Trendlyne account pool: login, session health, quota rotation,
proxy-aware requests and authenticated PDF download.

Trendlyne gates research-report PDFs behind a login, and each paid account may
download a limited number of reports per month (200). We hold a POOL of accounts and
spread downloads across them, automatically and with no admin input:

ACCOUNTS (env; numbered, in priority order)
    TRENDLYNE_USERNAME   / TRENDLYNE_PASSWORD     account #1
    TRENDLYNE_USERNAME_2 / TRENDLYNE_PASSWORD_2   account #2
    TRENDLYNE_USERNAME_3 / TRENDLYNE_PASSWORD_3   account #3   (up to _5)
  Optional cookie seeds: TRENDLYNE_COOKIES (#1, plus the legacy trendlyne_cookies.json),
  TRENDLYNE_COOKIES_2, TRENDLYNE_COOKIES_3 ... An admin can also paste a cookie per account.
  TRENDLYNE_MONTHLY_LIMIT (default 200) - downloads per account per calendar month.

ROTATION (download_pdf_sync)
  * Accounts are tried in order. An account is skipped while it is in a cool-down:
      - quota:   Trendlyne refused a download with a limit / quota / upgrade page, or our
                 own monthly counter reached TRENDLYNE_MONTHLY_LIMIT. Re-probed every
                 TRENDLYNE_QUOTA_RETRY_HOURS (12h) and from the 1st of the next month,
                 so a reset on Trendlyne's side is picked up without anyone acting.
      - expired: the session bounced to /accounts/login/ and a password re-login failed.
      - error:   an unrecognised non-PDF answer (1h cool-down; the snippet is kept for
                 the Admin page so the limit page's wording can be learnt).
  * An expired session is renewed by password login automatically, then the download
    is retried on the same account. Only if that fails does the pool move on.
  * Report PDFs are cached on disk by document id, so re-opening a report (company
    page link, agent re-run) never spends quota twice.

SHARED STATE: Redis (`trendlyne:acct:<id>`, `trendlyne:usage:<id>:<YYYY-MM>`) so every
worker / instance sees the same cookies, counters and cool-downs; in-memory fallback.

LOGIN FLOW
  1. GET  https://trendlyne.com/visitor/loginmodal.html  -> csrfmiddlewaretoken
  2. POST https://trendlyne.com/accounts/login/          -> `.trendlyne` session cookie
  The page carries a reCAPTCHA v3 field that is submitted empty. If Trendlyne starts
  enforcing it, password login fails with a clear error on the Admin page and the pool
  falls back to cookies (seeded or pasted) for that account. Failed logins are not
  retried for 6 hours so an account is never hammered into a lock-out.

NETWORK: direct first; on a WAF block (403/429/5xx/connection error - typical from
Azure) retried through RESIDENTIAL_PROXY_URL, then the proxy is used straight away for
30 minutes. TRENDLYNE_PROXY_MODE = fallback (default) | always | off.
"""

import asyncio
import datetime
import hashlib
import json
import os
import re
import sys
import threading
import time

LOGIN_MODAL_URL = "https://trendlyne.com/visitor/loginmodal.html"
LOGIN_POST_URL = "https://trendlyne.com/accounts/login/"
HEALTH_PAGE_URL = "https://trendlyne.com/research-reports/post/TCS/1372/"
COOKIE_FILE = os.path.join(os.path.dirname(__file__), "trendlyne_cookies.json")
PDF_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "temp_reports", "pdf_cache")
PDF_CACHE_MAX_FILES = 800

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Some reports redirect off-site to the broker's own server; cap how long we
# wait so a dead host fails fast instead of hanging the request.
PDF_TIMEOUT = 25
LOGIN_TIMEOUT = 30

MAX_ACCOUNTS = 5

# Trendlyne resets connections when several PDFs are pulled at once; downloads take
# seconds while extraction takes ~20s, so throttling only this step costs little.
_DOWNLOAD_SEM = threading.BoundedSemaphore(max(1, int(os.getenv("TRENDLYNE_MAX_PARALLEL_DOWNLOADS", "1") or 1)))
LOGIN_RETRY_SECONDS = 6 * 3600       # after a failed password login
ERROR_COOLDOWN_SECONDS = 3600        # after an unrecognised non-PDF answer

COOKIE_HELP = (
    "Set TRENDLYNE_USERNAME / TRENDLYNE_PASSWORD (and _2, _3 for more accounts) so the "
    "server logs in by itself, or paste a fresh `.trendlyne` session cookie for the "
    "account on the Admin page (Cache tab -> Trendlyne accounts)."
)


def _log(msg: str) -> None:
    line = f"TRENDLYNE_AUTH: {msg}"
    try:
        print(line, file=sys.stderr)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode(), file=sys.stderr)


def _monthly_limit() -> int:
    try:
        return max(1, int(os.getenv("TRENDLYNE_MONTHLY_LIMIT", "200")))
    except ValueError:
        return 200


def _quota_retry_seconds() -> int:
    try:
        return int(float(os.getenv("TRENDLYNE_QUOTA_RETRY_HOURS", "12")) * 3600)
    except ValueError:
        return 12 * 3600


def _now_ist() -> datetime.datetime:
    return datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)


def _month_key() -> str:
    return _now_ist().strftime("%Y-%m")


def _next_month_start_ts() -> float:
    n = _now_ist()
    first = datetime.datetime(n.year + (n.month == 12), 1 if n.month == 12 else n.month + 1, 1)
    return (first - datetime.timedelta(hours=5, minutes=30)).replace(tzinfo=datetime.timezone.utc).timestamp()


def _mask(username: str | None) -> str:
    if not username:
        return "(cookie only)"
    if "@" in username:
        name, dom = username.split("@", 1)
        return f"{name[:2]}***@{dom}"
    return username[:2] + "***"


# =====================================================================
# Redis (shared across workers) - optional
# =====================================================================

def _redis():
    try:
        import agents.base as ab
        return ab.AGENT_REDIS_CLIENT
    except Exception:
        return None


def _redis_get_json(key: str):
    r = _redis()
    if not r:
        return None
    try:
        raw = r.get(key)
        if raw:
            return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception as e:
        _log(f"Redis read failed for {key}: {e}")
    return None


def _redis_set_json(key: str, value) -> None:
    r = _redis()
    if not r:
        return
    try:
        r.set(key, json.dumps(value))
    except Exception as e:
        _log(f"Redis write failed for {key}: {e}")


# =====================================================================
# Accounts + per-account state
# =====================================================================

_state_lock = threading.Lock()
_state: dict[str, dict] = {}            # account id -> state (mirrors Redis)
_usage_mem: dict[str, int] = {}         # "<id>:<YYYY-MM>" -> count (no-Redis fallback)
_login_locks: dict[str, threading.Lock] = {}
_net: dict = {}                          # network-route facts (last block etc.)


def _clean_env(name: str) -> str | None:
    """Env value with surrounding whitespace / stray quotes removed (a half-quoted
    `TRENDLYNE_USERNAME=me@x.com"` in .env or app settings silently breaks the login)."""
    v = (os.getenv(name) or "").strip().strip('"').strip("'").strip()
    return v or None


def accounts() -> list[dict]:
    """Configured accounts in priority order: [{'id': '1', 'username': ..., 'password': ...}]."""
    out = []
    for i in range(1, MAX_ACCOUNTS + 1):
        sfx = "" if i == 1 else f"_{i}"
        user = _clean_env(f"TRENDLYNE_USERNAME{sfx}")
        pwd = _clean_env(f"TRENDLYNE_PASSWORD{sfx}")
        seed = os.getenv(f"TRENDLYNE_COOKIES{sfx}") or None
        has_state = bool(_get_state(str(i)).get("cookies"))
        legacy = i == 1 and (os.path.exists(COOKIE_FILE) or _redis_get_json("trendlyne:cookies"))
        if user or seed or has_state or legacy:
            out.append({"id": str(i), "username": user, "password": pwd, "seed_env": f"TRENDLYNE_COOKIES{sfx}"})
    return out


def _state_key(aid: str) -> str:
    return f"trendlyne:acct:{aid}"


def _get_state(aid: str) -> dict:
    shared = _redis_get_json(_state_key(aid))
    with _state_lock:
        cur = _state.setdefault(aid, {})
        if isinstance(shared, dict) and shared.get("updated_at", 0) > cur.get("updated_at", 0):
            _state[aid] = cur = shared
        return dict(cur)


def _update_state(aid: str, **fields) -> dict:
    _get_state(aid)
    with _state_lock:
        cur = _state.setdefault(aid, {})
        cur.update(fields)
        cur["updated_at"] = time.time()
        snapshot = dict(cur)
    _redis_set_json(_state_key(aid), snapshot)
    return snapshot


def _usage(aid: str) -> int:
    key = f"trendlyne:usage:{aid}:{_month_key()}"
    r = _redis()
    if r:
        try:
            v = r.get(key)
            return int(v) if v else 0
        except Exception:
            pass
    return _usage_mem.get(key, 0)


def _incr_usage(aid: str) -> int:
    key = f"trendlyne:usage:{aid}:{_month_key()}"
    r = _redis()
    if r:
        try:
            n = int(r.incr(key))
            r.expire(key, 40 * 86400)
            return n
        except Exception:
            pass
    _usage_mem[key] = _usage_mem.get(key, 0) + 1
    return _usage_mem[key]


def _seed_cookies(acct: dict) -> tuple[dict, str] | tuple[None, None]:
    """Out-of-band cookies for an account: env seed; for #1 also legacy Redis key / file."""
    env = os.getenv(acct["seed_env"])
    if env:
        try:
            return json.loads(env), "env"
        except json.JSONDecodeError as e:
            _log(f"Failed to parse {acct['seed_env']}: {e}")
    if acct["id"] == "1":
        legacy = _redis_get_json("trendlyne:cookies")
        if isinstance(legacy, dict) and legacy.get("cookies"):
            return legacy["cookies"], f"redis ({legacy.get('source', 'legacy')})"
        if os.path.exists(COOKIE_FILE):
            try:
                with open(COOKIE_FILE, "r") as f:
                    return json.load(f), "file"
            except (json.JSONDecodeError, OSError) as e:
                _log(f"Failed to read {COOKIE_FILE}: {e}")
    return None, None


def _account_cookies(acct: dict) -> dict:
    st = _get_state(acct["id"])
    if st.get("cookies"):
        return st["cookies"]
    seed, src = _seed_cookies(acct)
    if seed and ".trendlyne" in seed:
        _update_state(acct["id"], cookies=seed, cookies_source=src, cookies_saved_at=time.time())
        return seed
    return {}


def _save_account_cookies(acct: dict, cookies: dict, source: str) -> None:
    _update_state(acct["id"], cookies=cookies, cookies_source=source, cookies_saved_at=time.time(),
                  state="unknown", expired_until=0, last_error="")
    if acct["id"] == "1":
        # Legacy consumers (pdf_summarizer_cookies, scrapling_fetcher) read this file.
        try:
            with open(COOKIE_FILE, "w") as f:
                json.dump(cookies, f, indent=2)
        except OSError as e:
            _log(f"Could not persist cookies file: {e}")


def _login_lock(aid: str) -> threading.Lock:
    with _state_lock:
        return _login_locks.setdefault(aid, threading.Lock())


# =====================================================================
# Proxy-aware requests
# =====================================================================
_direct_blocked_until = 0.0
PROXY_STICKY_SECONDS = 30 * 60


def _proxies() -> dict | None:
    """Residential proxy, if configured (Trendlyne blocks some cloud IPs)."""
    proxy_url = os.environ.get("RESIDENTIAL_PROXY_URL")
    return {"http": proxy_url, "https": proxy_url} if proxy_url else None


def _proxy_mode() -> str:
    mode = (os.getenv("TRENDLYNE_PROXY_MODE") or "fallback").strip().lower()
    return mode if mode in ("fallback", "always", "off") else "fallback"


def _routes() -> list[bool]:
    """Order to try: False = direct, True = via proxy."""
    mode = _proxy_mode()
    proxy_ok = bool(_proxies()) and mode != "off"
    if mode == "always" and proxy_ok:
        return [True]
    if proxy_ok and time.time() < _direct_blocked_until:
        return [True, False]
    return [False, True] if proxy_ok else [False]


def _is_block(resp) -> bool:
    if resp is None:
        return True
    if resp.status_code in (403, 407, 429, 502, 503, 504):
        return True
    head = (resp.text[:3000] if "text" in (resp.headers.get("content-type") or "") else "")
    return any(s in head for s in ("Request blocked", "Access Denied", "cf-chl", "Generated by cloudfront"))


def record_blocked(context: str, error: str) -> None:
    _net.update(last_blocked_at=time.time(), last_blocked_context=context, last_blocked_error=error[:300])


def _new_session(use_proxy: bool = False):
    """A curl_cffi session that impersonates Chrome, falling back to httpx.

    Note: when curl_cffi is available we let it supply the User-Agent. Overriding
    it by hand produced a TLS-fingerprint/UA mismatch (chrome110 JA3 claiming to
    be Chrome 131), which is exactly the sort of inconsistency a WAF flags.
    """
    proxies = _proxies() if use_proxy else None
    try:
        from curl_cffi import requests as cr
        return cr.Session(impersonate="chrome131", proxies=proxies)
    except ImportError:
        import httpx
        return httpx.Client(
            follow_redirects=True,
            timeout=60.0,
            proxy=(proxies or {}).get("https"),
            headers={"User-Agent": USER_AGENT},
        )


def _headers(**extra) -> dict:
    """Only set a User-Agent when curl_cffi isn't supplying a matching one (see _new_session)."""
    headers = {}
    try:
        import curl_cffi  # noqa: F401
    except ImportError:
        headers["User-Agent"] = USER_AGENT
    headers.update(extra)
    return headers


def request(method: str, url: str, cookies: dict | None = None, context: str = "", **kwargs):
    """
    GET/POST to Trendlyne: direct first, proxy on block (see module docstring).
    Returns (response, via_proxy). Raises the last error if every route failed.
    """
    global _direct_blocked_until
    routes = _routes()
    last_err = None
    for via_proxy in routes:
        resp = None
        for attempt in range(4):
            session = _new_session(use_proxy=via_proxy)
            for name, value in (cookies or {}).items():
                try:
                    session.cookies.set(name, str(value).strip(), domain=".trendlyne.com")
                except TypeError:
                    session.cookies.set(name, str(value).strip())
            try:
                resp = session.request(method, url, **kwargs)
                break
            except Exception as e:
                last_err = e
                msg = str(e)
                # Connection resets / timeouts under load are transient: back off and retry the
                # same route. A DNS failure (broker-hosted PDF) is not - don't waste retries on it.
                transient = any(s in msg for s in ("(35)", "(56)", "(52)", "(28)", "(7)", "reset", "timed out", "Timeout"))
                if transient and "resolve host" not in msg and attempt < 3:
                    time.sleep(4 * (2 ** attempt))      # 4s, 8s, 16s
                    continue
                _log(f"{'proxy' if via_proxy else 'direct'} request failed ({context}): {msg[:160]}")
                if "resolve host" not in msg:
                    record_blocked(context, msg)
                break
        if resp is None:
            continue
        if _is_block(resp) and via_proxy is False and len(routes) > 1:
            _log(f"direct request blocked ({context}): HTTP {resp.status_code}; retrying via proxy")
            record_blocked(context, f"HTTP {resp.status_code}")
            _direct_blocked_until = time.time() + PROXY_STICKY_SECONDS
            continue
        _net["last_route"] = "proxy" if via_proxy else "direct"
        return resp, via_proxy
    if last_err is not None:
        raise last_err
    raise RuntimeError("Trendlyne request failed on every route")


# =====================================================================
# Login
# =====================================================================

def _login_sync(username: str | None = None, password: str | None = None) -> dict:
    """Log into Trendlyne and return the resulting cookie jar as a dict."""
    username = username or _clean_env("TRENDLYNE_USERNAME")
    password = password or _clean_env("TRENDLYNE_PASSWORD")
    if not username or not password:
        raise RuntimeError("No username / password configured for this Trendlyne account.")

    last_err = None
    # One session per route: the CSRF cookie from the GET must ride along on the POST.
    for via_proxy in _routes():
        session = _new_session(use_proxy=via_proxy)
        try:
            modal = session.get(LOGIN_MODAL_URL, headers=_headers(Referer="https://trendlyne.com/"),
                                timeout=LOGIN_TIMEOUT)
        except Exception as e:
            last_err = e
            record_blocked("login", str(e))
            continue
        if _is_block(modal):
            last_err = RuntimeError(f"Trendlyne login page blocked (HTTP {modal.status_code})")
            record_blocked("login", str(last_err))
            continue
        if modal.status_code != 200:
            raise RuntimeError(f"Could not load Trendlyne login page (HTTP {modal.status_code})")

        csrf_match = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', modal.text)
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
                "recaptcha_token": "",
                "recaptcha_action": "login",
            },
            headers=_headers(Referer=LOGIN_MODAL_URL, Origin="https://trendlyne.com",
                             **{"X-CSRFToken": csrf_token}),
            timeout=LOGIN_TIMEOUT,
        )
        if "are not correct" in resp.text or "e-mail address and/or password" in resp.text:
            raise RuntimeError(
                "Trendlyne rejected the login (wrong password, a Google-sign-in account with no "
                "password, or Trendlyne now enforcing its reCAPTCHA on the login form).")
        cookies = {c.name: c.value for c in session.cookies.jar}
        if ".trendlyne" not in cookies:
            raise RuntimeError(f"Trendlyne login did not return a session cookie (HTTP {resp.status_code})")
        _log(f"Logged in {_mask(username)} via {'proxy' if via_proxy else 'direct'}")
        return cookies

    raise RuntimeError(f"Could not reach the Trendlyne login page: {last_err}")


def _try_login(acct: dict, force: bool = False) -> dict | None:
    """Password login for one account, rate-limited. Returns cookies or None."""
    if not (acct.get("username") and acct.get("password")):
        return None
    aid = acct["id"]
    with _login_lock(aid):
        st = _get_state(aid)
        # Another worker may have just logged in.
        if not force and st.get("last_login_at", 0) > time.time() - 60 and st.get("cookies"):
            return st["cookies"]
        if not force and st.get("login_blocked_until", 0) > time.time():
            return None
        try:
            cookies = _login_sync(acct["username"], acct["password"])
        except Exception as e:
            _update_state(aid, last_login_error=str(e)[:300], last_login_attempt_at=time.time(),
                          login_blocked_until=time.time() + LOGIN_RETRY_SECONDS)
            _log(f"Account #{aid} login failed: {str(e)[:160]}")
            return None
        _save_account_cookies(acct, cookies, "login")
        _update_state(aid, last_login_at=time.time(), last_login_error="", login_blocked_until=0,
                      last_login_attempt_at=time.time())
        return cookies


# =====================================================================
# PDF cache (saves quota)
# =====================================================================

def _pdf_cache_path(pdf_url: str) -> str:
    m = re.search(r"/pdf/(\d+)/?", pdf_url or "")
    name = m.group(1) if m else hashlib.sha1(pdf_url.encode()).hexdigest()[:20]
    return os.path.join(PDF_CACHE_DIR, f"{name}.pdf")


def _pdf_cache_get(pdf_url: str) -> bytes | None:
    p = _pdf_cache_path(pdf_url)
    try:
        if os.path.exists(p):
            with open(p, "rb") as f:
                data = f.read()
            if data[:5] == b"%PDF-":
                os.utime(p, None)
                return data
    except OSError:
        pass
    return None


def _pdf_cache_put(pdf_url: str, data: bytes) -> None:
    try:
        os.makedirs(PDF_CACHE_DIR, exist_ok=True)
        with open(_pdf_cache_path(pdf_url), "wb") as f:
            f.write(data)
        files = [os.path.join(PDF_CACHE_DIR, x) for x in os.listdir(PDF_CACHE_DIR)]
        if len(files) > PDF_CACHE_MAX_FILES:
            for old in sorted(files, key=os.path.getmtime)[: len(files) - PDF_CACHE_MAX_FILES]:
                os.remove(old)
    except OSError as e:
        _log(f"PDF cache write failed: {e}")


# =====================================================================
# Response classification
# =====================================================================

def _looks_like_pdf(content: bytes, content_type: str) -> bool:
    return content[:5] == b"%PDF-" or "pdf" in (content_type or "").lower()


def _bounced_to_login(final_url: str) -> bool:
    return "/accounts/login" in (final_url or "").lower()


_QUOTA_RE = re.compile(
    r"(download limit|limit (?:has been |is )?(?:reached|exceeded|exhausted)|reached (?:your|the) "
    r"(?:monthly |daily )?(?:limit|quota)|quota|exceeded|maximum (?:number of )?(?:reports|downloads)|"
    r"upgrade (?:your )?(?:plan|subscription)|no (?:more )?downloads? (?:left|remaining))", re.I)


def _visible_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html or "")
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def _classify(content: bytes, content_type: str, final_url: str, status: int) -> tuple[str, str]:
    """-> (kind, detail): pdf | login | quota | not_found | unknown"""
    if _looks_like_pdf(content, content_type):
        return "pdf", ""
    if _bounced_to_login(final_url):
        return "login", "redirected to the login page"
    if status == 404:
        return "not_found", "Trendlyne has no file for this report (HTTP 404)"
    url_l = (final_url or "").lower()
    if any(s in url_l for s in ("/subscribe", "/pricing", "/plans", "/upgrade", "limit")):
        return "quota", f"redirected to {final_url[:100]}"
    try:
        text = _visible_text(content.decode("utf-8", "replace"))
    except Exception:
        text = ""
    m = _QUOTA_RE.search(text)
    if m:
        start = max(0, m.start() - 80)
        return "quota", text[start:m.end() + 120]
    return "unknown", f"HTTP {status}, {content_type or 'no content-type'}: {text[:200]}"


def _fetch_pdf_sync(pdf_url: str, cookies: dict):
    """GET the PDF with the given cookies. Returns (content, content_type, final_url, via_proxy, status)."""
    try:
        resp, via_proxy = request(
            "GET", pdf_url, cookies=cookies, context="pdf",
            headers=_headers(Referer="https://trendlyne.com/",
                             Accept="application/pdf,text/html;q=0.9,*/*;q=0.8"),
            timeout=PDF_TIMEOUT,
        )
    except Exception as e:
        # Trendlyne 302s some reports to the broker's own host (e.g. gcc.geojit.net for
        # Geojit). Those hosts are often down; that is not an account problem.
        host_match = (re.search(r'resolve host: ([A-Za-z0-9.\-]+\.[A-Za-z]{2,})', str(e))
                      or re.search(r'to ([A-Za-z0-9.\-]+\.[A-Za-z]{2,}) port', str(e)))
        if host_match and "trendlyne" not in host_match.group(1):
            raise BrokerHostError(
                f"This report is hosted off Trendlyne on {host_match.group(1)}, which is not responding. "
                "Nothing to fix on our side — that broker's server is down or blocking us."
            ) from e
        raise RuntimeError(f"Could not reach Trendlyne for this report: {str(e)[:200]}") from e
    return resp.content, resp.headers.get("content-type", ""), str(resp.url), via_proxy, resp.status_code


class BrokerHostError(RuntimeError):
    pass


# =====================================================================
# Account selection + download
# =====================================================================

def _availability(acct: dict, now: float | None = None) -> tuple[bool, str]:
    """(usable now?, reason if not)."""
    now = now or time.time()
    st = _get_state(acct["id"])
    if st.get("quota_until", 0) > now:
        return False, "quota"
    if st.get("error_until", 0) > now:
        return False, "error"
    if st.get("state") == "expired" and st.get("expired_until", 0) > now:
        return False, "expired"
    if not _account_cookies(acct) and not (acct.get("username") and acct.get("password")):
        return False, "no_credentials"
    return True, ""


def _candidates() -> list[dict]:
    """Accounts to try, in order. Our own monthly counter is a soft limit: an account past
    it is only tried after every account under it (Trendlyne itself is the authority)."""
    under, over = [], []
    limit = _monthly_limit()
    for a in accounts():
        ok, _ = _availability(a)
        if ok:
            (over if _usage(a["id"]) >= limit else under).append(a)
    return under + over


def download_pdf_sync(pdf_url: str) -> bytes:
    """
    Download a Trendlyne report PDF from the account pool (see module docstring).
    Raises RuntimeError with a human-readable message if no account can serve it.
    """
    cached = _pdf_cache_get(pdf_url)
    if cached:
        return cached

    pool = accounts()
    if not pool:
        raise RuntimeError(f"No Trendlyne account is configured. {COOKIE_HELP}")

    tried: list[str] = []
    unknown_hits: list[tuple[str, str]] = []
    for acct in _candidates():
        aid = acct["id"]
        cookies = _account_cookies(acct) or _try_login(acct) or {}
        if not cookies:
            tried.append(f"#{aid}: no session and login unavailable")
            continue

        for attempt in (1, 2):
            with _DOWNLOAD_SEM:
                content, ct, final_url, via_proxy, status = _fetch_pdf_sync(pdf_url, cookies)
            kind, detail = _classify(content, ct, final_url, status)

            if kind == "pdf":
                # Accounts that answered oddly while this one worked are the problem: cool them down.
                for bad_id, bad_detail in unknown_hits:
                    _update_state(bad_id, error_until=time.time() + ERROR_COOLDOWN_SECONDS,
                                  last_error=f"unrecognised response: {bad_detail[:250]}")
                used = _incr_usage(aid)
                _update_state(aid, state="valid", last_ok_at=time.time(), last_route="proxy" if via_proxy else "direct",
                              quota_until=0, error_until=0, expired_until=0, last_error="")
                _log(f"Downloaded {len(content)} bytes via account #{aid} ({used}/{_monthly_limit()} this month)")
                if used >= _monthly_limit():
                    _update_state(aid, quota_until=min(_next_month_start_ts(), time.time() + _quota_retry_seconds()),
                                  quota_reason=f"reached our monthly counter ({used})")
                _pdf_cache_put(pdf_url, content)
                return content

            if kind == "login" and attempt == 1:
                _update_state(aid, state="expired", last_expired_at=time.time(), last_error=detail)
                fresh = _try_login(acct)
                if fresh:
                    cookies = fresh
                    continue            # retry the same account with the new session
                _update_state(aid, expired_until=time.time() + LOGIN_RETRY_SECONDS)
                tried.append(f"#{aid}: session expired and re-login failed")
                break

            if kind == "login":
                _update_state(aid, state="expired", last_expired_at=time.time(),
                              expired_until=time.time() + LOGIN_RETRY_SECONDS,
                              last_error="still redirected to login after a fresh login")
                tried.append(f"#{aid}: session expired")
                break

            if kind == "quota":
                until = min(_next_month_start_ts(), time.time() + _quota_retry_seconds())
                _update_state(aid, quota_until=until, quota_reason=detail[:300], last_quota_at=time.time())
                _log(f"Account #{aid} hit its download limit; moving to the next account")
                tried.append(f"#{aid}: download limit reached")
                break

            if kind == "not_found":
                raise RuntimeError(detail)

            # unknown non-PDF answer: try the next account for this request
            unknown_hits.append((aid, detail))
            tried.append(f"#{aid}: unexpected response")
            break

    # Every account that reached Trendlyne gave an unrecognised answer -> it's this report,
    # not the accounts: no cool-downs, just report it.
    if unknown_hits and len(unknown_hits) == len(tried):
        raise RuntimeError(f"Trendlyne did not return a PDF for this report ({unknown_hits[0][1][:160]}).")

    not_tried = [f"#{a['id']}: {_availability(a)[1]}" for a in pool if a["id"] not in {t.split(':')[0][1:] for t in tried}]
    raise RuntimeError("No Trendlyne account could download this report — "
                       + "; ".join(tried + not_tried) + ". " + COOKIE_HELP)


async def download_pdf(pdf_url: str) -> bytes:
    """Async wrapper around download_pdf_sync."""
    return await asyncio.to_thread(download_pdf_sync, pdf_url)


# =====================================================================
# Cookies for non-PDF requests, admin actions, status
# =====================================================================

def active_account() -> dict | None:
    c = _candidates()
    return c[0] if c else None


def get_cookies(force_refresh: bool = False) -> dict:
    """Cookies of the account currently first in line (used for page fetches)."""
    acct = active_account()
    if not acct:
        return {}
    if force_refresh:
        return _try_login(acct, force=True) or _account_cookies(acct)
    return _account_cookies(acct) or _try_login(acct) or {}


def record_page_login_state(logged_in: bool | None, context: str = "reports page") -> None:
    """Report pages are public, so a logged-out page only marks the session, it never blocks."""
    acct = active_account()
    if not acct or logged_in is None:
        return
    if logged_in:
        _update_state(acct["id"], state="valid", last_ok_at=time.time(), last_ok_context=context)
    else:
        _update_state(acct["id"], state="expired", last_expired_at=time.time(),
                      last_error=f"{context} rendered logged-out")


# Back-compat names used elsewhere
def record_session_ok(context: str, via_proxy: bool = False) -> None:
    record_page_login_state(True, context)


def record_session_expired(context: str, error: str = "") -> None:
    record_page_login_state(False, context)


def page_is_logged_in(html: str) -> bool | None:
    """
    Trendlyne pages render for anonymous visitors too; a logged-out page wires the
    login modal into ~20+ places, a logged-in one into ~2. None = can't tell.
    """
    if not html or ("panel-post" not in html and "trendlyne" not in html.lower()):
        return None
    return html.count("loginmodal") <= 6


def parse_cookie_input(text: str) -> dict:
    """Accept a JSON object, a raw `Cookie:` header ("a=1; b=2"), or just the `.trendlyne` value."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        data = json.loads(text)
        return {str(k): str(v).strip() for k, v in data.items() if v}
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]
    if "=" in text:
        out = {}
        for part in text.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip():
                    out[k.strip()] = v.strip()
        return out
    return {".trendlyne": text}


def _account(aid: str) -> dict:
    for a in accounts():
        if a["id"] == str(aid):
            return a
    sfx = "" if str(aid) == "1" else f"_{aid}"
    return {"id": str(aid), "username": None, "password": None, "seed_env": f"TRENDLYNE_COOKIES{sfx}"}


def set_cookies_manually(cookies: dict, account_id: str = "1") -> None:
    """Admin-pasted cookies for one account: effective on every worker immediately."""
    if ".trendlyne" not in cookies:
        raise ValueError("The `.trendlyne` session cookie is required.")
    _save_account_cookies(_account(account_id), dict(cookies), "admin")


def force_login(account_id: str = "1") -> dict:
    acct = _account(account_id)
    if not (acct.get("username") and acct.get("password")):
        raise RuntimeError(f"Account #{account_id} has no username / password configured.")
    cookies = _try_login(acct, force=True)
    if not cookies:
        raise RuntimeError(_get_state(acct["id"]).get("last_login_error") or "Login failed")
    return cookies


def reset_cooldowns(account_id: str) -> None:
    _update_state(str(account_id), quota_until=0, error_until=0, expired_until=0, login_blocked_until=0)


def check_session(account_id: str | None = None) -> dict:
    """
    Live check of one account (default: the active one) WITHOUT spending a download:
    loads a public report page with the account's cookies and reads the logged-in marker.
    """
    acct = _account(account_id) if account_id else active_account()
    result = {"checked_at": time.time()}
    if not acct:
        result["error"] = "No usable Trendlyne account"
        result["status"] = get_status()
        return result
    result["account_id"] = acct["id"]
    cookies = _account_cookies(acct)
    result["has_cookies"] = bool(cookies)
    try:
        resp, via_proxy = request("GET", HEALTH_PAGE_URL, cookies=cookies, context="health",
                                  headers=_headers(Referer="https://trendlyne.com/"), timeout=30)
        result["page_http"] = resp.status_code
        result["route"] = "proxy" if via_proxy else "direct"
        logged_in = page_is_logged_in(resp.text) if cookies else False
        result["page_logged_in"] = logged_in
        if logged_in:
            _update_state(acct["id"], state="valid", last_ok_at=time.time(), last_ok_context="health check",
                          expired_until=0)
        elif logged_in is False:
            fresh = _try_login(acct)
            result["relogin_attempted"] = bool(acct.get("username"))
            if fresh:
                result["page_logged_in"] = True
                _update_state(acct["id"], state="valid", last_ok_at=time.time(), last_ok_context="health check (after login)")
            else:
                _update_state(acct["id"], state="expired", last_expired_at=time.time(),
                              last_error="health check: logged-out page")
    except Exception as e:
        result["error"] = str(e)[:300]
    result["status"] = get_status()
    return result


def get_status() -> dict:
    """Pool health for the Admin page / agent warnings."""
    now = time.time()
    limit = _monthly_limit()
    rows = []
    for a in accounts():
        st = _get_state(a["id"])
        ok, why = _availability(a, now)
        state = st.get("state", "unknown")
        if st.get("quota_until", 0) > now:
            state = "quota"
        elif st.get("error_until", 0) > now:
            state = "error"
        rows.append({
            "id": a["id"],
            "username": _mask(a.get("username")),
            "has_password": bool(a.get("username") and a.get("password")),
            "state": state,
            "usable": ok,
            "unusable_reason": why,
            "used_this_month": _usage(a["id"]),
            "monthly_limit": limit,
            "quota_until": st.get("quota_until") if st.get("quota_until", 0) > now else None,
            "quota_reason": st.get("quota_reason") if st.get("quota_until", 0) > now else None,
            "last_ok_at": st.get("last_ok_at"),
            "last_expired_at": st.get("last_expired_at"),
            "last_error": st.get("last_error"),
            "cookies_source": st.get("cookies_source"),
            "cookies_saved_at": st.get("cookies_saved_at"),
            "last_login_at": st.get("last_login_at"),
            "last_login_error": st.get("last_login_error"),
            "login_blocked_until": st.get("login_blocked_until") if st.get("login_blocked_until", 0) > now else None,
        })
    active = active_account()
    n_usable = sum(1 for r in rows if r["usable"])
    overall = "valid" if n_usable == len(rows) and rows else ("degraded" if n_usable else "down")
    return {
        "state": overall,
        "accounts": rows,
        "active_account": active["id"] if active else None,
        "month": _month_key(),
        "total_used_this_month": sum(r["used_this_month"] for r in rows),
        "total_monthly_limit": limit * len(rows),
        "proxy_configured": bool(_proxies()),
        "proxy_mode": _proxy_mode(),
        "proxy_sticky_until": _direct_blocked_until if _direct_blocked_until > now else None,
        "last_route": _net.get("last_route"),
        "last_blocked_at": _net.get("last_blocked_at"),
        "last_blocked_error": _net.get("last_blocked_error"),
    }


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://trendlyne.com/get-document/report/pdf/91363/"
    try:
        data = download_pdf_sync(url)
        print(f"OK: {len(data)} bytes, header={data[:8]!r}")
    except Exception as exc:
        print(f"FAILED: {exc}")
    print(json.dumps(get_status(), indent=2, default=str))
