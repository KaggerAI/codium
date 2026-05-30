"""
live_concall.py — Live Conference Call Module for Kagger AI

Enables automated joining, transcription, and real-time streaming of
earnings conference calls.

Workflow:
1. Fetch upcoming concall schedules from IR Pulse Engine API
2. Extract dial-in credentials from NSE/BSE PDF announcements
3. Place Twilio outbound call with DTMF passcode injection
4. Record the call; when recording is ready, transcribe via Gemini
5. Broadcast transcript & summary to frontend via Flask-SocketIO
6. Post-call: aggregate transcript and run Gemini analysis
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
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

IR_PULSE_API = "https://ir-pulse-engine-production.up.railway.app/api/calls"

# Cache for schedule data (refreshed every 30 min)
_schedule_cache: Dict[str, Any] = {"calls": [], "fetched_at": 0}
SCHEDULE_CACHE_TTL = 1800  # 30 minutes

# Active live sessions (keyed by session_id)
_active_sessions: Dict[str, "LiveConcallSession"] = {}

# Module-level reference to the Gemini API function (set during route registration)
_call_gemini_api_fn = None


# ---------------------------------------------------------------------------
# 1. SCHEDULE FETCHING (IR Pulse Engine API)
# ---------------------------------------------------------------------------

async def fetch_concall_schedule(days: int = 30, include_past: bool = False) -> List[Dict]:
    """
    Fetch upcoming concall schedule from IR Pulse Engine.
    Returns list of call dicts with company_name, call_date, call_time, announcement_url.
    Uses an in-memory cache with 30-minute TTL.
    """
    global _schedule_cache

    now = time.time()
    if _schedule_cache["calls"] and (now - _schedule_cache["fetched_at"]) < SCHEDULE_CACHE_TTL:
        print("LIVE_CONCALL: Returning cached schedule", file=sys.stderr)
        return _schedule_cache["calls"]

    try:
        url = f"{IR_PULSE_API}?include_past={'true' if include_past else 'false'}&days={days}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
        }

        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, headers=headers, timeout=15.0)
            resp.raise_for_status()

        data = resp.json()
        calls = data.get("calls", [])

        # Enrich each call with parsed datetime and status
        enriched = []
        for call in calls:
            try:
                call_date = call.get("call_date", "")
                call_time = call.get("call_time", "")
                dt_str = f"{call_date} {call_time}".strip()

                parsed_dt = None
                for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p",
                            "%d-%m-%Y %H:%M", "%Y-%m-%d"]:
                    try:
                        parsed_dt = datetime.strptime(dt_str, fmt)
                        break
                    except ValueError:
                        continue

                now_dt = datetime.now()
                status = "upcoming"
                if parsed_dt:
                    delta = parsed_dt - now_dt
                    if delta.total_seconds() < 0:
                        status = "past"
                    elif delta.total_seconds() < 900:  # 15 minutes
                        status = "starting_soon"

                enriched.append({
                    **call,
                    "parsed_datetime": parsed_dt.isoformat() if parsed_dt else None,
                    "status": status,
                })
            except Exception:
                enriched.append({**call, "parsed_datetime": None, "status": "unknown"})

        # Sort by date (soonest first)
        enriched.sort(
            key=lambda c: c.get("parsed_datetime") or "9999",
        )

        _schedule_cache = {"calls": enriched, "fetched_at": now}
        print(f"LIVE_CONCALL: Fetched {len(enriched)} calls from IR Pulse Engine",
              file=sys.stderr)
        return enriched

    except Exception as e:
        print(f"LIVE_CONCALL: Schedule fetch failed: {e}", file=sys.stderr)
        return _schedule_cache.get("calls", [])


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
# 3. TWILIO INTEGRATION
# ---------------------------------------------------------------------------

def _get_twilio_client():
    """Lazy-load Twilio client."""
    sid = TWILIO_ACCOUNT_SID or os.getenv("TWILIO_ACCOUNT_SID")
    token = TWILIO_AUTH_TOKEN or os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")
    from twilio.rest import Client
    return Client(sid, token)


def place_twilio_call(phone_number: str, passcode: str = "",
                      pin: str = "", session_id: str = "",
                      webhook_base_url: str = "") -> Dict[str, Any]:
    """
    Place an outbound Twilio call to a conference bridge.

    Args:
        phone_number: Dial-in number (e.g. +912262801123)
        passcode: Conference passcode (digits + optional # at end)
        pin: Participant PIN (if separate from passcode)
        session_id: Unique session identifier for tracking
        webhook_base_url: Base URL for Twilio callbacks (e.g. https://your-server.com)

    Returns:
        Dict with call_sid, status, etc.
    """
    try:
        client = _get_twilio_client()

        # Build TwiML URL — Twilio will fetch this to get call instructions
        twiml_url = f"{webhook_base_url}/api/live-concall/twiml?session_id={session_id}"
        status_url = f"{webhook_base_url}/api/live-concall/call-status?session_id={session_id}"

        # Build send_digits string with pauses
        send_digits = ""
        if passcode:
            clean_passcode = re.sub(r'[^0-9#*]', '', passcode)
            send_digits = f"wwwwww{clean_passcode}"  # 3-second pause then digits
            if not clean_passcode.endswith('#'):
                send_digits += '#'
        if pin:
            clean_pin = re.sub(r'[^0-9#*]', '', pin)
            send_digits += f"wwww{clean_pin}"
            if not clean_pin.endswith('#'):
                send_digits += '#'

        from_number = TWILIO_PHONE_NUMBER or os.getenv("TWILIO_PHONE_NUMBER")
        if not from_number:
            raise RuntimeError("TWILIO_PHONE_NUMBER not set")

        # Ensure phone number has proper format
        clean_phone = re.sub(r'[\s\-()]', '', phone_number)
        if not clean_phone.startswith('+'):
            if clean_phone.startswith('91') and len(clean_phone) >= 12:
                clean_phone = '+' + clean_phone
            elif len(clean_phone) == 10:
                clean_phone = '+91' + clean_phone
            else:
                clean_phone = '+' + clean_phone

        # Store session info for TwiML generation
        if session_id and session_id in _active_sessions:
            _active_sessions[session_id].send_digits = send_digits
            _active_sessions[session_id].dial_number = clean_phone

        recording_callback = f"{webhook_base_url}/api/live-concall/recording?session_id={session_id}"

        call = client.calls.create(
            url=twiml_url,
            to=clean_phone,
            from_=from_number,
            status_callback=status_url,
            status_callback_event=["initiated", "ringing", "answered",
                                   "completed", "failed", "busy", "no-answer"],
            status_callback_method="POST",
            record=True,
            recording_status_callback=recording_callback,
            recording_status_callback_method="POST",
        )

        print(f"LIVE_CONCALL: Twilio call placed: SID={call.sid}, "
              f"to={clean_phone}, status={call.status}", file=sys.stderr)

        return {
            "call_sid": call.sid,
            "status": call.status,
            "to": clean_phone,
            "session_id": session_id,
        }

    except Exception as e:
        print(f"LIVE_CONCALL: Twilio call failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return {"error": str(e), "session_id": session_id}


def inject_dtmf(call_sid: str, digits: str) -> Dict[str, Any]:
    """Send DTMF tones into an active Twilio call (fallback button)."""
    try:
        client = _get_twilio_client()
        call = client.calls(call_sid)

        from twilio.twiml.voice_response import VoiceResponse
        twiml = VoiceResponse()
        twiml.play(digits=digits)

        call.update(twiml=str(twiml))

        print(f"LIVE_CONCALL: Injected DTMF '{digits}' into call {call_sid}",
              file=sys.stderr)
        return {"status": "ok", "digits": digits, "call_sid": call_sid}

    except Exception as e:
        print(f"LIVE_CONCALL: DTMF injection failed: {e}", file=sys.stderr)
        return {"error": str(e)}


def end_twilio_call(call_sid: str) -> Dict[str, Any]:
    """Hang up an active Twilio call."""
    try:
        client = _get_twilio_client()
        call = client.calls(call_sid).update(status="completed")
        print(f"LIVE_CONCALL: Call {call_sid} ended", file=sys.stderr)
        return {"status": "completed", "call_sid": call_sid}
    except Exception as e:
        print(f"LIVE_CONCALL: End call failed: {e}", file=sys.stderr)
        return {"error": str(e)}


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
# 5. POST-CALL TRANSCRIPTION (Twilio Recording → Gemini)
# ---------------------------------------------------------------------------

def _transcribe_recording_with_gemini(session: LiveConcallSession,
                                       recording_url: str) -> str:
    """
    Download a Twilio recording and transcribe it with Gemini Files API.
    Returns transcript text or empty string on failure.
    """
    api_key = GOOGLE_API_KEY or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("LIVE_CONCALL: No GOOGLE_API_KEY for recording transcription",
              file=sys.stderr)
        return ""

    try:
        # Twilio recording URLs require auth credentials to download
        sid = TWILIO_ACCOUNT_SID or os.getenv("TWILIO_ACCOUNT_SID")
        token = TWILIO_AUTH_TOKEN or os.getenv("TWILIO_AUTH_TOKEN")

        # Ensure URL ends with .mp3 for easy download
        dl_url = recording_url
        if not any(dl_url.endswith(ext) for ext in ['.mp3', '.wav']):
            dl_url = dl_url + '.mp3'

        print(f"LIVE_CONCALL: Downloading recording from {dl_url}", file=sys.stderr)

        import requests as _requests
        resp = _requests.get(dl_url, auth=(sid, token), timeout=120)
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
      POST /api/live-concall/inject-dtmf    — Send DTMF to active call
      POST /api/live-concall/end            — End active call & summarize
      GET  /api/live-concall/session/<id>   — Get session state
      POST /api/live-concall/twiml          — TwiML webhook (Twilio)
      POST /api/live-concall/call-status    — Call status webhook (Twilio)
      POST /api/live-concall/recording      — Recording webhook (Twilio)
    """
    global _call_gemini_api_fn
    _call_gemini_api_fn = call_gemini_api_fn

    # --- Schedule ---
    @app.route('/api/live-concall/schedule', methods=['GET'])
    def live_concall_schedule():
        """Return upcoming concall schedule."""
        try:
            days = int(request.args.get('days', 30))
            include_past = request.args.get('include_past', 'false').lower() == 'true'
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                calls = loop.run_until_complete(
                    fetch_concall_schedule(days, include_past=include_past)
                )
            finally:
                loop.close()

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
        """Start a live concall session — place Twilio call."""
        try:
            data = request.get_json(force=True)
            phone_number = data.get('phone_number', '').strip()
            passcode = data.get('passcode', '').strip()
            pin = data.get('pin', '').strip()
            company_name = data.get('company_name', '').strip()
            ticker = data.get('ticker', '').strip().upper()

            if not phone_number:
                return jsonify({"error": "phone_number is required"}), 400

            # Check Twilio credentials before starting
            if not (TWILIO_ACCOUNT_SID or os.getenv("TWILIO_ACCOUNT_SID")):
                return jsonify({"error": "Twilio not configured. "
                                         "TWILIO_ACCOUNT_SID is missing."}), 503
            if not (TWILIO_PHONE_NUMBER or os.getenv("TWILIO_PHONE_NUMBER")):
                return jsonify({"error": "Twilio not configured. "
                                         "TWILIO_PHONE_NUMBER is missing."}), 503

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

            # Determine webhook base URL
            webhook_base = data.get('webhook_base_url', '').strip()
            if not webhook_base:
                webhook_base = request.host_url.rstrip('/')

            session.update_state("dialing")

            # Place call in background thread
            def _dial():
                result = place_twilio_call(
                    phone_number, passcode, pin,
                    session_id, webhook_base
                )
                if "error" in result:
                    session.update_state("error")
                    session._emit("live_concall_error", {
                        "session_id": session_id,
                        "error": result["error"]
                    })
                else:
                    session.call_sid = result.get("call_sid", "")

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

            # End Twilio call
            if session.call_sid:
                end_twilio_call(session.call_sid)

            # If we already have a recording URL, kick off the full pipeline now.
            # If not, the recording webhook will trigger it when Twilio delivers the file.
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

    # --- TwiML Webhook ---
    @app.route('/api/live-concall/twiml', methods=['POST', 'GET'])
    def live_concall_twiml():
        """
        Return TwiML instructions for Twilio call.
        1. Play DTMF digits to enter the conference passcode.
        2. Pause for up to 2 hours to keep the call alive (recording runs
           in parallel via the record=True flag set at call creation time).
        """
        session_id = request.args.get('session_id', '')
        session = _active_sessions.get(session_id)

        from twilio.twiml.voice_response import VoiceResponse
        resp = VoiceResponse()

        if session and session.send_digits:
            # Send passcode digits with preceding pauses
            resp.play(digits=session.send_digits)

        # Keep the call alive for up to 2 hours
        resp.pause(length=7200)

        return Response(str(resp), mimetype='text/xml')

    # --- Call Status Webhook ---
    @app.route('/api/live-concall/call-status', methods=['POST'])
    def live_concall_call_status():
        """Handle Twilio call status callbacks."""
        session_id = request.args.get('session_id', '')
        session = _active_sessions.get(session_id)

        call_status = request.form.get('CallStatus', '')
        call_sid = request.form.get('CallSid', '')

        print(f"LIVE_CONCALL: Call status update: {call_status} "
              f"(SID: {call_sid}, session: {session_id})", file=sys.stderr)

        if session:
            if not session.call_sid and call_sid:
                session.call_sid = call_sid

            if call_status == 'in-progress':
                session.connected_at = time.time()
                session.update_state("recording")
            elif call_status == 'completed':
                session.ended_at = time.time()
                if session.state not in ('summarizing', 'transcribing',
                                          'complete', 'ended'):
                    # Recording webhook will trigger the pipeline
                    session.update_state("ended")
            elif call_status in ('failed', 'busy', 'no-answer'):
                session.update_state("error")
                session._emit("live_concall_error", {
                    "session_id": session_id,
                    "error": f"Call {call_status}. "
                             "Please check the dial-in number and passcode.",
                })

        return Response("OK", status=200)

    # --- Recording Webhook ---
    @app.route('/api/live-concall/recording', methods=['POST'])
    def live_concall_recording():
        """
        Handle Twilio recording status callbacks.
        When the recording is ready, auto-trigger transcription + summarization.
        """
        session_id = request.args.get('session_id', '')
        recording_url = request.form.get('RecordingUrl', '')
        recording_status = request.form.get('RecordingStatus', '')
        recording_duration = request.form.get('RecordingDuration', '0')

        print(f"LIVE_CONCALL: Recording callback: status={recording_status}, "
              f"duration={recording_duration}s, url={recording_url} "
              f"(session: {session_id})", file=sys.stderr)

        session = _active_sessions.get(session_id)
        if not session:
            return Response("OK", status=200)

        if recording_status != 'completed' or not recording_url:
            return Response("OK", status=200)

        session.recording_url = recording_url
        session._emit("live_concall_recording", {
            "session_id": session_id,
            "recording_url": recording_url,
            "duration_seconds": int(recording_duration or 0),
        })

        # Only run the pipeline if not already running/complete
        if session.state not in ('summarizing', 'transcribing', 'complete'):
            thread = threading.Thread(
                target=_run_post_call_pipeline,
                args=(session, call_gemini_api_fn, recording_url),
                daemon=True
            )
            thread.start()

        return Response("OK", status=200)

    print("LIVE_CONCALL: ✓ Live Concall routes registered", file=sys.stderr)
