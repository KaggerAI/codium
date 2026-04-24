"""
Quarterly Results Watcher — Detects newly-published quarterly results
via BSE API and triggers selective light cache refresh.

Called at 8:00 AM and 5:00 PM IST by the scheduler in handler.py.
"""

import os
import csv
import json
import time
import traceback
import requests
import pickle
import zlib
from datetime import datetime, timedelta
from typing import Dict, Set, List, Optional

# ─── BSE API CONFIG ────────────────────────────────────────────────
BSE_API_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"
BSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bseindia.com/corporates/ann.html",
    "Accept": "application/json",
}


def _build_bse_to_nse_map(
    csv_path: str = "trendlyne_all_stocks_master.csv",
) -> Dict[str, str]:
    """
    Build a BSE scrip code → NSE ticker lookup table
    from the 'BSE Ticker' column in the master CSV.

    Returns: { "532174": "ICICIBANK", "532540": "TCS", ... }
    """
    bse_to_nse = {}
    try:
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                bse = str(row.get("BSE Ticker", "")).strip()
                nse = str(row.get("Ticker", "")).strip().upper()
                if bse and nse:
                    bse_to_nse[bse] = nse
        print(f"RESULTS_WATCHER: Loaded {len(bse_to_nse)} BSE->NSE mappings")
    except Exception as e:
        print(f"RESULTS_WATCHER: Failed to load CSV: {e}")
    return bse_to_nse


def fetch_recent_result_filings(lookback_days: int = 1) -> List[Dict]:
    """
    Query BSE API for all 'Result' category filings in the last 24 hours.
    Returns raw filing dicts from the API.
    """
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    to_date = now_ist.strftime("%Y%m%d")
    from_date = (now_ist - timedelta(days=lookback_days)).strftime("%Y%m%d")

    params = {
        "strCat": "Result",
        "strPrevDate": from_date,
        "strToDate": to_date,
        "strScrip": "",
        "strSearch": "P",
        "strType": "C",
    }

    try:
        resp = requests.get(
            BSE_API_URL,
            params=params,
            headers=BSE_HEADERS,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        filings = data.get("Table", [])
        print(
            f"RESULTS_WATCHER: BSE API returned {len(filings)} result filings "
            f"({from_date} -> {to_date})"
        )
        return filings

    except requests.exceptions.Timeout:
        print("RESULTS_WATCHER: BSE API timed out after 30s")
        return []
    except requests.exceptions.HTTPError as e:
        print(f"RESULTS_WATCHER: BSE API HTTP error: {e}")
        return []
    except Exception as e:
        print(f"RESULTS_WATCHER: BSE API failed: {e}")
        traceback.print_exc()
        return []


def get_cached_tickers(redis_client) -> List[str]:
    """Scan Redis for all stock_analysis_* keys -> list of ticker strings."""
    cached = []
    if not redis_client:
        return cached
    try:
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(
                cursor, match="stock_analysis_*", count=200
            )
            for key in keys:
                ticker = key.decode("utf-8").replace("stock_analysis_", "")
                if ticker:
                    cached.append(ticker)
            if cursor == 0:
                break
    except Exception as e:
        print(f"RESULTS_WATCHER: Redis scan failed: {e}")
    return cached


def run_results_watch_cycle(
    redis_client,
    run_batch_precache_fn,
    csv_path: str = "trendlyne_all_stocks_master.csv",
    max_refresh_per_cycle: int = 50,
):
    """
    Main orchestrator. Called by the scheduler daemon at 8am / 5pm IST.

    1. Build BSE scrip -> NSE ticker map from CSV
    2. Query BSE API for recent 'Result' filings (last 24h)
    3. Map filed scrip codes → NSE tickers
    4. Intersect with cached tickers in Redis
    5. Trigger light cache refresh for the intersection
    """
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    print(f"\n{'=' * 60}")
    print(
        f"RESULTS_WATCHER: Starting cycle at "
        f"{now_ist.strftime('%Y-%m-%d %H:%M IST')}"
    )
    print(f"{'=' * 60}")

    # -- Step 1: Build BSE -> NSE map --
    bse_to_nse = _build_bse_to_nse_map(csv_path)
    if not bse_to_nse:
        print("RESULTS_WATCHER: WARNING - No BSE->NSE mappings found. Aborting.")
        _log_run_summary(redis_client, now_ist, 0, 0, [], "no_mappings")
        return

    # -- Step 2: Fetch result filings from BSE --
    filings = fetch_recent_result_filings(lookback_days=1)
    if not filings:
        print(
            "RESULTS_WATCHER: No result filings returned. "
            "Either no filings or API issue."
        )
        _log_run_summary(redis_client, now_ist, 0, 0, [], "no_filings")
        return

    # -- Step 3: Map SCRIP_CD -> NSE tickers --
    filed_scrips = set()
    for filing in filings:
        scrip = str(filing.get("SCRIP_CD", "")).strip()
        if scrip:
            filed_scrips.add(scrip)

    filed_tickers = set()
    unmapped_scrips = []
    for scrip in filed_scrips:
        nse_ticker = bse_to_nse.get(scrip)
        if nse_ticker:
            filed_tickers.add(nse_ticker)
        else:
            # Log for awareness — these are BSE-only or missing from CSV
            unmapped_scrips.append(scrip)

    print(f"RESULTS_WATCHER: {len(filed_scrips)} unique scrip codes filed results")
    print(f"RESULTS_WATCHER: {len(filed_tickers)} mapped to NSE tickers")
    if unmapped_scrips:
        print(
            f"RESULTS_WATCHER: {len(unmapped_scrips)} scrips not in CSV "
            f"(BSE-only or missing mapping)"
        )

    # -- Step 4: Intersect with cached tickers --
    cached_tickers = get_cached_tickers(redis_client)
    print(f"RESULTS_WATCHER: {len(cached_tickers)} tickers in Redis cache")

    stale_tickers = list(filed_tickers & set(cached_tickers))
    stale_tickers.sort()  # Deterministic order

    print(f"RESULTS_WATCHER: {len(stale_tickers)} cached tickers have new results")

    # -- Step 5: Refresh stale tickers --
    if stale_tickers:
        batch = stale_tickers[:max_refresh_per_cycle]
        if len(stale_tickers) > max_refresh_per_cycle:
            print(
                f"RESULTS_WATCHER: WARNING - Capped at {max_refresh_per_cycle} "
                f"(total stale: {len(stale_tickers)}). "
                f"Remainder caught next cycle."
            )

        print(f"RESULTS_WATCHER: [REFRESH] Refreshing: {', '.join(batch)}")

        job_id = f"auto_{now_ist.strftime('%Y%m%d_%H%M')}"
        run_batch_precache_fn(job_id, batch)

        print(f"RESULTS_WATCHER: [DONE] Refresh complete for job {job_id}")
    else:
        print(
            "RESULTS_WATCHER: [OK] All cached data is current. No refresh needed."
        )

    # -- Step 6: Log summary --
    _log_run_summary(
        redis_client,
        now_ist,
        len(cached_tickers),
        len(stale_tickers),
        stale_tickers,
        "completed",
    )
    print(f"{'=' * 60}\n")


def _log_run_summary(
    redis_client,
    timestamp,
    cached_count: int,
    stale_count: int,
    stale_tickers: list,
    status: str,
):
    """Persist last-run summary in Redis for admin visibility."""
    summary = {
        "timestamp": timestamp.isoformat(),
        "cached_count": cached_count,
        "stale_count": stale_count,
        "stale_tickers": stale_tickers[:100],  # Cap for Redis size
        "method": "bse_api",
        "status": status,
    }
    try:
        if redis_client:
            redis_client.setex(
                "results_watcher_last_run",
                86400,  # 24h TTL
                json.dumps(summary),
            )
    except Exception:
        pass
