"""
live_concall.py — Live Conference Call Module for Kagger AI

Enables automated joining, transcription, and real-time streaming of
earnings conference calls.

Workflow:
1. Fetch upcoming concall schedules from IR Pulse Engine API
2. Extract dial-in credentials from NSE/BSE PDF announcements
3. Place Twilio outbound call with DTMF passcode injection
4. Stream Twilio media audio via WebSocket → Deepgram for real-time transcription
5. Broadcast transcript chunks to frontend via Flask-SocketIO
6. Post-call: aggregate transcript and run Gemini analysis
"""

import os
import sys
import json
import time
import asyncio
import threading
import traceback
import base64
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

        # Step 2: Extract text via pdfplumber
        text = ""
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
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

        if len(text.strip()) < 50:
            print("LIVE_CONCALL: PDF text too short, trying Gemini Vision fallback",
                  file=sys.stderr)
            return await _extract_dialin_gemini_vision(pdf_bytes, pdf_url)

        # Step 3: Parse with Gemini
        return await _parse_dialin_with_gemini(text, pdf_url)

    except Exception as e:
        print(f"LIVE_CONCALL: PDF extraction error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return empty_result


async def _parse_dialin_with_gemini(text: str, source_url: str) -> Dict[str, Any]:
    """Use Gemini to extract structured dial-in info from PDF text."""
    api_key = GOOGLE_API_KEY or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("LIVE_CONCALL: No GOOGLE_API_KEY for Gemini extraction", file=sys.stderr)
        return _regex_fallback_extract(text, source_url)

    extraction_prompt = f"""You are a financial parsing assistant. Extract the dial-in details for the earnings conference call from the text below.

Provide the output in JSON format with the following fields:
- phone_number: The dial-in phone number (preferably local India toll/toll-free number, cleaned of spaces, e.g. +912262801123)
- passcode: The participant passcode or conference ID if required (null if not needed)
- pin: The participant PIN if required (null if not needed)
- date: The date of the call (format YYYY-MM-DD)
- time: The time of the call (format HH:MM IST, e.g., 16:00 IST)
- webcast_url: Any webcast or online meeting URL if mentioned (null if none)
- extra_numbers: A list of alternative phone numbers if any

If the details cannot be found, set them to null.

Text:
\"\"\"
{text[:4000]}
\"\"\"
"""

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=extraction_prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.0
            )
        )

        if response.text:
            parsed = json.loads(response.text)
            parsed["raw_text"] = text[:2000]
            parsed["source_url"] = source_url
            print(f"LIVE_CONCALL: Gemini extracted dial-in: "
                  f"phone={parsed.get('phone_number')}, "
                  f"date={parsed.get('date')}, time={parsed.get('time')}",
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

        prompt = """Extract the dial-in details for this earnings conference call PDF.
Return JSON with: phone_number, passcode, pin, date (YYYY-MM-DD), time (HH:MM IST), webcast_url, extra_numbers.
Set any missing fields to null."""

        response = client.models.generate_content(
            model="gemini-3-flash-preview",
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

    # Passcode/PIN
    for label in ["passcode", "conference id", "participant code", "pin"]:
        m = re.search(rf'{label}[\s:]+(\d[\d\s#*]+)', text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if "pin" in label.lower():
                result["pin"] = val
            else:
                result["passcode"] = val

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

        call = client.calls.create(
            url=twiml_url,
            to=clean_phone,
            from_=from_number,
            status_callback=status_url,
            status_callback_event=["initiated", "ringing", "answered",
                                   "completed", "failed", "busy", "no-answer"],
            status_callback_method="POST",
            record=True,  # Fallback recording
            recording_status_callback=f"{webhook_base_url}/api/live-concall/recording?session_id={session_id}",
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

        # Use TwiML to play DTMF
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
    state: str = "created"  # created → dialing → connected → streaming → ended → summarizing → complete
    started_at: float = 0.0
    connected_at: float = 0.0
    ended_at: float = 0.0

    # Transcript
    transcript_chunks: List[str] = field(default_factory=list)
    full_transcript: str = ""
    chunk_count: int = 0

    # Deepgram connection
    deepgram_ws: Any = None

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
        }

    def _emit(self, event: str, data: Dict):
        """Emit via SocketIO if callback set."""
        if self._emit_fn:
            try:
                self._emit_fn(event, data)
            except Exception as e:
                print(f"LIVE_CONCALL: Emit error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# 5. DEEPGRAM STREAMING TRANSCRIPTION
# ---------------------------------------------------------------------------

async def _start_deepgram_stream(session: LiveConcallSession):
    """
    Open a WebSocket connection to Deepgram for streaming transcription.
    Runs in the background, receiving audio chunks from Twilio media stream.
    """
    dg_key = DEEPGRAM_API_KEY or os.getenv("DEEPGRAM_API_KEY")
    if not dg_key:
        print("LIVE_CONCALL: DEEPGRAM_API_KEY not set, "
              "falling back to chunk-based Gemini transcription", file=sys.stderr)
        return

    try:
        import websockets

        dg_url = (
            "wss://api.deepgram.com/v1/listen?"
            "encoding=mulaw&sample_rate=8000&channels=1&"
            "model=nova-2&language=en&punctuate=true&"
            "interim_results=true&endpointing=200"
        )

        headers = {"Authorization": f"Token {dg_key}"}

        async with websockets.connect(dg_url, extra_headers=headers) as ws:
            session.deepgram_ws = ws
            print(f"LIVE_CONCALL: Deepgram WebSocket connected for "
                  f"session {session.session_id}", file=sys.stderr)

            async for msg in ws:
                try:
                    data = json.loads(msg)
                    channel = data.get("channel", {})
                    alternatives = channel.get("alternatives", [{}])
                    transcript = alternatives[0].get("transcript", "")
                    is_final = data.get("is_final", False)

                    if transcript.strip():
                        session.add_transcript(transcript, is_final=is_final)
                except json.JSONDecodeError:
                    pass

    except Exception as e:
        print(f"LIVE_CONCALL: Deepgram stream error: {e}", file=sys.stderr)
    finally:
        session.deepgram_ws = None


# ---------------------------------------------------------------------------
# 6. GEMINI CHUNK-BASED TRANSCRIPTION FALLBACK
# ---------------------------------------------------------------------------

def _transcribe_audio_chunk_gemini(audio_data: bytes,
                                    mime_type: str = "audio/x-mulaw") -> str:
    """
    Fallback: transcribe a single audio chunk using Gemini.
    Used when Deepgram is not configured.
    """
    api_key = GOOGLE_API_KEY or os.getenv("GOOGLE_API_KEY")
    if not api_key or len(audio_data) < 500:
        return ""

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[
                types.Part.from_bytes(data=audio_data, mime_type=mime_type),
                types.Part.from_text(
                    text="Transcribe this audio verbatim. "
                         "This is from an Indian corporate earnings conference call. "
                         "Output ONLY the transcription. "
                         "If silent or unclear, respond with [SILENCE]."
                )
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=1500
            )
        )

        if response.text:
            text = response.text.strip()
            refusals = ["I am sorry", "I cannot", "unable to",
                        "cannot process", "Please provide"]
            if any(r.lower() in text.lower() for r in refusals):
                return ""
            return text

    except Exception as e:
        print(f"LIVE_CONCALL: Gemini chunk transcription error: {e}",
              file=sys.stderr)

    return ""


# ---------------------------------------------------------------------------
# 7. POST-CALL SUMMARIZATION
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
            model="gemini-3-flash-preview",
            temperature=1,
            thinking_level="HIGH"
        )
        return result
    except Exception as e:
        print(f"LIVE_CONCALL: Post-call summary failed: {e}", file=sys.stderr)
        return ""


# ---------------------------------------------------------------------------
# 8. FLASK ROUTE REGISTRATION
# ---------------------------------------------------------------------------

def register_live_concall_routes(app, socketio, call_gemini_api_fn):
    """
    Register all Live Concall REST and WebSocket endpoints.

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
      WS   /api/live-concall/media-stream   — Twilio media stream WebSocket
    """

    # --- Schedule ---
    @app.route('/api/live-concall/schedule', methods=['GET'])
    def live_concall_schedule():
        """Return upcoming concall schedule."""
        try:
            days = int(request.args.get('days', 30))
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                calls = loop.run_until_complete(fetch_concall_schedule(days))
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

            # Create session
            session_id = f"lc_{int(time.time())}_{ticker or 'UNKNOWN'}"
            session = LiveConcallSession(
                session_id=session_id,
                company_name=company_name,
                ticker=ticker,
                started_at=time.time(),
            )
            session._emit_fn = lambda evt, data: socketio.emit(evt, data)
            _active_sessions[session_id] = session

            # Determine webhook base URL
            webhook_base = data.get('webhook_base_url', '')
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

            session.update_state("summarizing")

            # Generate summary in background
            def _summarize():
                try:
                    result = _generate_post_call_summary(session, call_gemini_api_fn)
                    session.analysis_result = result
                    session.update_state("complete")

                    # Store in agent results for persistence
                    from agents.base import store_latest_result
                    store_latest_result('concall', session.ticker, {
                        'analysis': result,
                        'ticker': session.ticker,
                        'concall_label': f"Live Concall — {session.company_name}",
                        'concall_link': '',
                        'transcript_text': session.full_transcript,
                        'analyzed_at': time.time(),
                        'source': 'live_concall',
                        'duration_seconds': session.get_duration(),
                    })

                    session._emit("live_concall_summary", {
                        "session_id": session_id,
                        "analysis": result,
                        "duration": session.get_duration(),
                        "transcript_length": len(session.full_transcript),
                    })
                except Exception as e:
                    print(f"LIVE_CONCALL: Summary generation failed: {e}",
                          file=sys.stderr)
                    session.update_state("error")

            if session.full_transcript and len(session.full_transcript.strip()) > 200:
                thread = threading.Thread(target=_summarize, daemon=True)
                thread.start()
            else:
                session.update_state("complete")
                session._emit("live_concall_summary", {
                    "session_id": session_id,
                    "analysis": "",
                    "error": "Transcript too short for analysis "
                             f"({len(session.full_transcript)} chars)",
                })

            return jsonify({
                "status": "ok",
                "session_id": session_id,
                "duration": session.get_duration(),
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

    # --- TwiML Webhook ---
    @app.route('/api/live-concall/twiml', methods=['POST', 'GET'])
    def live_concall_twiml():
        """
        Return TwiML instructions for Twilio call.
        Configures: <Connect><Stream> for media streaming + <Number sendDigits>.
        """
        session_id = request.args.get('session_id', '')
        session = _active_sessions.get(session_id)

        from twilio.twiml.voice_response import VoiceResponse, Connect

        resp = VoiceResponse()

        if session:
            # Connect media stream for live transcription
            stream_url = request.host_url.rstrip('/').replace('http://', 'wss://').replace('https://', 'wss://')
            stream_url += f'/api/live-concall/media-stream?session_id={session_id}'

            connect = Connect()
            connect.stream(url=stream_url)
            resp.append(connect)

            # Also send DTMF digits if we have them
            if session.send_digits:
                resp.play(digits=session.send_digits)

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
            if call_status == 'in-progress':
                session.connected_at = time.time()
                session.update_state("connected")
            elif call_status == 'completed':
                session.ended_at = time.time()
                if session.state not in ('summarizing', 'complete'):
                    session.update_state("ended")
            elif call_status in ('failed', 'busy', 'no-answer'):
                session.update_state("error")
                session._emit("live_concall_error", {
                    "session_id": session_id,
                    "error": f"Call {call_status}",
                })

        return Response("OK", status=200)

    # --- Recording Webhook ---
    @app.route('/api/live-concall/recording', methods=['POST'])
    def live_concall_recording():
        """Handle Twilio recording status callbacks (fallback)."""
        session_id = request.args.get('session_id', '')
        recording_url = request.form.get('RecordingUrl', '')

        print(f"LIVE_CONCALL: Recording available: {recording_url} "
              f"(session: {session_id})", file=sys.stderr)

        session = _active_sessions.get(session_id)
        if session:
            session._emit("live_concall_recording", {
                "session_id": session_id,
                "recording_url": recording_url,
            })

        return Response("OK", status=200)

    # --- Media Stream WebSocket ---
    @socketio.on('connect', namespace='/live-concall-media')
    def handle_media_connect():
        print("LIVE_CONCALL: Media stream WebSocket connected", file=sys.stderr)

    @socketio.on('media', namespace='/live-concall-media')
    def handle_media_message(data):
        """
        Handle incoming Twilio media stream messages.
        Forwards audio chunks to Deepgram or Gemini for transcription.
        """
        event = data.get('event', '')
        session_id = data.get('session_id', '')

        if event == 'media':
            payload = data.get('media', {})
            audio_b64 = payload.get('payload', '')

            if not audio_b64:
                return

            audio_bytes = base64.b64decode(audio_b64)
            session = _active_sessions.get(session_id)

            if not session:
                return

            if session.state != "streaming":
                session.update_state("streaming")

            # Forward to Deepgram if connected
            if session.deepgram_ws:
                try:
                    asyncio.run(session.deepgram_ws.send(audio_bytes))
                except Exception:
                    pass
            else:
                # Fallback: accumulate and periodically transcribe with Gemini
                # (Buffer ~5 seconds of audio before transcribing)
                if not hasattr(session, '_audio_buffer'):
                    session._audio_buffer = bytearray()
                    session._last_chunk_time = time.time()

                session._audio_buffer.extend(audio_bytes)

                # 8kHz µ-law mono = 8000 bytes/sec, transcribe every 5 seconds
                if len(session._audio_buffer) >= 40000:
                    chunk = bytes(session._audio_buffer)
                    session._audio_buffer = bytearray()

                    def _transcribe():
                        text = _transcribe_audio_chunk_gemini(chunk)
                        if text:
                            session.add_transcript(text, is_final=True)

                    threading.Thread(target=_transcribe, daemon=True).start()

        elif event == 'start':
            stream_sid = data.get('start', {}).get('streamSid', '')
            print(f"LIVE_CONCALL: Media stream started (SID: {stream_sid})",
                  file=sys.stderr)

        elif event == 'stop':
            print(f"LIVE_CONCALL: Media stream stopped", file=sys.stderr)

    @socketio.on('disconnect', namespace='/live-concall-media')
    def handle_media_disconnect():
        print("LIVE_CONCALL: Media stream WebSocket disconnected", file=sys.stderr)

    print("LIVE_CONCALL: ✓ Live Concall routes registered", file=sys.stderr)
