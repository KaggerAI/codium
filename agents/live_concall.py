"""
live_concall.py — Live Conference Call Module for Kagger AI

Enables automated joining, transcription, and real-time streaming of
earnings conference calls.

Workflow:
1. Fetch upcoming concall schedules from IR Pulse Engine API
2. Extract dial-in credentials from NSE/BSE PDF announcements
3. Place Exotel outbound call; passcode entered via the 'number,,,passcode#' tuple
4. Record the call; when ready, transcribe via Gemini (post-call backstop)
5. Optionally stream call audio live (Exotel Stream applet → STT) for a live transcript
6. Broadcast transcript & summary to the frontend via Flask-SocketIO
7. Post-call: aggregate transcript and run Gemini analysis

Telephony provider: Exotel (India-domestic; ~20x cheaper India calls than Twilio).
Env vars: EXOTEL_API_KEY, EXOTEL_API_TOKEN, EXOTEL_SID, EXOTEL_CALLER_ID,
EXOTEL_FLOW_APP_ID (+ optional EXOTEL_SUBDOMAIN / EXOTEL_FLOW_URL).
"""

import os
import sys
import json
import time
import asyncio
import threading
import traceback
import re
from datetime import datetime, timedelta
from io import BytesIO
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

import httpx
import pdfplumber
from flask import request, jsonify, Response

from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
# Exotel (India-domestic telephony). HTTP Basic auth = API key + API token.
EXOTEL_API_KEY = os.getenv("EXOTEL_API_KEY")
EXOTEL_API_TOKEN = os.getenv("EXOTEL_API_TOKEN")
EXOTEL_SID = os.getenv("EXOTEL_SID")                       # Exotel Account SID
EXOTEL_SUBDOMAIN = os.getenv("EXOTEL_SUBDOMAIN", "api.in.exotel.com")
EXOTEL_CALLER_ID = os.getenv("EXOTEL_CALLER_ID")           # ExoPhone (caller ID)
EXOTEL_FLOW_APP_ID = os.getenv("EXOTEL_FLOW_APP_ID")       # App Bazaar flow id
EXOTEL_FLOW_URL = os.getenv("EXOTEL_FLOW_URL")             # optional full flow URL override
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")           # Tier 2: real-time streaming STT

# Tier 2 toggle: when enabled, the Exotel Stream applet forks call audio to our
# WebSocket (/api/live-concall/media-stream) for live transcription instead of
# relying only on the post-call recording. Requires the Stream applet in the
# Exotel flow + Azure WebSockets + a WS-capable worker. Off by default so Tier 1
# (record → post-call transcribe) runs cleanly with no extra infra.
LIVE_STREAM_ENABLED = os.getenv("LIVE_CONCALL_STREAMING", "").strip().lower() in (
    "1", "true", "yes", "on")

IR_PULSE_API = "https://ir-pulse-engine-production.up.railway.app/api/calls"

# Persisted daily schedule snapshot: scraped ONCE per day (8 AM IST) and served
# all day from disk, so opening the Live Concall section never re-scrapes.
# Stored at repo root alongside the other cached_*_results.json files.
SCHEDULE_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "cached_concall_schedule.json",
)
_schedule_mem: Dict[str, Any] = {"calls": [], "generated_at": None}  # in-memory mirror

# Active live sessions (keyed by session_id)
_active_sessions: Dict[str, "LiveConcallSession"] = {}

# Module-level reference to the Gemini API function (set during route registration)
_call_gemini_api_fn = None


# ---------------------------------------------------------------------------
# 1. SCHEDULE FETCHING (IR Pulse Engine API)
# ---------------------------------------------------------------------------

_DATE_FORMATS = ["%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%d-%m-%Y %H:%M", "%Y-%m-%d"]


def _norm_company_name(name: str) -> str:
    """Normalize a company name for cross-source dedup (lowercase, alnum-only,
    drop a trailing 'limited'/'ltd')."""
    s = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    for suf in ("limited", "ltd"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s


def _parse_call_dt(call_date: str, call_time: str):
    """Parse call_date (+ optional call_time) into a datetime, or None."""
    dt_str = f"{call_date or ''} {call_time or ''}".strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    return None


async def _fetch_ir_pulse(days: int = 7, include_past: bool = False) -> List[Dict]:
    """Fetch raw calls from the IR Pulse Engine API; tag source='ir_pulse'.
    Returns [] on any failure (never raises) so it can't break the aggregation."""
    out: List[Dict] = []
    try:
        url = f"{IR_PULSE_API}?include_past={'true' if include_past else 'false'}&days={days}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
        }
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers=headers, timeout=15.0)
            resp.raise_for_status()
        for call in resp.json().get("calls", []):
            out.append({
                **call,
                "call_time": call.get("call_time") or "",
                "announcement_url": call.get("announcement_url") or "",
                "source": "ir_pulse",
            })
    except Exception as e:
        print(f"LIVE_CONCALL: IR Pulse fetch failed: {e}", file=sys.stderr)
    return out


def _merge_calls(calls: List[Dict]) -> List[Dict]:
    """Dedup by (normalized company name, call_date) with a field-union merge:
    prefer a real call_time, an announcement_url, and a ticker from whichever
    source has them. `calls` should be ordered best-source-first (ScanX has exact
    times, so pass it first)."""
    merged: Dict[Any, Dict] = {}
    for c in calls:
        norm = _norm_company_name(c.get("company_name", ""))
        date = c.get("call_date", "")
        if not norm or not date:
            continue  # unusable entry
        key = (norm, date)
        if key not in merged:
            entry = dict(c)
            entry["sources"] = [c.get("source", "")] if c.get("source") else []
            merged[key] = entry
            continue
        existing = merged[key]
        for fld in ("call_time", "announcement_url", "ticker", "scanx_status", "company_name"):
            if not existing.get(fld) and c.get(fld):
                existing[fld] = c[fld]
        src = c.get("source", "")
        if src and src not in existing["sources"]:
            existing["sources"].append(src)
    return list(merged.values())


async def refresh_concall_schedule_cache() -> List[Dict]:
    """
    Scrape ALL sources (IR Pulse + Screener + ScanX) concurrently, merge + dedup,
    persist to cached_concall_schedule.json, and update the in-memory mirror.

    This is the heavy network step — it runs ONCE per day (8 AM IST, wired in
    handler.py) plus a startup-prime if the snapshot is missing/stale. Returns the
    merged calls (raw, un-windowed; status is computed at read time).
    """
    global _schedule_mem
    from fetchers.screener_fetcher import fetch_upcoming_concalls_screener_async
    from fetchers.scanx_fetcher import fetch_upcoming_concalls_scanx_async

    results = await asyncio.gather(
        _fetch_ir_pulse(days=7, include_past=False),
        fetch_upcoming_concalls_screener_async(),
        fetch_upcoming_concalls_scanx_async(),
        return_exceptions=True,
    )
    names = ("ir_pulse", "screener", "scanx")
    src = {}
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            print(f"LIVE_CONCALL: source '{name}' failed: {res}", file=sys.stderr)
            src[name] = []
        else:
            src[name] = res or []

    # ScanX first (it carries exact times), then Screener, then IR Pulse.
    merged = _merge_calls(src["scanx"] + src["screener"] + src["ir_pulse"])
    for c in merged:
        dt = _parse_call_dt(c.get("call_date", ""), c.get("call_time", ""))
        c["parsed_datetime"] = dt.isoformat() if dt else None
    merged.sort(key=lambda c: c.get("parsed_datetime") or "9999")

    payload = {"generated_at": datetime.now().isoformat(), "calls": merged}
    try:
        tmp = SCHEDULE_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, SCHEDULE_CACHE_FILE)
    except Exception as e:
        print(f"LIVE_CONCALL: failed to persist schedule snapshot: {e}", file=sys.stderr)
    _schedule_mem = payload

    print(
        f"LIVE_CONCALL: refreshed schedule - ir_pulse={len(src['ir_pulse'])} "
        f"screener={len(src['screener'])} scanx={len(src['scanx'])} merged={len(merged)}",
        file=sys.stderr,
    )
    return merged


def _run_coro_sync(coro):
    """Run an async coroutine from a synchronous context, whether or not an event
    loop is already running in this thread."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            return ex.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


def load_concall_schedule(days: int = 5) -> List[Dict]:
    """
    Cheap read used by the route + notification watcher. Loads the persisted daily
    snapshot (NO network scrape), recomputes time-relative `status` against now,
    filters to today..+`days`, and sorts soonest-first.

    Falls back to an inline one-shot refresh only if no snapshot exists yet
    (e.g. a fresh deploy before the first 8 AM run).
    """
    global _schedule_mem

    data = _schedule_mem if _schedule_mem.get("calls") else None
    if data is None:
        try:
            if os.path.exists(SCHEDULE_CACHE_FILE):
                with open(SCHEDULE_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    _schedule_mem = data
        except Exception as e:
            print(f"LIVE_CONCALL: failed to read schedule snapshot: {e}", file=sys.stderr)

    calls = (data or {}).get("calls", [])
    if not calls:
        # First run / missing snapshot — populate once inline.
        print("LIVE_CONCALL: no snapshot yet, doing inline refresh", file=sys.stderr)
        try:
            calls = _run_coro_sync(refresh_concall_schedule_cache())
        except Exception as e:
            print(f"LIVE_CONCALL: inline refresh failed: {e}", file=sys.stderr)
            calls = []

    today = datetime.now().date()
    horizon = today + timedelta(days=days)
    now_dt = datetime.now()

    out: List[Dict] = []
    for call in calls:
        call_date = call.get("call_date", "")
        call_time = call.get("call_time", "") or ""
        try:
            d = datetime.strptime(call_date, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            d = None

        # Window filter: keep today..+days (entries with an unparseable date are dropped).
        if d is None or not (today <= d <= horizon):
            continue

        parsed_dt = _parse_call_dt(call_date, call_time)
        if call_time and parsed_dt is not None:
            delta = (parsed_dt - now_dt).total_seconds()
            if delta < 0:
                status = "past"
            elif delta < 900:  # 15 minutes
                status = "starting_soon"
            else:
                status = "upcoming"
        else:
            # Date-only / time-TBD: upcoming if the date is today or later
            # (do NOT mark today's TBD entries 'past' just because midnight elapsed).
            status = "upcoming" if d >= today else "past"

        out.append({
            **call,
            "parsed_datetime": parsed_dt.isoformat() if parsed_dt else None,
            "status": status,
        })

    out.sort(key=lambda c: c.get("parsed_datetime") or "9999")
    return out


async def fetch_concall_schedule(days: int = 5, include_past: bool = False) -> List[Dict]:
    """Backward-compatible async shim. Serves the persisted snapshot filtered to
    `days` — it does NOT scrape (scraping happens once daily via
    refresh_concall_schedule_cache)."""
    return load_concall_schedule(days=days)


# ---------------------------------------------------------------------------
# 2. PDF EXTRACTION — Dial-in Credential Parsing
# ---------------------------------------------------------------------------

async def extract_dialin_from_pdf(pdf_url: str) -> Dict[str, Any]:
    """
    Download a concall announcement PDF and extract dial-in details.
    Strategy:
      1. Download PDF from NSE/BSE archive
      2. Extract text via pdfplumber
      3. Use Gemini to parse structured dial-in info from extracted text
      4. Fallback: Send PDF page image to Gemini Vision if text extraction fails

    Returns dict with:
      phone_number, passcode, pin, date, time, webcast_url, extra_numbers
    """
    empty_result = {
        "phone_number": None, "passcode": None, "pin": None,
        "date": None, "time": None, "webcast_url": None,
        "extra_numbers": [], "raw_text": "", "source_url": pdf_url
    }

    try:
        # Step 1: Download PDF
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/pdf,*/*",
        }

        pdf_bytes = None
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(pdf_url, headers=headers, timeout=20.0)
            if resp.status_code == 200:
                pdf_bytes = resp.content

        if not pdf_bytes:
            # Fallback: try curl_cffi for anti-bot bypassing
            try:
                def _cffi_download():
                    from curl_cffi import requests as cffi_requests
                    session = cffi_requests.Session(impersonate="chrome110")
                    r = session.get(pdf_url, headers=headers,
                                    allow_redirects=True, timeout=20)
                    return r.content if r.status_code == 200 else None
                pdf_bytes = await asyncio.to_thread(_cffi_download)
            except Exception as cffi_err:
                print(f"LIVE_CONCALL: curl_cffi PDF download failed: {cffi_err}",
                      file=sys.stderr)

        if not pdf_bytes:
            print(f"LIVE_CONCALL: Failed to download PDF: {pdf_url}", file=sys.stderr)
            return empty_result

        print(f"LIVE_CONCALL: Downloaded PDF ({len(pdf_bytes)} bytes)", file=sys.stderr)

        # Step 2: Extract text AND hyperlink annotations via pdfplumber
        text = ""
        hyperlink_urls: List[str] = []
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        try:
            with pdfplumber.open(tmp_path) as pdf:
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        text += t + "\n"

                    # Extract embedded hyperlinks from PDF annotations.
                    # DiamondPass links are often "Click Here" buttons with a hidden URI.
                    try:
                        for annot in (page.annots or []):
                            uri = (annot.get("uri") or annot.get("URI")
                                   or annot.get("A", {}).get("URI", ""))
                            if uri and uri.startswith("http"):
                                hyperlink_urls.append(uri)
                    except Exception:
                        pass

                    # pdfplumber >= 0.7 also exposes page.hyperlinks
                    try:
                        for link in (page.hyperlinks or []):
                            uri = link.get("uri", "")
                            if uri and uri.startswith("http"):
                                hyperlink_urls.append(uri)
                    except Exception:
                        pass

        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        # Deduplicate and look for DiamondPass URL immediately
        seen = set()
        unique_links: List[str] = []
        for u in hyperlink_urls:
            u_clean = u.strip().rstrip('/')
            if u_clean not in seen:
                seen.add(u_clean)
                unique_links.append(u_clean)

        diamondpass_url = next(
            (u for u in unique_links if "diamondpass" in u.lower()),
            None
        )
        if diamondpass_url:
            print(f"LIVE_CONCALL: Found DiamondPass URL in PDF hyperlinks: {diamondpass_url}",
                  file=sys.stderr)
        elif unique_links:
            print(f"LIVE_CONCALL: Found {len(unique_links)} hyperlink(s) in PDF: {unique_links[:3]}",
                  file=sys.stderr)

        if len(text.strip()) < 50:
            print("LIVE_CONCALL: PDF text too short, trying Gemini Vision fallback",
                  file=sys.stderr)
            result = await _extract_dialin_gemini_vision(pdf_bytes, pdf_url)
            # Override webcast_url with DiamondPass if vision missed it
            if diamondpass_url and not result.get("webcast_url"):
                result["webcast_url"] = diamondpass_url
            return result

        # Step 3: Parse with Gemini (pass hyperlinks as additional context)
        result = await _parse_dialin_with_gemini(text, pdf_url,
                                                  hyperlink_urls=unique_links)
        # Hard override: if DiamondPass URL found in annotations, always use it
        if diamondpass_url and not result.get("webcast_url"):
            result["webcast_url"] = diamondpass_url
        return result

    except Exception as e:
        print(f"LIVE_CONCALL: PDF extraction error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return empty_result


async def _parse_dialin_with_gemini(text: str, source_url: str,
                                    hyperlink_urls: List[str] = None) -> Dict[str, Any]:
    """Use Gemini to extract structured dial-in info from PDF text."""
    api_key = GOOGLE_API_KEY or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("LIVE_CONCALL: No GOOGLE_API_KEY for Gemini extraction", file=sys.stderr)
        return _regex_fallback_extract(text, source_url)

    # Build hyperlinks context block if we found any from PDF annotations
    hyperlink_context = ""
    if hyperlink_urls:
        hyperlink_context = (
            "\n\nEmbedded hyperlinks found in PDF annotations (these are clickable "
            "links from the PDF that may not appear in the text above):\n"
            + "\n".join(f"  - {u}" for u in hyperlink_urls[:20])
            + "\nIf any of these is a webcast/DiamondPass link, use it as webcast_url."
        )

    extraction_prompt = f"""You are an expert at extracting dial-in details from Indian corporate earnings conference call (concall) announcement PDFs.

Extract ALL dial-in details from the text below and return them as JSON.

Fields to extract:
- phone_number: Primary Indian dial-in number (prefer toll-free or local India number; clean of spaces/dashes, e.g. "+912262801123"). Look for labels like "Dial-in Number", "India Toll", "India Toll Free", "Phone Number".
- passcode: The participant passcode/access code needed to join. This is often labelled "Access Code", "Passcode", "Conference ID", "Conference Password", "Participant Passcode", "Participant Code", "Conference Code", "Entry Code". Extract only the DIGITS (and optional trailing #). Example: "12345678" or "12345678#".
- pin: Separate participant PIN if present (labels: "PIN", "Participant PIN", "Participant Pass Code", "PIN Number"). Often a short 4–6 digit number. Set null if same as passcode or not present.
- date: Date of the call in YYYY-MM-DD format.
- time: Time of the call with timezone, e.g. "16:00 IST" or "4:30 PM IST".
- webcast_url: The URL for joining the call online. This is commonly a DiamondPass link (domain: diamondpass.net), but could also be a Zoom, Teams, Webex, or other meeting URL. In the PDF text it may appear as "Click Here", "Join Webcast", "Webcast Link", or similar — check the embedded hyperlinks section below. Return the full URL.
- extra_numbers: List of alternative dial-in numbers (international or other cities).

IMPORTANT NOTES:
- Indian concalls typically have an "Access Code" that is 8–12 digits. This is NOT the same as the phone number.
- Look for patterns like: "Access Code: 12345678", "Conference ID: 98765432#", "Passcode: 55554321"
- DiamondPass (diamondpass.net) is the most common webcast provider for Indian concalls. Its URLs look like: https://www.diamondpass.net/... or https://app.diamondpass.net/...
- If the document only shows a phone number with no separate code, set passcode to null.
- Return null for any field you cannot find — do NOT guess.

Text:
\"\"\"
{text[:8000]}
\"\"\"{hyperlink_context}
"""

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=extraction_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0
            )
        )

        if response.text:
            parsed = json.loads(response.text)
            # Gemini occasionally wraps the object in an array; unwrap if needed
            if isinstance(parsed, list):
                parsed = parsed[0] if parsed else {}
            if not isinstance(parsed, dict):
                raise ValueError(f"Unexpected Gemini response type: {type(parsed)}")
            parsed["raw_text"] = text[:2000]
            parsed["source_url"] = source_url
            print(f"LIVE_CONCALL: Gemini extracted dial-in: "
                  f"phone={parsed.get('phone_number')}, "
                  f"passcode={parsed.get('passcode')}, pin={parsed.get('pin')}, "
                  f"date={parsed.get('date')}, time={parsed.get('time')}, "
                  f"webcast={parsed.get('webcast_url')}",
                  file=sys.stderr)
            return parsed
    except Exception as e:
        print(f"LIVE_CONCALL: Gemini extraction failed: {e}", file=sys.stderr)

    return _regex_fallback_extract(text, source_url)


async def _extract_dialin_gemini_vision(pdf_bytes: bytes,
                                         source_url: str) -> Dict[str, Any]:
    """Fallback: send first page of PDF as image to Gemini Vision."""
    api_key = GOOGLE_API_KEY or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return {
            "phone_number": None, "passcode": None, "pin": None,
            "date": None, "time": None, "webcast_url": None,
            "extra_numbers": [], "raw_text": "", "source_url": source_url
        }

    try:
        client = genai.Client(api_key=api_key)

        uploaded = client.files.upload(
            file=BytesIO(pdf_bytes),
            config=types.UploadFileConfig(
                display_name="concall_announcement.pdf",
                mime_type="application/pdf"
            )
        )

        # Wait for processing
        wait = 0
        while uploaded.state and str(uploaded.state) == "PROCESSING" and wait < 30:
            time.sleep(1)
            uploaded = client.files.get(name=uploaded.name)
            wait += 1

        prompt = """Extract all dial-in details from this Indian corporate earnings conference call (concall) announcement PDF.

Return JSON with these fields:
- phone_number: Primary India dial-in number (e.g. "+912262801123")
- passcode: Participant access/conference code (labels: "Access Code", "Conference ID", "Passcode", "Conference Password", "Participant Passcode"). Extract digits only + optional trailing #.
- pin: Separate participant PIN if present (short 4-6 digit code, different from passcode). Null if not present.
- date: Call date in YYYY-MM-DD format
- time: Call time with timezone (e.g. "16:00 IST")
- webcast_url: Any webcast/Teams/Zoom URL (null if none)
- extra_numbers: List of alternative dial-in numbers

Set any missing field to null. Do NOT guess."""

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[types.Part.from_text(text=prompt), uploaded],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0
            )
        )

        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass

        if response.text:
            parsed = json.loads(response.text)
            if isinstance(parsed, list):
                parsed = parsed[0] if parsed else {}
            if not isinstance(parsed, dict):
                raise ValueError(f"Unexpected Gemini Vision response type: {type(parsed)}")
            parsed["source_url"] = source_url
            parsed["raw_text"] = ""
            return parsed

    except Exception as e:
        print(f"LIVE_CONCALL: Gemini Vision fallback failed: {e}", file=sys.stderr)

    return {
        "phone_number": None, "passcode": None, "pin": None,
        "date": None, "time": None, "webcast_url": None,
        "extra_numbers": [], "raw_text": "", "source_url": source_url
    }


def _regex_fallback_extract(text: str, source_url: str) -> Dict[str, Any]:
    """Last-resort regex extraction of phone numbers and dates from text."""
    result = {
        "phone_number": None, "passcode": None, "pin": None,
        "date": None, "time": None, "webcast_url": None,
        "extra_numbers": [], "raw_text": text[:2000], "source_url": source_url
    }

    # Phone number patterns (Indian format)
    phone_patterns = [
        r'(?:\+91[\s-]?)?(?:[0-9]{2,4}[\s-]?){2,3}[0-9]{4,6}',
        r'(?:Toll\s*(?:Free)?[\s:]+)(\+?\d[\d\s\-()]+)',
        r'(?:Dial[\s-]?in[\s:]+)(\+?\d[\d\s\-()]+)',
    ]
    for pat in phone_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            num = re.sub(r'[\s\-()]', '', m.group(0) if not m.groups() else m.group(1))
            if len(num) >= 10:
                result["phone_number"] = num
                break

    # Passcode/PIN — covers common Indian concall PDF label variants
    passcode_labels = [
        "access code", "conference id", "conference password", "conference code",
        "participant passcode", "participant code", "entry code", "passcode",
    ]
    pin_labels = ["participant pin", "pin number", "pin"]

    for label in passcode_labels:
        m = re.search(rf'{re.escape(label)}\s*[:#\-]?\s*(\d[\d\s#*]*)',
                      text, re.IGNORECASE)
        if m:
            val = re.sub(r'\s+', '', m.group(1)).rstrip('#') + '#'
            if len(val) >= 5:  # must be at least 4 digits + #
                result["passcode"] = val
                break

    for label in pin_labels:
        m = re.search(rf'{re.escape(label)}\s*[:#\-]?\s*(\d[\d\s]*)',
                      text, re.IGNORECASE)
        if m:
            val = re.sub(r'\s+', '', m.group(1))
            if len(val) >= 4:
                result["pin"] = val
                break

    # Date
    date_patterns = [
        r'(\d{1,2})\s*(January|February|March|April|May|June|July|August|'
        r'September|October|November|December)\s*,?\s*(\d{4})',
        r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})',
    ]
    for pat in date_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                result["date"] = m.group(0)
            except Exception:
                pass
            break

    # Time
    m = re.search(r'(\d{1,2}[:.]\d{2})\s*(AM|PM|IST|hrs?|hours?)?',
                  text, re.IGNORECASE)
    if m:
        result["time"] = m.group(0).strip()

    # Webcast URL
    m = re.search(r'(https?://\S+)', text)
    if m:
        url = m.group(1).rstrip('.')
        if 'webcast' in url.lower() or 'meet' in url.lower() or 'zoom' in url.lower():
            result["webcast_url"] = url

    return result


# ---------------------------------------------------------------------------
# 3. EXOTEL INTEGRATION
# ---------------------------------------------------------------------------
# Exotel is an India-domestic CPaaS (cheap India termination). We dial the
# conference bridge via the Calls/connect API, enter the passcode using the
# post-dial "number,,,passcode#" DTMF tuple, and run an App Bazaar flow
# (Record + Passthru, plus an optional Stream applet for live transcription).
# Call status + RecordingUrl arrive on /api/live-concall/status. Plain REST.

# Maps an Exotel CallSid -> our session_id. Set when the call is placed; used to
# correlate the Stream applet's WebSocket and the /status callbacks back to a
# session. Pruned when the session ends.
_callsid_to_session: Dict[str, str] = {}


def _exotel_base() -> str:
    """Build the authenticated Exotel REST base URL."""
    key = EXOTEL_API_KEY or os.getenv("EXOTEL_API_KEY")
    token = EXOTEL_API_TOKEN or os.getenv("EXOTEL_API_TOKEN")
    sid = EXOTEL_SID or os.getenv("EXOTEL_SID")
    subdomain = (EXOTEL_SUBDOMAIN or os.getenv("EXOTEL_SUBDOMAIN")
                 or "api.in.exotel.com")
    if not (key and token and sid):
        raise RuntimeError("EXOTEL_API_KEY, EXOTEL_API_TOKEN and EXOTEL_SID must be set")
    return f"https://{key}:{token}@{subdomain}/v1/Accounts/{sid}"


def _format_phone_exotel(phone_number: str) -> str:
    """
    Normalize a dial-in number to Exotel's national format. Exotel expects a
    leading 0 for Indian numbers (NOT E.164 '+91').
    """
    p = re.sub(r'[^\d+]', '', phone_number or "")
    if p.startswith('+91'):
        p = p[3:]
    elif p.startswith('91') and len(p) >= 12:
        p = p[2:]
    p = p.lstrip('+')
    if p and not p.startswith('0'):
        p = '0' + p
    return p


def _build_dial_string(phone_number: str, passcode: str = "",
                       pin: str = "") -> str:
    """
    Build Exotel's 'number,,,passcode#' post-dial DTMF tuple so the passcode is
    keyed in automatically after the bridge answers (commas insert pauses).
    """
    number = _format_phone_exotel(phone_number)
    digits = ""
    clean_passcode = re.sub(r'[^0-9#*]', '', passcode or "")
    if clean_passcode:
        digits = clean_passcode if clean_passcode.endswith('#') else clean_passcode + '#'
    clean_pin = re.sub(r'[^0-9#*]', '', pin or "")
    if clean_pin:
        digits += clean_pin if clean_pin.endswith('#') else clean_pin + '#'
    if digits:
        # 3 commas ≈ a few seconds of pause to let the bridge prompt play first.
        return f"{number},,,{digits}"
    return number


def place_exotel_call(phone_number: str, passcode: str = "",
                      pin: str = "", session_id: str = "",
                      webhook_base_url: str = "") -> Dict[str, Any]:
    """
    Place an outbound Exotel call to a conference bridge and run our App flow.

    The passcode is entered via the post-dial DTMF tuple in 'From'. The flow
    (configured once in the Exotel dashboard) records the call, optionally
    streams audio to /media-stream for live transcription, and POSTs status +
    the RecordingUrl to /status. Calls/connect returns the CallSid synchronously.

    Returns {call_sid, status, to} or {"error": ...} on failure.
    """
    try:
        caller_id = EXOTEL_CALLER_ID or os.getenv("EXOTEL_CALLER_ID")
        app_id = EXOTEL_FLOW_APP_ID or os.getenv("EXOTEL_FLOW_APP_ID")
        sid = EXOTEL_SID or os.getenv("EXOTEL_SID")
        if not caller_id:
            raise RuntimeError("EXOTEL_CALLER_ID (ExoPhone) not set")
        flow_url = (EXOTEL_FLOW_URL or os.getenv("EXOTEL_FLOW_URL")
                    or (f"http://my.exotel.com/{sid}/exoml/start_voice/{app_id}"
                        if app_id else ""))
        if not flow_url:
            raise RuntimeError("EXOTEL_FLOW_APP_ID or EXOTEL_FLOW_URL not set")

        dial_string = _build_dial_string(phone_number, passcode, pin)

        if session_id and session_id in _active_sessions:
            sess = _active_sessions[session_id]
            sess.dial_number = dial_string
            if webhook_base_url:
                sess.webhook_base = webhook_base_url

        status_cb = f"{webhook_base_url}/api/live-concall/status?session_id={session_id}"
        form = {
            "From": dial_string,
            "CallerId": caller_id,
            "CallType": "trans",
            "Url": flow_url,
            "TimeLimit": "7200",
            "StatusCallback": status_cb,
            "CustomField": session_id,
        }

        resp = httpx.post(f"{_exotel_base()}/Calls/connect.json", data=form,
                          timeout=30)
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        call_sid = (((body or {}).get("Call") or {}).get("Sid")) or ""

        if call_sid:
            _callsid_to_session[call_sid] = session_id
            if session_id and session_id in _active_sessions:
                _active_sessions[session_id].call_sid = call_sid

        print(f"LIVE_CONCALL: Exotel call placed: CallSid={call_sid}, "
              f"to={dial_string}", file=sys.stderr)

        return {
            "call_sid": call_sid,
            "status": "queued",
            "to": dial_string,
            "session_id": session_id,
        }

    except Exception as e:
        print(f"LIVE_CONCALL: Exotel call failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return {"error": str(e), "session_id": session_id}


def inject_dtmf(call_sid: str, digits: str) -> Dict[str, Any]:
    """
    Not supported on Exotel: there is no documented API to inject DTMF into an
    active call. The passcode is entered automatically at dial time via the
    'number,,,passcode#' tuple in place_exotel_call().
    """
    return {"error": "DTMF injection is not supported on Exotel; the passcode is "
                     "entered automatically when the call is placed."}


def end_exotel_call(call_sid: str) -> Dict[str, Any]:
    """Hang up an active Exotel call by setting its status to completed."""
    try:
        resp = httpx.post(f"{_exotel_base()}/Calls/{call_sid}.json",
                          data={"Status": "completed"}, timeout=30)
        ok = resp.status_code < 400
        print(f"LIVE_CONCALL: End call {call_sid}: HTTP {resp.status_code}",
              file=sys.stderr)
        return {"status": "completed" if ok else "error", "call_sid": call_sid}
    except Exception as e:
        print(f"LIVE_CONCALL: End call failed: {e}", file=sys.stderr)
        return {"error": str(e)}


# --- Tier 2: live transcription from the Exotel Stream applet ----------------

def _resolve_stream_session(start_obj: Dict[str, Any]):
    """Correlate a Stream 'start' event to a LiveConcallSession."""
    start_obj = start_obj or {}
    call_sid = start_obj.get("call_sid", "")
    sid = _callsid_to_session.get(call_sid, "")
    if not sid:
        custom = start_obj.get("custom_parameters") or {}
        sid = custom.get("session_id", "")
    session = _active_sessions.get(sid) if sid else None
    if session is None:
        # Last resort: if exactly one call is live, attribute the stream to it.
        live = [s for s in _active_sessions.values()
                if s.state in ("dialing", "connected", "recording")]
        if len(live) == 1:
            session = live[0]
    return session


def _handle_media_stream(ws):
    """
    Handle the raw WebSocket opened by Exotel's Stream applet: decode PCM media
    frames, stream them to the STT engine, and push transcripts into the
    session. Listen-only — we never send audio back. Fully isolated/defensive so
    a failure here never affects the call, recording, or the rest of the app.
    """
    import base64
    session = None
    stt = None
    try:
        from agents.live_concall_stt import open_stt_stream
    except Exception as e:
        print(f"LIVE_CONCALL: STT unavailable, media-stream is a no-op ({e})",
              file=sys.stderr)
        open_stt_stream = None

    try:
        while True:
            msg = ws.receive()
            if msg is None:
                break
            try:
                evt = json.loads(msg)
            except Exception:
                continue
            etype = evt.get("event")
            if etype == "start":
                session = _resolve_stream_session(evt.get("start") or evt)
                if session and open_stt_stream:
                    stt = open_stt_stream(
                        lambda text, final: session.add_transcript(text, is_final=final))
                print("LIVE_CONCALL: media-stream started (session="
                      f"{session.session_id if session else None})", file=sys.stderr)
            elif etype == "media":
                if stt:
                    payload = (evt.get("media") or {}).get("payload", "")
                    if payload:
                        try:
                            stt.send(base64.b64decode(payload))
                        except Exception:
                            pass
            elif etype == "stop":
                break
    except Exception as e:
        print(f"LIVE_CONCALL: media-stream error: {e}", file=sys.stderr)
    finally:
        if stt:
            try:
                stt.finish()
            except Exception:
                pass
        print("LIVE_CONCALL: media-stream closed", file=sys.stderr)


# ---------------------------------------------------------------------------
# 4. LIVE CONCALL SESSION
# ---------------------------------------------------------------------------

@dataclass
class LiveConcallSession:
    """
    State machine for a single live concall session.
    Tracks call state, transcript chunks, and manages the session lifecycle.
    """
    session_id: str
    company_name: str = ""
    ticker: str = ""
    call_sid: str = ""
    dial_number: str = ""
    send_digits: str = ""

    # State
    state: str = "created"  # created → dialing → connected → recording → ended → summarizing → complete
    started_at: float = 0.0
    connected_at: float = 0.0
    ended_at: float = 0.0

    # Transcript
    transcript_chunks: List[str] = field(default_factory=list)
    full_transcript: str = ""
    chunk_count: int = 0

    # Recording
    recording_url: str = ""

    # Analysis
    analysis_result: str = ""

    # Call tracking / webhook routing
    request_uuid: str = ""   # (legacy) provider request id; unused with Exotel
    webhook_base: str = ""   # public base URL Exotel calls back to

    # Callback for SocketIO emit
    _emit_fn: Any = None

    def update_state(self, new_state: str):
        """Update session state and emit to frontend."""
        old = self.state
        self.state = new_state
        print(f"LIVE_CONCALL: Session {self.session_id} "
              f"state: {old} → {new_state}", file=sys.stderr)
        self._emit("live_concall_state", {
            "session_id": self.session_id,
            "state": new_state,
            "company": self.company_name,
            "ticker": self.ticker,
            "call_sid": self.call_sid,
        })

    def add_transcript(self, text: str, is_final: bool = False):
        """Add a transcript chunk and emit to frontend."""
        if not text or text.strip() in ("[SILENCE]", "[UNCLEAR]", ""):
            return

        self.transcript_chunks.append(text)
        self.full_transcript += " " + text
        self.chunk_count += 1

        self._emit("live_concall_transcript", {
            "session_id": self.session_id,
            "text": text,
            "is_final": is_final,
            "chunk_id": self.chunk_count,
            "total_chars": len(self.full_transcript),
        })

    def get_duration(self) -> int:
        """Get call duration in seconds."""
        if not self.connected_at:
            return 0
        end = self.ended_at or time.time()
        return int(end - self.connected_at)

    def get_state_dict(self) -> Dict[str, Any]:
        """Serialize session state for API responses."""
        return {
            "session_id": self.session_id,
            "company_name": self.company_name,
            "ticker": self.ticker,
            "state": self.state,
            "call_sid": self.call_sid,
            "duration": self.get_duration(),
            "transcript_length": len(self.full_transcript),
            "chunk_count": self.chunk_count,
            "recording_url": self.recording_url,
        }

    def _emit(self, event: str, data: Dict):
        """Emit via SocketIO if callback set."""
        if self._emit_fn:
            try:
                self._emit_fn(event, data)
            except Exception as e:
                print(f"LIVE_CONCALL: Emit error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 5. POST-CALL TRANSCRIPTION (Exotel Recording → Gemini)
# ---------------------------------------------------------------------------

def _transcribe_recording_with_gemini(session: LiveConcallSession,
                                       recording_url: str) -> str:
    """
    Download an Exotel recording and transcribe it with Gemini Files API.
    Exotel recording URLs are usually public; we retry with HTTP Basic auth if
    the download is blocked. Returns transcript text or empty string on failure.
    """
    api_key = GOOGLE_API_KEY or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("LIVE_CONCALL: No GOOGLE_API_KEY for recording transcription",
              file=sys.stderr)
        return ""

    try:
        # Ensure URL ends with .mp3 for easy download
        dl_url = recording_url
        if not any(dl_url.endswith(ext) for ext in ['.mp3', '.wav']):
            dl_url = dl_url + '.mp3'

        print(f"LIVE_CONCALL: Downloading recording from {dl_url}", file=sys.stderr)

        import requests as _requests
        resp = _requests.get(dl_url, timeout=120)
        # Exotel recording URLs may require HTTP Basic auth — retry if blocked.
        if resp.status_code in (401, 403):
            key = EXOTEL_API_KEY or os.getenv("EXOTEL_API_KEY")
            token = EXOTEL_API_TOKEN or os.getenv("EXOTEL_API_TOKEN")
            if key and token:
                resp = _requests.get(dl_url, auth=(key, token), timeout=120)
        if resp.status_code != 200:
            print(f"LIVE_CONCALL: Recording download failed: HTTP {resp.status_code}",
                  file=sys.stderr)
            return ""

        audio_bytes = resp.content
        file_size_mb = len(audio_bytes) / (1024 * 1024)
        print(f"LIVE_CONCALL: Downloaded recording ({file_size_mb:.1f} MB)",
              file=sys.stderr)

        if len(audio_bytes) < 10000:
            print("LIVE_CONCALL: Recording too small, likely no audio captured",
                  file=sys.stderr)
            return ""

        # Upload to Gemini and transcribe
        client = genai.Client(api_key=api_key)
        uploaded = client.files.upload(
            file=BytesIO(audio_bytes),
            config=types.UploadFileConfig(
                display_name=f"concall_recording_{session.session_id}.mp3",
                mime_type="audio/mpeg"
            )
        )

        # Wait for processing
        wait = 0
        while uploaded.state and str(uploaded.state) == "PROCESSING" and wait < 60:
            time.sleep(2)
            uploaded = client.files.get(name=uploaded.name)
            wait += 1

        prompt = """You are a financial transcription specialist. This audio is from an earnings conference call (concall) for a publicly traded Indian company.

Transcribe ALL spoken words accurately. Preserve speaker attributions where possible (e.g., "Management:", "Analyst:", "Moderator:"). Capture all financial figures, percentages, and guidance numbers precisely.

DO NOT summarize — transcribe only.
Return ONLY the transcript text."""

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[types.Part.from_text(text=prompt), uploaded]
        )

        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass

        transcript = response.text.strip() if response.text else ""
        if transcript:
            print(f"LIVE_CONCALL: Transcribed {len(transcript)} chars from recording",
                  file=sys.stderr)
        return transcript

    except Exception as e:
        print(f"LIVE_CONCALL: Recording transcription failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# 6. POST-CALL SUMMARIZATION
# ---------------------------------------------------------------------------

def _generate_post_call_summary(session: LiveConcallSession,
                                 call_gemini_api_fn) -> str:
    """Generate AI analysis from the full live transcript."""
    if not session.full_transcript or len(session.full_transcript.strip()) < 200:
        return ""

    from agents.prompts.concall_prompts import CONCALL_ANALYSIS_PROMPT

    prompt = f"""{CONCALL_ANALYSIS_PROMPT}

## Company: {session.ticker} ({session.company_name})
## Transcript Source: Live Concall — Auto-Transcribed ({session.get_duration()}s duration)
## Full Transcript:
{session.full_transcript}
"""

    try:
        messages = [{"role": "user", "content": prompt}]
        result = call_gemini_api_fn(
            messages,
            model="gemini-2.5-flash-preview-05-20",
            temperature=1,
            thinking_level="HIGH"
        )
        return result
    except Exception as e:
        print(f"LIVE_CONCALL: Post-call summary failed: {e}", file=sys.stderr)
        return ""


def _run_post_call_pipeline(session: LiveConcallSession,
                              call_gemini_api_fn,
                              recording_url: str = ""):
    """
    Full post-call pipeline: transcribe recording (if needed) → summarize.
    Runs in a background thread.
    """
    session_id = session.session_id

    # Step 1: Transcribe recording if we don't have enough transcript text
    if recording_url and len(session.full_transcript.strip()) < 200:
        session._emit("live_concall_state", {
            "session_id": session_id,
            "state": "transcribing",
            "company": session.company_name,
            "ticker": session.ticker,
            "call_sid": session.call_sid,
        })
        transcript = _transcribe_recording_with_gemini(session, recording_url)
        if transcript:
            session.full_transcript = transcript
            session.add_transcript(transcript, is_final=True)

    # Step 2: Summarize
    if not session.full_transcript or len(session.full_transcript.strip()) < 200:
        session.update_state("complete")
        session._emit("live_concall_summary", {
            "session_id": session_id,
            "analysis": "",
            "error": "Transcript too short for analysis. "
                     "The call may have failed to connect or had no audio.",
            "duration": session.get_duration(),
            "transcript_length": len(session.full_transcript),
        })
        return

    session.update_state("summarizing")
    result = _generate_post_call_summary(session, call_gemini_api_fn)
    session.analysis_result = result
    session.update_state("complete")

    # Persist result for standard concall agent retrieval
    try:
        from agents.base import store_latest_result
        store_latest_result('concall', session.ticker, {
            'analysis': result,
            'ticker': session.ticker,
            'concall_label': f"Live Concall — {session.company_name}",
            'concall_link': session.recording_url or '',
            'transcript_text': session.full_transcript,
            'analyzed_at': time.time(),
            'source': 'live_concall',
            'duration_seconds': session.get_duration(),
        })
    except Exception as e:
        print(f"LIVE_CONCALL: Failed to store result: {e}", file=sys.stderr)

    session._emit("live_concall_summary", {
        "session_id": session_id,
        "analysis": result,
        "duration": session.get_duration(),
        "transcript_length": len(session.full_transcript),
    })


# ---------------------------------------------------------------------------
# 7. FLASK ROUTE REGISTRATION
# ---------------------------------------------------------------------------

def register_live_concall_routes(app, socketio, call_gemini_api_fn):
    """
    Register all Live Concall REST endpoints.

    Endpoints:
      GET  /api/live-concall/schedule       — Get upcoming concall schedule
      POST /api/live-concall/extract-dialin  — Extract dial-in from PDF URL
      POST /api/live-concall/start          — Start a live session (dial out)
      POST /api/live-concall/inject-dtmf    — (Exotel: not supported)
      POST /api/live-concall/end            — End active call & summarize
      GET  /api/live-concall/session/<id>   — Get session state
      GET/POST /api/live-concall/status     — Exotel Passthru/StatusCallback (lifecycle + recording)
      WS   /api/live-concall/media-stream   — Exotel Stream applet audio (Tier 2)
    """
    global _call_gemini_api_fn
    _call_gemini_api_fn = call_gemini_api_fn

    # --- Schedule ---
    @app.route('/api/live-concall/schedule', methods=['GET'])
    def live_concall_schedule():
        """Return the upcoming concall schedule for the next `days` days.
        Serves the persisted daily snapshot (no scrape on open)."""
        try:
            days = int(request.args.get('days', 5))
            calls = load_concall_schedule(days=days)
            return jsonify({"status": "ok", "calls": calls, "count": len(calls)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- Extract Dial-in ---
    @app.route('/api/live-concall/extract-dialin', methods=['POST'])
    def live_concall_extract_dialin():
        """Extract dial-in details from a PDF announcement URL."""
        try:
            data = request.get_json(force=True)
            pdf_url = data.get('pdf_url', '').strip()
            if not pdf_url:
                return jsonify({"error": "pdf_url is required"}), 400

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(extract_dialin_from_pdf(pdf_url))
            finally:
                # Drain any pending tasks (e.g. genai.Client's internal aclose)
                # before closing the loop to prevent RuntimeWarning noise.
                pending = asyncio.all_tasks(loop)
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.close()

            return jsonify({"status": "ok", "dialin": result})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- Start Session ---
    @app.route('/api/live-concall/start', methods=['POST'])
    def live_concall_start():
        """Start a live concall session — place Exotel call."""
        try:
            data = request.get_json(force=True)
            phone_number = data.get('phone_number', '').strip()
            passcode = data.get('passcode', '').strip()
            pin = data.get('pin', '').strip()
            company_name = data.get('company_name', '').strip()
            ticker = data.get('ticker', '').strip().upper()

            if not phone_number:
                return jsonify({"error": "phone_number is required"}), 400

            # Check Exotel credentials before starting
            if not (EXOTEL_API_KEY or os.getenv("EXOTEL_API_KEY")):
                return jsonify({"error": "Exotel not configured. EXOTEL_API_KEY is "
                                         "missing. Set EXOTEL_API_KEY, "
                                         "EXOTEL_API_TOKEN, EXOTEL_SID, "
                                         "EXOTEL_CALLER_ID and EXOTEL_FLOW_APP_ID "
                                         "in the environment."}), 503
            if not (EXOTEL_API_TOKEN or os.getenv("EXOTEL_API_TOKEN")):
                return jsonify({"error": "Exotel not configured. "
                                         "EXOTEL_API_TOKEN is missing."}), 503
            if not (EXOTEL_SID or os.getenv("EXOTEL_SID")):
                return jsonify({"error": "Exotel not configured. "
                                         "EXOTEL_SID is missing."}), 503
            if not (EXOTEL_CALLER_ID or os.getenv("EXOTEL_CALLER_ID")):
                return jsonify({"error": "Exotel not configured. "
                                         "EXOTEL_CALLER_ID (ExoPhone) is missing."}), 503
            if not (EXOTEL_FLOW_APP_ID or os.getenv("EXOTEL_FLOW_APP_ID")
                    or EXOTEL_FLOW_URL or os.getenv("EXOTEL_FLOW_URL")):
                return jsonify({"error": "Exotel not configured. "
                                         "EXOTEL_FLOW_APP_ID is missing."}), 503

            # Create session
            session_id = f"lc_{int(time.time())}_{ticker or 'UNKNOWN'}"
            session = LiveConcallSession(
                session_id=session_id,
                company_name=company_name,
                ticker=ticker,
                started_at=time.time(),
            )
            session._emit_fn = lambda evt, d: socketio.emit(evt, d)
            _active_sessions[session_id] = session

            # Determine webhook base URL (Exotel calls back to this public host)
            webhook_base = data.get('webhook_base_url', '').strip()
            if not webhook_base:
                webhook_base = request.host_url.rstrip('/')
            session.webhook_base = webhook_base

            session.update_state("dialing")

            # Place call in background thread (place_exotel_call sets call_sid)
            def _dial():
                result = place_exotel_call(
                    phone_number, passcode, pin,
                    session_id, webhook_base
                )
                if "error" in result:
                    session.update_state("error")
                    session._emit("live_concall_error", {
                        "session_id": session_id,
                        "error": result["error"]
                    })

            thread = threading.Thread(target=_dial, daemon=True)
            thread.start()

            return jsonify({
                "status": "ok",
                "session_id": session_id,
                "message": f"Dialing {phone_number}..."
            })

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- Inject DTMF ---
    @app.route('/api/live-concall/inject-dtmf', methods=['POST'])
    def live_concall_inject_dtmf():
        """Send DTMF digits to active call."""
        try:
            data = request.get_json(force=True)
            session_id = data.get('session_id', '')
            digits = data.get('digits', '')

            session = _active_sessions.get(session_id)
            if not session:
                return jsonify({"error": "Session not found"}), 404
            if not session.call_sid:
                return jsonify({"error": "No active call"}), 400

            result = inject_dtmf(session.call_sid, digits)
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- End Call ---
    @app.route('/api/live-concall/end', methods=['POST'])
    def live_concall_end():
        """End the active call and trigger summarization."""
        try:
            data = request.get_json(force=True)
            session_id = data.get('session_id', '')

            session = _active_sessions.get(session_id)
            if not session:
                return jsonify({"error": "Session not found"}), 404

            session.ended_at = time.time()

            # End Exotel call
            if session.call_sid:
                end_exotel_call(session.call_sid)

            # If we already have a recording URL, kick off the full pipeline now.
            # If not, the /status webhook will trigger it when Exotel delivers it.
            if session.recording_url:
                thread = threading.Thread(
                    target=_run_post_call_pipeline,
                    args=(session, call_gemini_api_fn, session.recording_url),
                    daemon=True
                )
                thread.start()
            else:
                # Mark as ended; recording webhook will continue the pipeline
                session.update_state("ended")

            return jsonify({
                "status": "ok",
                "session_id": session_id,
                "duration": session.get_duration(),
                "message": "Call ended. Transcript will be generated when recording is ready."
            })

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- Get Session State ---
    @app.route('/api/live-concall/session/<session_id>', methods=['GET'])
    def live_concall_session_status(session_id):
        """Get current session state."""
        session = _active_sessions.get(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        return jsonify(session.get_state_dict())

    # --- List Active Sessions ---
    @app.route('/api/live-concall/sessions', methods=['GET'])
    def live_concall_sessions():
        """List all active sessions."""
        return jsonify({
            "sessions": [s.get_state_dict() for s in _active_sessions.values()]
        })

    # --- Status / Recording Webhook (Exotel Passthru + StatusCallback) ---
    @app.route('/api/live-concall/status', methods=['GET', 'POST'])
    def live_concall_status():
        """
        Exotel Passthru / StatusCallback receiver: drives the call lifecycle and
        kicks off post-call transcription when the RecordingUrl arrives. Exotel
        may call this via GET (Passthru) or POST (StatusCallback).
        """
        vals = request.values
        call_sid = vals.get('CallSid', '')
        session_id = (request.args.get('session_id', '')
                      or vals.get('CustomField', '')
                      or _callsid_to_session.get(call_sid, ''))
        call_status = (vals.get('Status', '') or vals.get('CallStatus', '')).lower()
        recording_url = vals.get('RecordingUrl', '')

        print(f"LIVE_CONCALL: Exotel status: status={call_status} "
              f"CallSid={call_sid} rec={'yes' if recording_url else 'no'} "
              f"(session={session_id})", file=sys.stderr)

        session = _active_sessions.get(session_id)
        if not session:
            return Response("OK", status=200)

        if call_sid and not session.call_sid:
            session.call_sid = call_sid
            _callsid_to_session[call_sid] = session_id

        # Lifecycle transitions
        if call_status in ('in-progress', 'in_progress', 'answered', 'connected'):
            if not session.connected_at:
                session.connected_at = time.time()
            if session.state in ('created', 'dialing'):
                session.update_state("recording")
        elif call_status in ('failed', 'busy', 'no-answer', 'no_answer',
                             'canceled', 'cancelled'):
            session.ended_at = time.time()
            if session.state not in ('summarizing', 'transcribing', 'complete'):
                session.update_state("error")
                session._emit("live_concall_error", {
                    "session_id": session_id,
                    "error": f"Call {call_status}. "
                             "Please check the dial-in number and passcode.",
                })
        elif call_status in ('completed', 'complete'):
            if not session.ended_at:
                session.ended_at = time.time()
            if session.state not in ('summarizing', 'transcribing',
                                     'complete', 'ended'):
                session.update_state("ended")

        # Recording ready → run the post-call pipeline (Tier-1 transcript / backstop)
        if recording_url and session.state not in ('summarizing', 'transcribing',
                                                    'complete'):
            session.recording_url = recording_url
            session._emit("live_concall_recording", {
                "session_id": session_id,
                "recording_url": recording_url,
            })
            threading.Thread(
                target=_run_post_call_pipeline,
                args=(session, call_gemini_api_fn, recording_url),
                daemon=True,
            ).start()

        return Response("OK", status=200)

    # --- Live audio WebSocket (Exotel Stream applet → STT) — Tier 2, gated ---
    # Registered only when LIVE_CONCALL_STREAMING is on, and guarded so a missing
    # flask-sock package (or any error) can never block app startup.
    if LIVE_STREAM_ENABLED:
        try:
            from flask_sock import Sock
            _sock = Sock(app)

            @_sock.route('/api/live-concall/media-stream')
            def live_concall_media_stream(ws):
                _handle_media_stream(ws)

            print("LIVE_CONCALL: ✓ media-stream WebSocket registered",
                  file=sys.stderr)
        except Exception as e:
            print(f"LIVE_CONCALL: media-stream WS NOT registered ({e})",
                  file=sys.stderr)

    print("LIVE_CONCALL: ✓ Live Concall routes registered", file=sys.stderr)
