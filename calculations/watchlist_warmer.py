"""
watchlist_warmer.py - Keeps the four Watchlist panels (Financials, Concall,
Forensic, Analyst) pre-computed so they render instantly on click.

Freshness is QUARTER-BASED, not time-based. Work happens only when:
  * a ticker is added to a watchlist for the first time  -> warm all four now
  * the company announces a new quarter                  -> re-warm all four
  * a scheduled concall finishes                         -> re-warm concall
  * the user forces a refresh from a panel               -> re-warm that one

In steady state a cycle costs one Screener page fetch per watchlist ticker and
zero LLM calls.

Design notes
------------
* No Flask import. Everything handler.py owns is injected via configure().
* Payload caches are NOT reimplemented. Each pipeline already persists its own
  result (`store_latest_result` -> `agent_result_{type}_{TICKER}`, and /analyze
  -> `stock_analysis_{TICKER}`), both on a quarter-boundary TTL. This module
  only tracks *state* in `wl_warm:{TICKER}`.
* Financials is warmed through the real /analyze endpoint. That is deliberate:
  `get_analysis_for_ticker` and `run_batch_precache` both stamp
  `light_cache: True`, and /analyze re-fetches TradingView + yfinance +
  Trendlyne and rebuilds every chart on a light payload (handler.py:10029),
  keeping it light forever (handler.py:10345). Only the full /analyze path
  writes a payload that later returns via FULL CACHE HIT.
"""

import csv
import json
import os
import queue
import sys
import threading
import time
import traceback
import asyncio
from datetime import datetime, timedelta

# -- Agents, in warm order --------------------------------------------------
# financials first: forensic reads the base data it produces, so ordering this
# way keeps forensic off its synchronous 40-60s fallback path.
# concall last: slowest and flakiest, so a failure there cannot block the rest.
WARM_AGENTS = ("financials", "forensic", "analyst", "concall")

# Forensic audits the financial statements the financials step caches, so it must
# never run before that succeeded. Enforced in warm_one(), not just implied by
# the order above -- a panel refresh or a gap-fill can request forensic alone.
AGENT_DEPENDS_ON = {"forensic": "financials"}

# -- Tunables --------------------------------------------------------------
WARM_CYCLE_TIMES_IST = ("23:00", "15:00")  # quarter probe + concall sweep
MAX_WARM_PER_CYCLE = 25          # bounded burst; backlog drains next cycle
CONCALL_RETRY_DAYS = 14          # keep retrying while no transcript exists
CONCALL_DURATION_MINUTES = 90    # assumed call length before we re-warm
CONCALL_LOOKBACK_HOURS = 48      # ignore calls older than this on restart
FINANCIALS_WARM_ATTEMPTS = 2     # price-history fetch is flaky; retry once
FINANCIALS_RETRY_GAP_SECONDS = 30
QUARTER_PROBE_GAP_SECONDS = 1.5  # Screener.in politeness
TICKER_GAP_SECONDS = 5           # pause between tickers in the worker
REGISTRY_TTL_SECONDS = 60 * 86400
TICKER_LOCK_TTL_SECONDS = 3600
STATES = ("none", "warming", "ready", "stale", "failed", "awaiting_transcript")

# -- Injected dependencies (set by configure() from handler.py) -------------
_deps = {}
_configured = False


def configure(**kwargs):
    """Wire in handler.py's helpers. Safe to call more than once."""
    global _configured
    _deps.update({k: v for k, v in kwargs.items() if v is not None})
    _configured = True
    print(
        "WL_WARMER: configured with -> "
        + ", ".join(sorted(k for k, v in _deps.items() if v is not None)),
        file=sys.stderr,
    )


def _dep(name):
    return _deps.get(name)


def _redis():
    return _deps.get("redis_client")


# =====================================================================
# WARM STATE REGISTRY  ->  wl_warm:{TICKER}
# =====================================================================
_LOCAL_REGISTRY = {}
_REGISTRY_LOCK = threading.Lock()


def _registry_key(ticker):
    return f"wl_warm:{ticker.upper()}"


def _blank_agent_state():
    return {
        "state": "none",
        "quarter": None,
        "warmed_at": None,
        "attempts": 0,
        "last_error": None,
        "first_attempt_at": None,
    }


def _blank_registry(ticker):
    return {
        "ticker": ticker.upper(),
        "quarter": None,
        "quarter_checked_at": None,
        "agents": {a: _blank_agent_state() for a in WARM_AGENTS},
        "concall_calls_done": [],
    }


def registry_get(ticker):
    """Read the warm registry for a ticker (Redis, falling back to memory)."""
    ticker = ticker.upper()
    key = _registry_key(ticker)
    r = _redis()
    if r:
        try:
            raw = r.get(key)
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                reg = json.loads(raw)
                # Tolerate registries written by an older build.
                reg.setdefault("agents", {})
                for a in WARM_AGENTS:
                    reg["agents"].setdefault(a, _blank_agent_state())
                reg.setdefault("concall_calls_done", [])
                return reg
        except Exception as e:
            print(f"WL_WARMER: registry read failed for {ticker}: {e}", file=sys.stderr)

    with _REGISTRY_LOCK:
        return json.loads(json.dumps(_LOCAL_REGISTRY.get(key) or _blank_registry(ticker)))


def registry_save(ticker, reg):
    key = _registry_key(ticker)
    with _REGISTRY_LOCK:
        _LOCAL_REGISTRY[key] = reg
    r = _redis()
    if r:
        try:
            r.setex(key, REGISTRY_TTL_SECONDS, json.dumps(reg))
        except Exception as e:
            print(f"WL_WARMER: registry write failed for {ticker}: {e}", file=sys.stderr)


def _set_agent_state(ticker, agent, state, quarter=None, error=None,
                     bump_attempts=False):
    """Update one agent's slot in the registry. Never raises."""
    try:
        reg = registry_get(ticker)
        slot = reg["agents"].setdefault(agent, _blank_agent_state())
        slot["state"] = state
        slot["last_error"] = error
        if quarter is not None:
            slot["quarter"] = quarter
        if state == "ready":
            slot["warmed_at"] = time.time()
            slot["attempts"] = 0
            slot["first_attempt_at"] = None
        if bump_attempts:
            slot["attempts"] = int(slot.get("attempts") or 0) + 1
            if not slot.get("first_attempt_at"):
                slot["first_attempt_at"] = time.time()
        registry_save(ticker, reg)
    except Exception as e:
        print(f"WL_WARMER: state update failed {ticker}/{agent}: {e}", file=sys.stderr)


# =====================================================================
# QUEUE + SINGLE WORKER
# =====================================================================
# Priority 0 = user-triggered (a stock was just added), 1 = scheduled.
_QUEUE = queue.PriorityQueue()
_QUEUE_SEQ = 0
_QUEUE_SEQ_LOCK = threading.Lock()
_INFLIGHT = set()
_INFLIGHT_LOCK = threading.Lock()
_WORKER_STARTED = False

# Serialises a single (ticker, agent) so a user refresh and a scheduled warm
# can never run the same pipeline twice concurrently.
_AGENT_LOCKS = {}
_AGENT_LOCKS_GUARD = threading.Lock()


def _agent_lock(ticker, agent):
    key = f"{ticker.upper()}:{agent}"
    with _AGENT_LOCKS_GUARD:
        lock = _AGENT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _AGENT_LOCKS[key] = lock
        return lock


def enqueue(ticker, agents=None, force=False, priority=1):
    """Queue a background warm. Returns True when it was actually queued."""
    global _QUEUE_SEQ
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return False
    agents = tuple(agents or WARM_AGENTS)

    with _INFLIGHT_LOCK:
        if ticker in _INFLIGHT:
            print(f"WL_WARMER: {ticker} already queued/running - skipping enqueue",
                  file=sys.stderr)
            return False
        _INFLIGHT.add(ticker)

    with _QUEUE_SEQ_LOCK:
        _QUEUE_SEQ += 1
        seq = _QUEUE_SEQ

    _QUEUE.put((priority, seq, ticker, agents, bool(force)))
    print(f"WL_WARMER: queued {ticker} agents={list(agents)} force={force} "
          f"priority={priority} (depth={_QUEUE.qsize()})", file=sys.stderr)
    return True


def queue_depth():
    return _QUEUE.qsize()


def _warm_worker():
    print("WL_WARMER: worker online.", file=sys.stderr)
    while True:
        ticker = None
        try:
            priority, seq, ticker, agents, force = _QUEUE.get()
        except Exception:
            time.sleep(5)
            continue

        try:
            warm_ticker(ticker, agents=agents, force=force)
        except Exception as e:
            print(f"WL_WARMER: warm_ticker crashed for {ticker}: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(ticker)
            try:
                _QUEUE.task_done()
            except Exception:
                pass

        # Be polite to Screener/Gemini/Perplexity between tickers.
        time.sleep(TICKER_GAP_SECONDS)


def start_worker():
    """Start the single warm worker. Idempotent."""
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    _WORKER_STARTED = True
    threading.Thread(target=_warm_worker, daemon=True,
                     name="wl-warm-worker").start()


# -- Cross-instance guard --------------------------------------------------
def _acquire_ticker_lock(ticker):
    """Redis NX lock so a scaled-out second instance can't double-spend.

    Deliberately not the CWD-relative check-then-write screener.lock pattern,
    which is racy. Returns True when this process owns the ticker.
    """
    r = _redis()
    if not r:
        return True
    try:
        return bool(r.set(f"wl_warm_lock:{ticker.upper()}", str(os.getpid()),
                          nx=True, ex=TICKER_LOCK_TTL_SECONDS))
    except Exception:
        return True  # never block warming just because the lock failed


def _release_ticker_lock(ticker):
    r = _redis()
    if not r:
        return
    try:
        r.delete(f"wl_warm_lock:{ticker.upper()}")
    except Exception:
        pass


# =====================================================================
# COMPANY NAME HELPERS
# =====================================================================
_NAME_TO_TICKER = None
_NAME_MAP_LOCK = threading.Lock()


def _company_name(ticker):
    """Best-effort company name, matching what the Analyst Agent uses."""
    try:
        from analyst_reports.trendlyne_fetcher import TRENDLYNE_DATA
        row = TRENDLYNE_DATA.get(ticker.upper())
        if row:
            return row.get("stock_name") or ticker
    except Exception:
        pass
    return ticker


def _name_to_ticker_map(csv_path="trendlyne_all_stocks_master.csv"):
    """normalized company name -> NSE ticker.

    Concall schedule entries frequently carry no ticker (sources are deduped by
    company name), so completed calls have to be matched by name. Uses
    live_concall._norm_company_name on BOTH sides so normalisation is identical.
    """
    global _NAME_TO_TICKER
    with _NAME_MAP_LOCK:
        if _NAME_TO_TICKER is not None:
            return _NAME_TO_TICKER
        mapping = {}
        try:
            from agents.live_concall import _norm_company_name
            with open(csv_path, "r", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    name = (row.get("Stock Name") or "").strip()
                    tick = (row.get("Ticker") or "").strip().upper()
                    if not name or not tick:
                        continue
                    norm = _norm_company_name(name)
                    if norm and norm not in mapping:
                        mapping[norm] = tick
            print(f"WL_WARMER: built {len(mapping)} name->ticker mappings",
                  file=sys.stderr)
        except Exception as e:
            print(f"WL_WARMER: name->ticker map failed: {e}", file=sys.stderr)
        _NAME_TO_TICKER = mapping
        return mapping


MIN_PREFIX_MATCH_LEN = 6


def _resolve_call_ticker(company_name, watchlist):
    """Map a concall schedule company name onto a watchlist ticker, or ''.

    The schedule sources truncate names ("Kalpat.", "Godrej Propert.",
    "Nazara Technolo."), so an exact normalised match only covers about half of
    a real day's calls. Fall back to a prefix match, but never guess between
    several candidates: a wrong guess wastes a concall run on the wrong company.
    """
    try:
        from agents.live_concall import _norm_company_name
    except Exception:
        return ""

    norm = _norm_company_name(company_name or "")
    if not norm:
        return ""

    mapping = _name_to_ticker_map()

    exact = mapping.get(norm)
    if exact:
        return exact if exact in watchlist else ""

    if len(norm) < MIN_PREFIX_MATCH_LEN:
        return ""  # too short to prefix-match safely

    # Truncated schedule name -> the CSV name extends it.
    candidates = {t for name, t in mapping.items() if name.startswith(norm)}
    if not candidates:
        return ""

    # We only ever act on watchlist tickers, so narrowing to those is both the
    # useful filter and a natural disambiguator.
    on_watchlist = candidates & set(watchlist)
    if len(on_watchlist) == 1:
        return next(iter(on_watchlist))
    if len(on_watchlist) > 1:
        print(f"WL_WARMER: ambiguous concall company '{company_name}' -> "
              f"{sorted(on_watchlist)}; skipping rather than guessing",
              file=sys.stderr)
    return ""


# =====================================================================
# PER-AGENT WARM STEPS
# =====================================================================
def _read_stock_cache(ticker):
    """Prefer the reader that sees what /analyze itself would find on open."""
    fn = _dep("read_stock_cache_fn") or _dep("get_any_cache_fn")
    if not fn:
        return None
    try:
        return fn(ticker)
    except Exception:
        return None


def _cached_is_consolidated(ticker):
    data = _read_stock_cache(ticker)
    if isinstance(data, dict):
        return bool(data.get("is_consolidated", False))
    return False


def probe_latest_quarter(ticker):
    """Live Screener latest-quarter header, or '' when the fetch failed.

    '' means 'unknown', never 'new quarter' - same guard the Forensic Agent
    uses (agents/forensic_agent.py:760). Callers must not invalidate on ''.
    """
    try:
        from fetchers.screener_fetcher import fetch_latest_quarter_header_async
        return asyncio.run(
            fetch_latest_quarter_header_async(
                ticker, consolidated=_cached_is_consolidated(ticker))
        ) or ""
    except Exception as e:
        print(f"WL_WARMER: quarter probe failed for {ticker}: {e}", file=sys.stderr)
        return ""


def _already_fresh(ticker, agent):
    """True when a usable cached payload exists and state is ready."""
    reg = registry_get(ticker)
    slot = reg["agents"].get(agent) or {}
    if slot.get("state") != "ready":
        return False
    try:
        if agent == "financials":
            return bool(_financials_cached(ticker))
        from agents.base import get_latest_result
        return bool(get_latest_result(agent, ticker))
    except Exception:
        return False


def _dependency_ready(dep, ticker):
    """Has `dep` produced data its dependants can actually use?"""
    if dep == "financials":
        return _financials_cached(ticker)
    return bool(_has_agent_result(dep, ticker))


def _financials_cached(ticker):
    """A non-light stock_analysis payload - the only kind that returns instantly."""
    data = _read_stock_cache(ticker)
    if not isinstance(data, dict):
        return False
    if data.get("light_cache"):
        return False  # /analyze would rebuild every chart (~30-45s) on open
    return bool(data.get("fundamentals"))


_PRICE_FEED_PRIMED = False
_PRIME_LOCK = threading.Lock()


def _prime_price_feed(ticker):
    """Establish the TradingView session before /analyze needs it.

    The first tvDatafeed call in a fresh process fails ("Connection to remote
    host was lost"), which makes /analyze answer 404 "No or insufficient
    historical data" for a perfectly valid ticker. A direct
    evaluate_ticker_signal call establishes the session -- it retries three
    times and falls back to yfinance -- after which /analyze succeeds.

    The warmer is the code most exposed to this, because it is usually the first
    thing to touch the price feed after a restart or a fresh deploy. Priming is
    done once per process, plus before any retry (a failure suggests the session
    dropped again).
    """
    global _PRICE_FEED_PRIMED
    try:
        from calculations.tech_calculations import evaluate_ticker_signal
        res = evaluate_ticker_signal(ticker)
        rows = len(res["Data"]) if res and res.get("Data") is not None else 0
        print(f"WL_WARMER: [{ticker}] primed price feed ({rows} bars, "
              f"signal={(res or {}).get('Signal')})", file=sys.stderr)
        if rows:
            _PRICE_FEED_PRIMED = True
        return rows > 0
    except Exception as e:
        print(f"WL_WARMER: [{ticker}] price feed priming failed: {e}",
              file=sys.stderr)
        return False


def _warm_financials(ticker, job_id):
    """Rebuild the full /analyze payload through the real endpoint.

    Primes the price feed first (see _prime_price_feed) and retries once, so a
    cold or dropped TradingView session doesn't leave the panel broken until the
    next scheduled cycle.
    """
    run_full = _dep("run_full_analyze_fn")
    if not run_full:
        return "failed", "run_full_analyze_fn not configured"

    with _PRIME_LOCK:
        if not _PRICE_FEED_PRIMED:
            _job_progress(job_id, f"Connecting to the price feed for {ticker}...")
            _prime_price_feed(ticker)

    last_err = None
    for attempt in range(1, FINANCIALS_WARM_ATTEMPTS + 1):
        suffix = "" if attempt == 1 else f" (retry {attempt - 1})"
        _job_progress(job_id, f"Rebuilding full financial analysis for {ticker}{suffix}...")
        ok, err = run_full(ticker)
        if ok:
            return "ready", None
        last_err = err or "analysis failed"
        if attempt < FINANCIALS_WARM_ATTEMPTS:
            print(f"WL_WARMER: [{ticker}] financials attempt {attempt} failed "
                  f"({last_err}); re-priming price feed and retrying in "
                  f"{FINANCIALS_RETRY_GAP_SECONDS}s", file=sys.stderr)
            time.sleep(FINANCIALS_RETRY_GAP_SECONDS)
            _prime_price_feed(ticker)

    return "failed", last_err


def _warm_forensic(ticker, job_id):
    get_cache = _dep("get_any_cache_fn")
    gemini = _dep("call_gemini_api_fn")
    perplexity = _dep("call_perplexity_api_fn")
    docs = _dep("fetch_forensic_documents_async_fn")
    if not all([get_cache, gemini, perplexity, docs]):
        return "failed", "forensic dependencies not configured"

    cached_data = None
    try:
        cached_data = get_cache(ticker)
    except Exception as e:
        return "failed", f"base data lookup failed: {e}"

    if not cached_data:
        # Financials runs first, so this only happens when that step failed.
        # Deliberately NOT falling back to get_analysis_for_ticker: it stamps
        # light_cache=True and would downgrade the financials payload.
        return "failed", ("no financial data cached yet - the Financials step "
                          "has to succeed first")

    from agents.forensic_agent import _run_forensic_analysis
    _run_forensic_analysis(job_id, ticker, cached_data, gemini, perplexity, docs)
    return _job_outcome(job_id)


def _warm_analyst(ticker, job_id):
    gemini = _dep("call_gemini_api_fn")
    psearch = _dep("call_perplexity_search_api_fn")
    reports = _dep("fetch_analyst_reports_async_fn")
    if not all([gemini, psearch, reports]):
        return "failed", "analyst dependencies not configured"

    from agents.analyst_agent import _run_analyst_analysis
    _run_analyst_analysis(job_id, ticker, _company_name(ticker),
                          gemini, psearch, reports)
    return _job_outcome(job_id)


def _warm_concall(ticker, job_id):
    gemini = _dep("call_gemini_api_fn")
    docs = _dep("fetch_latest_documents_async_fn")
    pdf_text = _dep("get_text_from_pdf_url_async_fn")
    if not all([gemini, docs, pdf_text]):
        return "failed", "concall dependencies not configured"

    from agents.concall_agent import _run_concall_analysis
    _run_concall_analysis(job_id, ticker, gemini, docs, pdf_text)
    state, err = _job_outcome(job_id)

    if state == "failed":
        # A missing transcript is the normal case for days after a filing, not
        # an error. Keep retrying inside the window; give up after it.
        slot = (registry_get(ticker)["agents"].get("concall") or {})
        first = slot.get("first_attempt_at") or time.time()
        if (time.time() - first) < CONCALL_RETRY_DAYS * 86400:
            return "awaiting_transcript", err
    return state, err


_WARM_FUNCS = {
    "financials": _warm_financials,
    "forensic": _warm_forensic,
    "analyst": _warm_analyst,
    "concall": _warm_concall,
}


def _job_progress(job_id, message):
    if not job_id:
        return
    try:
        from agents.base import update_agent_job
        update_agent_job(job_id, {"progress": message})
    except Exception:
        pass


def _job_outcome(job_id):
    """Read a finished pipeline's job record -> (state, error)."""
    try:
        from agents.base import get_agent_job
        job = get_agent_job(job_id) or {}
        if job.get("status") == "complete" and job.get("result"):
            return "ready", None
        return "failed", job.get("error") or "pipeline did not complete"
    except Exception as e:
        return "failed", f"job lookup failed: {e}"


# =====================================================================
# WARM ONE AGENT / ONE TICKER
# =====================================================================
def warm_one(ticker, agent, force=False, job_id=None, quarter=None, auto_dep=True):
    """Warm a single agent for a single ticker. Never raises.

    Returns (state, error). state is one of ready / failed /
    awaiting_transcript / skipped.

    auto_dep: when this agent declares a dependency (forensic -> financials) and
    that dependency has no usable data, warm it first. warm_ticker passes False
    because it already runs the agents in dependency order and reports the skip
    itself, rather than retrying a step that just failed.
    """
    ticker = (ticker or "").strip().upper()
    if agent not in _WARM_FUNCS:
        return "failed", f"unknown agent '{agent}'"
    if not _configured:
        return "failed", "warmer not configured"

    with _agent_lock(ticker, agent):
        if not force and _already_fresh(ticker, agent):
            print(f"WL_WARMER: {ticker}/{agent} already fresh - skipping",
                  file=sys.stderr)
            return "skipped", None

        if quarter is None:
            quarter = (registry_get(ticker).get("quarter"))

        # --- dependency gate: forensic only runs once financials has data ---
        dep = AGENT_DEPENDS_ON.get(agent)
        if dep and not _dependency_ready(dep, ticker):
            if not auto_dep:
                reason = f"needs {dep} to succeed first"
                _set_agent_state(ticker, agent, "failed", quarter=quarter,
                                 error=reason)
                return "failed", reason

            print(f"WL_WARMER: [{ticker}] {agent} needs {dep} first - warming it",
                  file=sys.stderr)
            _job_progress(job_id, f"Preparing {dep} for {ticker} first...")
            # Separate lock, so no deadlock with the one held above.
            dep_state, dep_err = warm_one(ticker, dep, force=True, auto_dep=False)
            if not _dependency_ready(dep, ticker):
                reason = (f"{dep} could not be prepared, so {agent} was skipped"
                          + (f": {dep_err}" if dep_err else ""))
                _set_agent_state(ticker, agent, "failed", quarter=quarter,
                                 error=reason)
                print(f"WL_WARMER: [{ticker}] {agent} skipped - {dep} "
                      f"unavailable ({dep_state})", file=sys.stderr)
                return "failed", reason

        _set_agent_state(ticker, agent, "warming", quarter=quarter,
                         bump_attempts=True)

        own_job = False
        if not job_id:
            try:
                from agents.base import create_agent_job
                job_id = create_agent_job(agent, ticker)
                own_job = True
            except Exception as e:
                _set_agent_state(ticker, agent, "failed", quarter=quarter,
                                 error=f"job creation failed: {e}")
                return "failed", str(e)

        started = time.time()
        print(f"WL_WARMER: [{ticker}] warming {agent}...", file=sys.stderr)
        try:
            state, err = _WARM_FUNCS[agent](ticker, job_id)
        except Exception as e:
            state, err = "failed", str(e)
            print(f"WL_WARMER: [{ticker}] {agent} raised: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

        elapsed = int(time.time() - started)
        _set_agent_state(ticker, agent, state, quarter=quarter, error=err)
        print(f"WL_WARMER: [{ticker}] {agent} -> {state} in {elapsed}s"
              + (f" ({err})" if err else ""), file=sys.stderr)

        # Mirror the outcome onto the job so a polling UI sees a terminal state.
        if own_job and state in ("failed", "awaiting_transcript"):
            try:
                from agents.base import update_agent_job, get_agent_job
                if (get_agent_job(job_id) or {}).get("status") == "processing":
                    update_agent_job(job_id, {"status": "error",
                                              "error": err or state})
            except Exception:
                pass

        return state, err


def warm_ticker(ticker, agents=None, force=False):
    """Warm the requested agents for one ticker, in WARM_AGENTS order."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {}
    requested = [a for a in WARM_AGENTS if a in tuple(agents or WARM_AGENTS)]

    if not _acquire_ticker_lock(ticker):
        print(f"WL_WARMER: {ticker} locked by another instance - skipping",
              file=sys.stderr)
        return {}

    outcomes = {}
    try:
        quarter = registry_get(ticker).get("quarter")
        print(f"WL_WARMER: === {ticker} start (agents={requested}, "
              f"force={force}, quarter={quarter}) ===", file=sys.stderr)
        for agent in requested:
            dep = AGENT_DEPENDS_ON.get(agent)
            # The dependency ran earlier in this same pass (WARM_AGENTS order)
            # and left nothing usable. Don't re-run it and don't run the
            # dependant: that would burn a pipeline and report a misleading
            # "no base data" as though the problem were the dependant's.
            if dep and dep in requested and not _dependency_ready(dep, ticker):
                reason = (f"skipped - {dep.title()} did not complete this run, "
                          f"so there is no financial data to audit yet")
                _set_agent_state(ticker, agent, "failed", quarter=quarter,
                                 error=reason)
                outcomes[agent] = "skipped"
                print(f"WL_WARMER: [{ticker}] {agent} {reason}", file=sys.stderr)
                continue

            # auto_dep is on for agents whose dependency wasn't part of this
            # pass, so a gap-fill of ['forensic'] alone still gets its base data.
            state, err = warm_one(ticker, agent, force=force, quarter=quarter,
                                  auto_dep=True)
            outcomes[agent] = state
        print(f"WL_WARMER: === {ticker} done -> {outcomes} ===", file=sys.stderr)
    finally:
        _release_ticker_lock(ticker)
    return outcomes


def refresh_agent_now(ticker, agent, force=True):
    """User-triggered per-panel refresh.

    Runs in its own thread rather than via the queue, so an interactive refresh
    never waits behind a nightly backlog. Returns a job_id to poll.
    """
    ticker = (ticker or "").strip().upper()
    from agents.base import create_agent_job
    job_id = create_agent_job(agent, ticker)

    def _run():
        try:
            warm_one(ticker, agent, force=force, job_id=job_id)
        except Exception as e:
            print(f"WL_WARMER: refresh_agent_now failed {ticker}/{agent}: {e}",
                  file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

    threading.Thread(target=_run, daemon=True,
                     name=f"wl-refresh-{ticker}-{agent}").start()
    return job_id


# =====================================================================
# CYCLE A - quarter probe
# =====================================================================
def run_watchlist_quarter_cycle(max_warm=MAX_WARM_PER_CYCLE):
    """Probe every watchlist ticker's latest quarter; enqueue what moved."""
    started = datetime.now()
    tickers = _all_watchlist_tickers()
    print(f"\n{'=' * 60}\nWL_WARMER: quarter cycle over {len(tickers)} "
          f"watchlist ticker(s) at {started:%Y-%m-%d %H:%M}\n{'=' * 60}",
          file=sys.stderr)
    if not tickers:
        _log_cycle_summary(started, 0, 0, [], "no_tickers")
        return

    new_quarter, incomplete, probe_failed = [], [], []

    for i, ticker in enumerate(tickers):
        if i:
            time.sleep(QUARTER_PROBE_GAP_SECONDS)
        reg = registry_get(ticker)
        known = reg.get("quarter")
        live = probe_latest_quarter(ticker)

        if not live:
            probe_failed.append(ticker)
            print(f"WL_WARMER: {ticker} quarter probe returned nothing - "
                  f"leaving state untouched", file=sys.stderr)
        elif known and live != known:
            print(f"WL_WARMER: {ticker} NEW QUARTER {known} -> {live}",
                  file=sys.stderr)
            reg["quarter"] = live
            reg["quarter_checked_at"] = time.time()
            for a in WARM_AGENTS:
                slot = reg["agents"].setdefault(a, _blank_agent_state())
                slot["state"] = "stale"
                slot["first_attempt_at"] = None
                slot["attempts"] = 0
            registry_save(ticker, reg)
            new_quarter.append(ticker)
            continue
        else:
            reg["quarter"] = live
            reg["quarter_checked_at"] = time.time()
            registry_save(ticker, reg)

        # Nothing new - but pick up anything never warmed or previously failed.
        missing = [
            a for a in WARM_AGENTS
            if (reg["agents"].get(a) or {}).get("state") in
            (None, "none", "failed", "stale")
        ]
        if missing:
            incomplete.append((ticker, missing))

    # New quarters first, then gap-fills.
    queued = 0
    deferred = []
    for ticker in new_quarter:
        if queued >= max_warm:
            deferred.append(ticker)
            continue
        if enqueue(ticker, WARM_AGENTS, force=True, priority=1):
            queued += 1
    for ticker, missing in incomplete:
        if queued >= max_warm:
            deferred.append(ticker)
            continue
        if enqueue(ticker, missing, force=False, priority=1):
            queued += 1

    if deferred:
        print(f"WL_WARMER: CAP REACHED ({max_warm}) - deferred to next cycle: "
              f"{', '.join(deferred[:40])}"
              f"{' ...' if len(deferred) > 40 else ''}", file=sys.stderr)
    if probe_failed:
        print(f"WL_WARMER: {len(probe_failed)} quarter probe(s) failed: "
              f"{', '.join(probe_failed[:20])}", file=sys.stderr)

    print(f"WL_WARMER: quarter cycle queued {queued}, new_quarter="
          f"{len(new_quarter)}, gap_fill={len(incomplete)}, "
          f"deferred={len(deferred)}", file=sys.stderr)
    _log_cycle_summary(started, len(tickers), queued, new_quarter, "completed",
                       deferred=deferred, probe_failed=probe_failed)


def _all_watchlist_tickers():
    try:
        from auth.database import Watchlist
        return Watchlist.get_all_tickers()
    except Exception as e:
        print(f"WL_WARMER: could not read watchlist tickers: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return []


def _log_cycle_summary(started, ticker_count, queued, new_quarter, status,
                       deferred=None, probe_failed=None):
    """Persist a last-run summary for admin visibility."""
    summary = {
        "timestamp": started.isoformat(),
        "duration_seconds": int((datetime.now() - started).total_seconds()),
        "watchlist_tickers": ticker_count,
        "queued": queued,
        "new_quarter_tickers": (new_quarter or [])[:100],
        "deferred": (deferred or [])[:100],
        "probe_failed": (probe_failed or [])[:100],
        "queue_depth": queue_depth(),
        "status": status,
    }
    r = _redis()
    if r:
        try:
            r.setex("wl_warm_last_run", 86400 * 3, json.dumps(summary))
        except Exception:
            pass
    return summary


# =====================================================================
# CYCLE B - re-warm concall after a scheduled call finishes
# =====================================================================
def run_post_concall_cycle():
    """Find watchlist concalls that have finished and re-warm that agent.

    Uses the raw snapshot: load_concall_schedule() filters to today..+days and
    drops past entries, so it can never see a completed call.
    """
    try:
        from agents.live_concall import load_raw_concall_calls
    except Exception as e:
        print(f"WL_WARMER: concall schedule unavailable: {e}", file=sys.stderr)
        return 0

    watchlist = set(_all_watchlist_tickers())
    if not watchlist:
        return 0

    try:
        calls = load_raw_concall_calls()
    except Exception as e:
        print(f"WL_WARMER: raw concall read failed: {e}", file=sys.stderr)
        return 0

    now = datetime.now()
    triggered = 0

    for call in calls:
        try:
            call_date = (call.get("call_date") or "").strip()
            if not call_date:
                continue

            # Schedule entries rarely carry a ticker (sources dedupe by company
            # name), so name resolution is the primary path, not a fallback.
            ticker = (call.get("ticker") or "").strip().upper()
            if ticker not in watchlist:
                ticker = _resolve_call_ticker(call.get("company_name", ""), watchlist)
                if not ticker:
                    continue

            try:
                day = datetime.strptime(call_date, "%Y-%m-%d")
            except ValueError:
                continue

            call_time = (call.get("call_time") or "").strip()
            if call_time:
                start = _parse_call_start(call_date, call_time)
                if start is None:
                    continue
                finished_at = start + timedelta(minutes=CONCALL_DURATION_MINUTES)
            else:
                # Date-only / time-TBD: treat it as done at end of that day, so
                # the 23:00 cycle picks it up the same night.
                finished_at = day + timedelta(hours=22, minutes=30)

            if now < finished_at:
                continue
            if (now - finished_at) > timedelta(hours=CONCALL_LOOKBACK_HOURS):
                continue  # old call; don't replay history after a restart

            reg = registry_get(ticker)
            done = reg.get("concall_calls_done") or []
            if call_date in done:
                continue

            done.append(call_date)
            reg["concall_calls_done"] = done[-12:]
            slot = reg["agents"].setdefault("concall", _blank_agent_state())
            slot["state"] = "stale"
            slot["attempts"] = 0
            slot["first_attempt_at"] = None
            registry_save(ticker, reg)

            print(f"WL_WARMER: concall for {ticker} finished "
                  f"({call_date} {call_time or 'time TBD'}) - re-warming",
                  file=sys.stderr)
            if enqueue(ticker, ("concall",), force=True, priority=1):
                triggered += 1
        except Exception as e:
            print(f"WL_WARMER: post-concall entry failed: {e}", file=sys.stderr)

    print(f"WL_WARMER: post-concall cycle triggered {triggered} re-warm(s)",
          file=sys.stderr)
    return triggered


def _parse_call_start(call_date, call_time):
    """Reuse live_concall's own parser so formats stay consistent."""
    try:
        from agents.live_concall import _parse_call_dt
        return _parse_call_dt(call_date, call_time)
    except Exception:
        return None


# =====================================================================
# CYCLE C - nightly concall transcript retry (up to CONCALL_RETRY_DAYS)
# =====================================================================
def run_concall_retry_cycle():
    """Retry tickers still waiting on a transcript; retire past the window."""
    retried = retired = 0
    for ticker in _all_watchlist_tickers():
        try:
            reg = registry_get(ticker)
            slot = reg["agents"].get("concall") or {}
            if slot.get("state") != "awaiting_transcript":
                continue

            first = slot.get("first_attempt_at")
            age_days = ((time.time() - first) / 86400) if first else 0
            if first and age_days >= CONCALL_RETRY_DAYS:
                _set_agent_state(
                    ticker, "concall", "failed",
                    error=f"no transcript found after {CONCALL_RETRY_DAYS} days")
                print(f"WL_WARMER: {ticker} concall retired after "
                      f"{age_days:.1f}d without a transcript", file=sys.stderr)
                retired += 1
                continue

            print(f"WL_WARMER: {ticker} concall retry "
                  f"(day {age_days:.1f}/{CONCALL_RETRY_DAYS}, "
                  f"attempt {slot.get('attempts')})", file=sys.stderr)
            if enqueue(ticker, ("concall",), force=True, priority=1):
                retried += 1
        except Exception as e:
            print(f"WL_WARMER: concall retry failed for {ticker}: {e}",
                  file=sys.stderr)

    print(f"WL_WARMER: concall retry cycle - {retried} retried, "
          f"{retired} retired", file=sys.stderr)
    return retried


# =====================================================================
# SCHEDULER ENTRY POINT
# =====================================================================
def run_full_cycle(is_night=True):
    """One scheduled pass. Called on a daemon thread by handler.py."""
    try:
        run_watchlist_quarter_cycle()
    except Exception as e:
        print(f"WL_WARMER: quarter cycle failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    try:
        run_post_concall_cycle()
    except Exception as e:
        print(f"WL_WARMER: post-concall cycle failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    if is_night:
        try:
            run_concall_retry_cycle()
        except Exception as e:
            print(f"WL_WARMER: concall retry cycle failed: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)


# =====================================================================
# READINESS (for the watchlist cards)
# =====================================================================
def readiness_for(tickers):
    """Per-ticker, per-agent state for the watchlist grid.

    Cheap: registry read plus a cache existence probe per agent. Never raises.
    """
    out = {}
    for ticker in tickers or []:
        ticker = (ticker or "").strip().upper()
        if not ticker:
            continue
        try:
            reg = registry_get(ticker)
            agents = {}
            for a in WARM_AGENTS:
                slot = dict(reg["agents"].get(a) or _blank_agent_state())
                have = (_financials_payload_exists(ticker) if a == "financials"
                        else _has_agent_result(a, ticker))
                state = slot.get("state") or "none"
                # Reconcile the registry against what is actually cached, in
                # both directions, so a card never misreports what a click does.
                if have and state == "none":
                    # A payload exists but we never warmed it, so we can't vouch
                    # for it being current (or, for financials, non-light).
                    # Openable, but honestly reported as stale.
                    state = "stale"
                elif have and state == "failed":
                    # Openable, but the last refresh failed -- old data, so say
                    # stale rather than claiming it is current.
                    state = "stale"
                elif not have and state == "ready":
                    # Registry says ready but the payload is gone (TTL expired).
                    state = "stale"
                slot["state"] = state
                slot["has_payload"] = bool(have)
                agents[a] = slot
            out[ticker] = {
                "quarter": reg.get("quarter"),
                "quarter_checked_at": reg.get("quarter_checked_at"),
                "agents": agents,
            }
        except Exception as e:
            print(f"WL_WARMER: readiness failed for {ticker}: {e}", file=sys.stderr)
    return out


def _redis_exists(*keys):
    """Cheap existence probe. None means 'cannot tell' (no Redis)."""
    r = _redis()
    if not r:
        return None
    for key in keys:
        try:
            if r.exists(key):
                return True
        except Exception:
            return None
    return False


def _has_agent_result(agent, ticker):
    """Does a cached agent result exist? EXISTS only - never decompress.

    readiness_for runs this for 4 agents x every watchlist ticker and the UI
    polls it, so decoding the payloads here (zlib + JSON, megabytes each) would
    make the endpoint far more expensive than the data it returns.
    """
    ticker = ticker.upper()
    try:
        from agents.base import AGENT_LATEST_RESULTS
        entry = AGENT_LATEST_RESULTS.get(f"{agent}_{ticker}")
        if entry and time.time() <= entry.get("expires_at", 0):
            return True
    except Exception:
        pass
    found = _redis_exists(f"agent_result_{agent}_{ticker}")
    return bool(found)


def _financials_payload_exists(ticker):
    """As above, for the stock_analysis payload. Existence only."""
    ticker = ticker.upper().strip()
    found = _redis_exists(f"stock_analysis_{ticker}",
                          f"flask_cache_stock_analysis_{ticker}")
    if found is not None:
        return found
    # No Redis (local dev): fall back to the full read.
    return _financials_cached(ticker)


def last_run_summary():
    r = _redis()
    if not r:
        return None
    try:
        raw = r.get("wl_warm_last_run")
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)
    except Exception:
        return None
