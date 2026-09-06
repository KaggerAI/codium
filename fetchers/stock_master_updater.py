"""
stock_master_updater.py - Keeps trendlyne_all_stocks_master.csv current.

Trendlyne publishes every equity page it knows about in a public sitemap:

    https://trendlyne.com/equity-sitemap-stocks.xml

Every <loc> in it carries the three fields the master CSV keys on:

    https://trendlyne.com/equity/<trendlyne_id>/<ticker>/<company-name-slug>/

Once a day this module diffs that sitemap against the master CSV and does two
things, keyed on the Trendlyne ID (the only stable identity a company has here):

  * APPENDS listings whose ID we have never seen.
  * UPDATES a row in place when Trendlyne has changed its ticker symbol or its
    company name - the Akzo Nobel India -> JSW Dulux kind of rebrand. Only
    Ticker, Stock Name and the Research Report URL derived from them are
    touched; MarketCap, Industry Name, Sector Name and BSE Ticker are carried
    over untouched, because the sitemap knows nothing about them.

A company name is only rewritten when the change is substantive. Slugs are
lower-cased and stripped of punctuation, so deriving a name from one turns
'3M India Ltd' into '3m India Ltd' and 'Amara Raja Energy & Mobility Ltd' into
'Amara Raja Energy Mobility Ltd'. Comparing the two names with punctuation and
case normalised away keeps the better-curated stored name in those cases and
still catches a real rename.

Two situations are reported rather than applied, because guessing wrong would
corrupt a lookup key the whole app depends on:

  * ambiguous - several master rows share the Trendlyne ID the sitemap wants to
    update (the master already holds ~23 such pairs, e.g. LTM / LTIM). Updating
    both would produce two identical tickers.
  * conflicts - the new ticker symbol is already held by a different company.

New rows are appended with MarketCap blank on purpose: enrich_market_caps()
picks up blank market caps on the next screener scan and back-fills them from
yfinance, so new listings join the screener universe on their own.

CLI:
    python -m fetchers.stock_master_updater --dry-run
    python -m fetchers.stock_master_updater
"""

import csv
import html
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import unquote

import requests

SITEMAP_URL = "https://trendlyne.com/equity-sitemap-stocks.xml"

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER_CSV_PATH = os.path.join(_PROJECT_ROOT, "trendlyne_all_stocks_master.csv")
STATUS_FILE = os.path.join(_PROJECT_ROOT, "cached_stock_master_update.json")

# Columns this module is allowed to write. Everything else in a known row is
# carried over verbatim.
_TICKER_COL = "Ticker"
_NAME_COL = "Stock Name"
_ID_COL = "Trendlyne ID"
_URL_COL = "Research Report URL"

# Trendlyne serves the sitemap gzipped; requests transparently inflates it.
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Accept": "application/xml,text/xml,*/*",
}

# https://trendlyne.com/equity/1127/RELIANCE/reliance-industries-ltd/
_EQUITY_URL_RE = re.compile(r"/equity/(\d+)/([^/]+)/([^/]+)/")
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")

# Guards for an unattended job that mutates the file the whole app reads from.
# A truncated response or an HTML error page parses to near-zero URLs; a change
# in Trendlyne's URL or slug shape would make every row look renamed and every
# entry look brand new.
MIN_SITEMAP_ENTRIES = 1000
MAX_NEW_ROWS_RATIO = 0.25
MAX_UPDATED_ROWS_RATIO = 0.10

# The scheduler, the startup prime and the admin endpoint can all reach this.
_UPDATE_LOCK = threading.Lock()


def fetch_sitemap(url=SITEMAP_URL, timeout=90):
    """GET the sitemap and return its XML text. Raises on a non-200."""
    resp = requests.get(url, headers=_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def slug_to_name(slug):
    """'reliance-industries-ltd' -> 'Reliance Industries Ltd'.

    Matches the capitalisation already used throughout the master CSV.
    """
    return " ".join(w.capitalize() for w in unquote(slug).split("-") if w)


def _normalize_name(name):
    """Case- and punctuation-free form, for telling a rebrand from a slug artefact."""
    return _NON_ALNUM_RE.sub("", (name or "").lower())


def _report_url(trendlyne_id, ticker):
    return f"https://trendlyne.com/research-reports/stock/{trendlyne_id}/{ticker}/"


def parse_sitemap_entries(xml_text):
    """Return [(trendlyne_id, ticker, name_slug), ...] in sitemap order.

    Parsed with a regex rather than ElementTree so a partially malformed
    document still yields the URLs it does contain instead of raising.
    """
    seen_ids = set()
    entries = []
    for loc in _LOC_RE.findall(xml_text or ""):
        # Tickers carrying an ampersand (GVT&D, ARE&M, IL&FSENGG) arrive
        # XML-escaped as GVT&amp;D; the master CSV stores them unescaped.
        match = _EQUITY_URL_RE.search(html.unescape(loc))
        if not match:
            continue
        tid, ticker, slug = match.groups()
        ticker = unquote(ticker).strip()
        if not ticker or tid in seen_ids:
            continue
        seen_ids.add(tid)
        entries.append((tid, ticker, slug))
    return entries


def _read_master_rows(csv_path):
    """Return (fieldnames, rows) with every column preserved, in file order."""
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    return fieldnames, rows


def _write_master(csv_path, fieldnames, rows):
    """Rewrite the master atomically, so a reader never sees a half-written file.

    A straight read/write round-trip of this file is byte-identical, so rows the
    planner left alone come out of here exactly as they went in.
    """
    tmp_path = csv_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="",
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    # os.replace is atomic, but on Windows it fails while another thread has the
    # file open for reading - which the agents do constantly.
    last_error = None
    for _ in range(5):
        try:
            os.replace(tmp_path, csv_path)
            return
        except PermissionError as e:
            last_error = e
            time.sleep(0.4)
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    raise last_error


def _plan_updates(rows, sitemap_by_id):
    """Work out which existing rows Trendlyne has renamed.

    Returns (updates, ambiguous, conflicts). Nothing is written here; each
    update is {'row': idx, 'trendlyne_id', 'changes': {column: (old, new)}}.
    """
    id_counts = Counter((r.get(_ID_COL) or "").strip() for r in rows)
    # Which row currently owns each symbol, kept in sync as renames are applied
    # so a symbol freed by one rename can be taken by another.
    ticker_owner = {}
    for idx, row in enumerate(rows):
        ticker = (row.get(_TICKER_COL) or "").strip().upper()
        if ticker:
            ticker_owner.setdefault(ticker, idx)

    candidates, ambiguous = [], []
    for idx, row in enumerate(rows):
        tid = (row.get(_ID_COL) or "").strip()
        if not tid or tid not in sitemap_by_id:
            continue
        new_ticker, slug = sitemap_by_id[tid]
        cur_ticker = (row.get(_TICKER_COL) or "").strip()
        cur_name = (row.get(_NAME_COL) or "").strip()
        new_name = slug_to_name(slug)

        ticker_changed = cur_ticker.upper() != new_ticker.upper()
        name_changed = _normalize_name(cur_name) != _normalize_name(new_name)
        if not ticker_changed and not name_changed:
            continue

        if id_counts[tid] > 1:
            # Several rows claim this ID; updating them all would collapse them
            # onto one ticker. Report and let a human decide.
            ambiguous.append({"trendlyne_id": tid, "ticker": cur_ticker,
                              "name": cur_name, "sitemap_ticker": new_ticker,
                              "sitemap_name": new_name})
            continue

        candidates.append({"row": idx, "trendlyne_id": tid,
                           "cur_ticker": cur_ticker, "new_ticker": new_ticker,
                           "cur_name": cur_name, "new_name": new_name,
                           "ticker_changed": ticker_changed,
                           "name_changed": name_changed})

    updates = []
    pending = candidates
    # A rename can be blocked by a symbol another rename is about to free
    # (A->B while B->C). Re-run the blocked ones until nothing more moves.
    for _ in range(3):
        if not pending:
            break
        blocked, progressed = [], False
        for cand in pending:
            idx = cand["row"]
            if cand["ticker_changed"]:
                owner = ticker_owner.get(cand["new_ticker"].upper())
                if owner is not None and owner != idx:
                    blocked.append(cand)
                    continue

            changes = {}
            if cand["ticker_changed"]:
                changes[_TICKER_COL] = (cand["cur_ticker"], cand["new_ticker"])
                new_url = _report_url(cand["trendlyne_id"], cand["new_ticker"])
                old_url = (rows[idx].get(_URL_COL) or "").strip()
                if old_url != new_url:
                    changes[_URL_COL] = (old_url, new_url)
                ticker_owner.pop(cand["cur_ticker"].upper(), None)
                ticker_owner[cand["new_ticker"].upper()] = idx
            if cand["name_changed"]:
                changes[_NAME_COL] = (cand["cur_name"], cand["new_name"])

            updates.append({"row": idx, "trendlyne_id": cand["trendlyne_id"],
                            "changes": changes})
            progressed = True

        pending = blocked
        if not progressed:
            break

    conflicts = [{"trendlyne_id": c["trendlyne_id"], "ticker": c["cur_ticker"],
                  "wanted_ticker": c["new_ticker"],
                  "reason": "another company already holds that ticker"}
                 for c in pending]
    return updates, ambiguous, conflicts


def _write_status(summary):
    try:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
    except Exception as e:
        print(f"STOCK_MASTER: could not write status file: {e}", file=sys.stderr)


def load_status():
    """Last run summary, or None when the job has never run here."""
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def is_stale(max_age_hours=24):
    """True when the master has not been checked within max_age_hours.

    Judged on the run timestamp recorded inside the status file, not its mtime:
    if the file is committed, a deploy checks it out with a brand-new mtime and
    a week-old image would look freshly checked.
    """
    try:
        status = load_status()
        if not isinstance(status, dict):
            return True  # missing or unreadable: assume a check is due
        ran_at = (status.get("ran_at") or "").replace(" IST", "")
        if not ran_at:
            return True
        last = datetime.strptime(ran_at, "%Y-%m-%d %H:%M:%S")
        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        return (now_ist - last).total_seconds() > max_age_hours * 3600
    except Exception:
        return True


def _summarise_updates(updates):
    """Compact, human-readable change records for the status file."""
    out = []
    for u in updates:
        record = {"trendlyne_id": u["trendlyne_id"]}
        for col, (old, new) in u["changes"].items():
            if col != _URL_COL:  # the URL just follows the ticker
                record[col] = f"{old} -> {new}"
        out.append(record)
    return out


def update_stock_master(csv_path=MASTER_CSV_PATH, url=SITEMAP_URL,
                        dry_run=False, force=False, write_status=True):
    """Sync the master CSV with the Trendlyne sitemap.

    Appends newly listed stocks and applies ticker/name changes to rows we
    already hold. Returns a summary dict: {'success', 'added', 'added_tickers',
    'updated', 'updates', 'ambiguous', 'conflicts', 'sitemap_entries',
    'rows_before', 'rows_after', 'error', 'ran_at'}.
    Never raises - the caller is a background thread.
    """
    started = time.time()
    now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
    summary = {
        "success": False,
        "ran_at": now_ist.strftime("%Y-%m-%d %H:%M:%S IST"),
        "dry_run": bool(dry_run),
        "sitemap_entries": 0,
        "rows_before": 0,
        "rows_after": 0,
        "added": 0,
        "added_tickers": [],
        "updated": 0,
        "updates": [],
        "ambiguous": [],
        "conflicts": [],
        "error": None,
    }

    if not _UPDATE_LOCK.acquire(blocking=False):
        summary["error"] = "An update is already running"
        print("STOCK_MASTER: update already in progress, skipping.", file=sys.stderr)
        return summary

    try:
        if not os.path.exists(csv_path):
            # Rebuilding the master from a sitemap would drop MarketCap,
            # sectors and BSE codes for all ~6k rows. Surface it instead.
            summary["error"] = f"Master CSV not found at {csv_path}"
            print(f"STOCK_MASTER: {summary['error']}", file=sys.stderr)
            return summary

        fieldnames, rows = _read_master_rows(csv_path)
        rows_before = len(rows)
        summary["rows_before"] = summary["rows_after"] = rows_before
        if not fieldnames:
            summary["error"] = "Master CSV has no header row"
            print(f"STOCK_MASTER: {summary['error']}", file=sys.stderr)
            return summary

        print(f"STOCK_MASTER: fetching {url} ...", file=sys.stderr)
        entries = parse_sitemap_entries(fetch_sitemap(url))
        summary["sitemap_entries"] = len(entries)
        print(f"STOCK_MASTER: sitemap listed {len(entries)} equities; "
              f"master holds {rows_before} rows.", file=sys.stderr)

        if len(entries) < MIN_SITEMAP_ENTRIES:
            summary["error"] = (f"Only {len(entries)} equities parsed from the sitemap "
                                f"(expected at least {MIN_SITEMAP_ENTRIES}) - "
                                f"treating as a bad response and leaving the CSV alone")
            print(f"STOCK_MASTER: {summary['error']}", file=sys.stderr)
            return summary

        sitemap_by_id = {tid: (ticker, slug) for tid, ticker, slug in entries}

        # ---- 1. renames on rows we already hold -------------------------
        updates, ambiguous, conflicts = _plan_updates(rows, sitemap_by_id)
        summary["ambiguous"] = ambiguous
        summary["conflicts"] = conflicts
        summary["updates"] = _summarise_updates(updates)
        summary["updated"] = len(updates)

        update_limit = int(rows_before * MAX_UPDATED_ROWS_RATIO)
        if not force and rows_before and len(updates) > update_limit:
            summary["updated"] = 0
            summary["updates"] = []
            summary["error"] = (f"{len(updates)} rows would be rewritten, over the "
                                f"{update_limit}-row safety cap "
                                f"({int(MAX_UPDATED_ROWS_RATIO * 100)}% of the master) - "
                                f"refusing to touch the CSV. Re-run with force=True "
                                f"if Trendlyne really did rename that many")
            print(f"STOCK_MASTER: {summary['error']}", file=sys.stderr)
            return summary

        # ---- 2. brand new listings --------------------------------------
        # Computed against the post-rename symbols, so a listing may legitimately
        # take a ticker another company has just moved off.
        existing_ids = {(r.get(_ID_COL) or "").strip() for r in rows}
        existing_tickers = {t for t in ((r.get(_TICKER_COL) or "").strip().upper()
                                        for r in rows) if t}
        for upd in updates:
            if _TICKER_COL in upd["changes"]:
                old, new = upd["changes"][_TICKER_COL]
                existing_tickers.discard(old.upper())
                existing_tickers.add(new.upper())

        new_rows, added_tickers = [], []
        for tid, ticker, slug in entries:
            ticker_key = ticker.upper()
            if tid in existing_ids or ticker_key in existing_tickers:
                continue
            existing_tickers.add(ticker_key)
            existing_ids.add(tid)
            added_tickers.append(ticker)
            new_rows.append({
                _NAME_COL: slug_to_name(slug),
                _TICKER_COL: ticker,
                _ID_COL: tid,
                # MarketCap intentionally blank: enrich_market_caps() fills it.
                _URL_COL: _report_url(tid, ticker),
            })

        summary["added_tickers"] = added_tickers

        add_limit = int(rows_before * MAX_NEW_ROWS_RATIO)
        if not force and rows_before and len(new_rows) > add_limit:
            summary["error"] = (f"{len(new_rows)} new rows exceeds the {add_limit}-row safety "
                                f"cap ({int(MAX_NEW_ROWS_RATIO * 100)}% of the master) - "
                                f"refusing to append. Re-run with force=True if correct")
            print(f"STOCK_MASTER: {summary['error']}", file=sys.stderr)
            return summary

        if ambiguous:
            print(f"STOCK_MASTER: {len(ambiguous)} rename(s) skipped - several rows "
                  f"share the Trendlyne ID (see the status file).", file=sys.stderr)
        if conflicts:
            print(f"STOCK_MASTER: {len(conflicts)} rename(s) skipped - the new ticker "
                  f"is held by another company (see the status file).", file=sys.stderr)

        if not updates and not new_rows:
            summary["success"] = True
            print("STOCK_MASTER: already in sync - nothing to add or update.",
                  file=sys.stderr)
            return summary

        if dry_run:
            summary["success"] = True
            summary["added"] = len(new_rows)
            print(f"STOCK_MASTER: dry run - would update {len(updates)} row(s) and "
                  f"append {len(new_rows)} row(s).", file=sys.stderr)
            return summary

        for upd in updates:
            row = rows[upd["row"]]
            for col, (_old, new) in upd["changes"].items():
                row[col] = new

        try:
            shutil.copyfile(csv_path, csv_path + ".bak")
        except Exception as e:
            print(f"STOCK_MASTER: backup failed ({e}); continuing.", file=sys.stderr)

        _write_master(csv_path, fieldnames, rows + new_rows)

        summary["success"] = True
        summary["added"] = len(new_rows)
        summary["rows_after"] = rows_before + len(new_rows)
        for upd in summary["updates"][:5]:
            detail = ", ".join(f"{k}: {v}" for k, v in upd.items() if k != "trendlyne_id")
            print(f"STOCK_MASTER:   updated {detail}", file=sys.stderr)
        print(f"STOCK_MASTER: updated {len(updates)} row(s), appended {len(new_rows)} "
              f"new listing(s) ({rows_before} -> {summary['rows_after']} rows).",
              file=sys.stderr)
        return summary

    except Exception as e:
        summary["error"] = str(e)
        print(f"STOCK_MASTER: update failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return summary
    finally:
        summary["duration_seconds"] = round(time.time() - started, 1)
        _UPDATE_LOCK.release()
        if write_status and not dry_run:
            _write_status(summary)


def main():
    dry_run = "--dry-run" in sys.argv
    force = "--force" in sys.argv
    result = update_stock_master(dry_run=dry_run, force=force)
    print(json.dumps({k: v for k, v in result.items() if k != "added_tickers"}, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
