"""
nse_fetcher.py — Fetch earnings-call AUDIO recordings from NSE corporate filings.

Indian listed companies file an "Audio recording / Link of Recording of earnings
conference call" announcement with the exchange shortly after the call — days
before the call typically appears on YouTube. The actual audio URL lives as a
clickable hyperlink *annotation* inside the filed intimation PDF (not in its
visible text), and points either to a direct audio file (e.g. an .mp3 on the
company's own domain) or to the company's Investor Relations webpage.

This module queries the NSE corporate-announcements API (keyed by the NSE
symbol = our ticker), finds the latest audio-recording filing, downloads the
intimation PDF, and resolves the embedded link into either a direct ``audio_url``
or an ``ir_url`` (handed off to the IR-website scraper by the caller).
"""

import os
import re
import sys
import asyncio
from io import BytesIO
from urllib.parse import urlparse, parse_qs, unquote

import httpx

ANN_API = "https://www.nseindia.com/api/corporate-announcements"
NSE_HOME = "https://www.nseindia.com/"
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}

# Subject keywords that mark a *post-call audio recording* filing (not a pre-call
# schedule, transcript, or generic intimation).
_REC_KEYWORDS = ("link of recording", "audio recording", "recording of the")

# NSE states transcripts tersely — the live text is literally
# "...has informed the Exchange about Transcript" — so match the bare word and
# exclude the meeting types that also produce transcripts but are not concalls.
_TRANSCRIPT_MARKER = "transcript"
_TRANSCRIPT_EXCLUDE = ("agm", "annual general meeting", "egm", "postal ballot",
                       "extraordinary general meeting", "court convened")

# "Q1FY27", "Q4 FY 2026", "Q3-FY26" as stated in the filing subject. Preferred
# over inferring from the filing date, which is only ever an approximation.
_QFY_RE = re.compile(r'\bq\s*-?\s*([1-4])\s*[-/ ]?\s*fy\s*-?\s*(\d{2,4})\b', re.IGNORECASE)


def _quarter_from_text(text: str) -> str:
    """'link of Q4FY2026 earnings call' -> 'Mar 2026'. '' when not stated."""
    m = _QFY_RE.search(text or '')
    if not m:
        return ''
    qn = int(m.group(1))
    fy_end_year = 2000 + int(m.group(2)[-2:])
    if qn == 4:
        return f"Mar {fy_end_year}"
    return f"{ {1: 'Jun', 2: 'Sep', 3: 'Dec'}[qn]} {fy_end_year - 1}"


# NSE attachment names look like  SYMBOL_ddmmyyyyhhmmss_Human_Readable_Part.pdf
_FNAME_PREFIX_RE = re.compile(r'^[A-Za-z0-9]+(?:_[A-Za-z]+)?_\d{14}_')
_FNAME_DATE_RE = re.compile(r'(\d{2})(\d{2})(20\d{2})')

# Filenames run words together, so the word-boundary form used for subject text
# misses them ("...TranscriptQ3FY26PEL"). This variant drops the leading
# boundary, accepts "Qtr", and tolerates a short run between the quarter and the
# fiscal year ("Qtr2H1FY26" -> Q2 FY26).
_FNAME_QFY_RE = re.compile(r'(?:qtr|q)\s*-?\s*([1-4]).{0,8}?fy\s*-?\s*(\d{2,4})', re.IGNORECASE)


def _quarter_from_filename(pdf_url: str) -> str:
    """
    Quarter inferred from an NSE attachment filename.

    Worth doing because the filing date alone is unreliable for transcripts:
    they are submitted about a week after the call, so a Q4 call held on 30 May
    can have its transcript filed on 2 June and be misread as a Q1 document.
    Filenames carry better evidence — either the quarter outright
    ('..._Q4FY26_Transcript...') or the CALL date
    ('...ConcallTranscript30052026.pdf'). Returns '' when neither is present.
    """
    name = (pdf_url or '').rsplit('/', 1)[-1]
    # Drop the symbol + filing-timestamp prefix so its date isn't mistaken for
    # the call date.
    body = _FNAME_PREFIX_RE.sub('', name)

    m = _FNAME_QFY_RE.search(body)
    if m:
        qn = int(m.group(1))
        fy_end_year = 2000 + int(m.group(2)[-2:])
        if qn == 4:
            return f"Mar {fy_end_year}"
        return f"{ {1: 'Jun', 2: 'Sep', 3: 'Dec'}[qn]} {fy_end_year - 1}"

    from datetime import datetime as _dtm
    for m in _FNAME_DATE_RE.finditer(body):
        dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            called = _dtm(yyyy, mm, dd)
        except ValueError:
            continue
        q_month = _MONTH_TO_QUARTER[called.month]
        q_year = called.year
        if called.month in (1, 2, 3) and q_month == 'Dec':
            q_year -= 1
        return f"{q_month} {q_year}"
    return ''

_AUDIO_EXT = {'.mp3', '.wav', '.m4a', '.ogg', '.aac', '.flac', '.wma', '.opus'}
_VIDEO_EXT = {'.mp4', '.webm', '.mkv', '.avi', '.mov', '.flv', '.wmv'}

# Links that are never the recording (boilerplate footers / social / exchange).
_LINK_BLOCKLIST = ("mailto:", "nseindia.com", "bseindia.com", "sebi.gov", "mca.gov",
                   "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com")

# Publication month -> the financial quarter such a document discusses.
#
# Keyed on the LAST QUARTER THAT HAD ALREADY ENDED when the document appeared.
# A quarter-end month belongs to the PREVIOUS quarter, not its own: a call held
# on 4 September is about the June quarter, because the September quarter does
# not close until the 30th. Months 3, 6, 9 and 12 used to map to their own
# quarter and were wrong for exactly that reason.
# (Mirror of concall_agent's _MONTH_TO_QUARTER; duplicated here to avoid a
# circular import — keep all three in step.)
_MONTH_TO_QUARTER = {
    1: 'Dec', 2: 'Dec', 3: 'Dec', 4: 'Mar', 5: 'Mar', 6: 'Mar',
    7: 'Jun', 8: 'Jun', 9: 'Jun', 10: 'Sep', 11: 'Sep', 12: 'Sep',
}


def _andt_to_quarter(an_dt: str) -> str:
    """Map an NSE announcement date ('30-May-2026 12:32:35') to its discussed
    financial quarter label ('Mar 2026'). Returns '' on failure."""
    if not an_dt:
        return ''
    m = re.match(r'(\d{1,2})-([A-Za-z]{3})-(\d{4})', an_dt.strip())
    if not m:
        return ''
    from datetime import datetime as _dt
    try:
        d = _dt.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%d %b %Y")
    except ValueError:
        return ''
    q_month = _MONTH_TO_QUARTER[d.month]
    q_year = d.year
    if d.month in (1, 2, 3) and q_month == 'Dec':
        q_year -= 1
    return f"{q_month} {q_year}"


def _unwrap_safelink(url: str) -> str:
    """Unwrap an Outlook 'safelinks' wrapper to recover the real target URL."""
    try:
        if "safelinks.protection.outlook.com" in url:
            q = parse_qs(urlparse(url).query).get("url")
            if q:
                return unquote(q[0])
    except Exception:
        pass
    return url


def _url_kind(url: str) -> str:
    """'audio', 'video', or 'webpage' based on the URL's file extension."""
    clean = url.split('?')[0].split('#')[0].lower()
    last = clean.rsplit('/', 1)[-1]
    ext = '.' + last.rsplit('.', 1)[1] if '.' in last else ''
    if ext in _AUDIO_EXT:
        return 'audio'
    if ext in _VIDEO_EXT:
        return 'video'
    return 'webpage'


def _extract_pdf_hyperlinks(pdf_bytes: bytes) -> set:
    """Extract clickable hyperlink annotations from a PDF (the audio link is an
    annotation, not visible text). Uses pdfplumber + pypdf for coverage and
    unwraps Outlook safelinks."""
    links = set()
    try:
        import pdfplumber
        with pdfplumber.open(BytesIO(pdf_bytes)) as doc:
            for pg in doc.pages:
                for h in (pg.hyperlinks or []):
                    if h.get("uri"):
                        links.add(h["uri"])
                for a in (pg.annots or []):
                    data = a.get("data") or {}
                    A = data.get("A") or {}
                    u = A.get("URI") or a.get("uri")
                    if isinstance(u, bytes):
                        try:
                            u = u.decode("utf-8", "ignore")
                        except Exception:
                            u = None
                    if isinstance(u, str) and u.startswith("http"):
                        links.add(u)
    except Exception as e:
        print(f"NSE_FETCHER: pdfplumber link extraction failed: {e}", file=sys.stderr)
    try:
        try:
            from pypdf import PdfReader
        except Exception:
            from PyPDF2 import PdfReader
        reader = PdfReader(BytesIO(pdf_bytes))
        for pg in reader.pages:
            for an in (pg.get("/Annots") or []):
                try:
                    A = an.get_object().get("/A") or {}
                    uri = A.get("/URI")
                    if uri:
                        links.add(str(uri))
                except Exception:
                    continue
    except Exception as e:
        print(f"NSE_FETCHER: pypdf link extraction failed: {e}", file=sys.stderr)

    return {_unwrap_safelink(u) for u in links}


def _row_quarter(row: dict) -> str:
    """
    Best available quarter for an announcement row, most reliable evidence first:
    the subject line, then the attachment filename (which often carries the
    quarter or the call date), and only then the filing date.
    """
    return (_quarter_from_text(str(row.get("attchmntText", "")))
            or _quarter_from_filename(str(row.get("attchmntFile", "")))
            or _andt_to_quarter(row.get("an_dt", "")))


async def _nse_get_json(params: dict):
    """GET the NSE announcements API as JSON. Tries httpx (with cookie priming),
    then falls back to curl_cffi + residential proxy (NSE blocks datacenter IPs).
    Returns the parsed JSON (a list) or None."""
    # Attempt 1: httpx with a homepage prime to establish the session.
    try:
        async with httpx.AsyncClient(follow_redirects=True, headers=NSE_HEADERS, timeout=30.0) as client:
            try:
                await client.get(NSE_HOME, timeout=15.0)
            except Exception:
                pass
            r = await client.get(ANN_API, params=params, timeout=30.0)
            if r.status_code == 200:
                return r.json()
            print(f"NSE_FETCHER: httpx API status {r.status_code}; trying proxy fallback", file=sys.stderr)
    except Exception as e:
        print(f"NSE_FETCHER: httpx API fetch failed: {e}; trying proxy fallback", file=sys.stderr)

    # Attempt 2: curl_cffi (Chrome TLS impersonation) + optional residential proxy.
    try:
        def _cffi():
            from curl_cffi import requests as cffi_requests
            proxy_url = os.environ.get("RESIDENTIAL_PROXY_URL")
            proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
            session = cffi_requests.Session(impersonate="chrome110", proxies=proxies, headers=NSE_HEADERS)
            try:
                session.get(NSE_HOME, timeout=20)
            except Exception:
                pass
            r = session.get(ANN_API, params=params, timeout=30)
            return r.json() if r.status_code == 200 else None
        return await asyncio.to_thread(_cffi)
    except Exception as e:
        print(f"NSE_FETCHER: curl_cffi API fetch failed: {e}", file=sys.stderr)
        return None


async def fetch_nse_concall_transcript_async(ticker: str, results_quarter: str = '') -> dict:
    """
    Find the latest *written* earnings-call transcript a company filed with NSE.

    This is the cheapest and most reliable concall source available: the PDF is
    the transcript itself, so there is no audio to download and no AI
    transcription to pay for, and it is exact-company by construction (filed
    under the NSE symbol) with a filing date to pin the quarter.

    Returns ``{pdf_url, date, quarter}`` or ``{}`` when nothing is filed.
    Note transcripts typically appear about a week AFTER the call, so this comes
    up empty in the days right after results — the audio sources cover that gap.
    """
    sym = (ticker or '').strip().upper()
    if not sym:
        return {}
    try:
        data = await _nse_get_json({"index": "equities", "symbol": sym})
        if not isinstance(data, list) or not data:
            return {}

        transcripts = []
        for row in data[:120]:
            text = str(row.get("attchmntText", "")).lower()
            pdf = (row.get("attchmntFile", "") or "")
            if _TRANSCRIPT_MARKER not in text:
                continue
            if any(x in text for x in _TRANSCRIPT_EXCLUDE):
                continue
            if not pdf.lower().endswith(".pdf"):
                continue
            transcripts.append(row)

        if not transcripts:
            print(f"NSE_FETCHER: no transcript filing for {sym}", file=sys.stderr)
            return {}

        chosen = None
        if results_quarter:
            for row in transcripts:
                row_q = _row_quarter(row)
                if row_q == results_quarter:
                    chosen = row
                    break
        if chosen is None:
            chosen = transcripts[0]

        an_dt = chosen.get("an_dt", "")
        quarter = _row_quarter(chosen)
        result = {"pdf_url": chosen["attchmntFile"], "date": an_dt[:11], "quarter": quarter}
        print(f"NSE_FETCHER: {sym} transcript filing [{an_dt[:11]}] quarter={quarter or '?'} "
              f"-> {result['pdf_url']}", file=sys.stderr)
        return result
    except Exception as e:
        print(f"NSE_FETCHER: transcript lookup error for {sym}: {e}", file=sys.stderr)
        return {}


async def fetch_nse_concall_recording_async(ticker: str, results_quarter: str = '') -> dict:
    """
    Find the latest earnings-call audio recording a company filed with NSE.

    Returns a dict ``{audio_url, ir_url, date, quarter, pdf_url}`` (any field may
    be empty) or ``{}`` if no recording filing exists. ``audio_url`` is a direct
    media file; ``ir_url`` is a company webpage to hand to the IR-website scraper.
    """
    sym = (ticker or '').strip().upper()
    if not sym:
        return {}
    try:
        data = await _nse_get_json({"index": "equities", "symbol": sym})
        if not isinstance(data, list) or not data:
            return {}

        # Newest-first; scan a bounded recent window for recording filings.
        recordings = []
        for row in data[:120]:
            text = str(row.get("attchmntText", "")).lower()
            pdf = (row.get("attchmntFile", "") or "")
            if any(k in text for k in _REC_KEYWORDS) and pdf.lower().endswith(".pdf"):
                recordings.append(row)
        if not recordings:
            print(f"NSE_FETCHER: no audio-recording filing for {sym}", file=sys.stderr)
            return {}

        # Prefer the filing matching the target quarter; else the newest.
        chosen = None
        if results_quarter:
            for row in recordings:
                row_q = _row_quarter(row)
                if row_q == results_quarter:
                    chosen = row
                    break
        if chosen is None:
            chosen = recordings[0]

        pdf_url = chosen["attchmntFile"]
        an_dt = chosen.get("an_dt", "")
        # The subject often names the quarter outright ("link of Q4FY2026
        # earnings call"); trust that over inferring it from the filing date.
        quarter = _row_quarter(chosen)
        print(f"NSE_FETCHER: {sym} recording filing [{an_dt[:11]}] quarter={quarter or '?'} -> {pdf_url}", file=sys.stderr)

        # Download the intimation PDF and resolve its embedded link.
        from fetchers.screener_fetcher import _stealth_download_pdf_async
        pdf_bytes, _ct, _sc = await _stealth_download_pdf_async(pdf_url)
        result = {"audio_url": "", "ir_url": "", "date": an_dt[:11], "quarter": quarter, "pdf_url": pdf_url}
        if not pdf_bytes:
            print(f"NSE_FETCHER: could not download intimation PDF for {sym}", file=sys.stderr)
            return result

        links = _extract_pdf_hyperlinks(pdf_bytes)
        # Prefer a direct media file; else fall back to a company webpage (IR seed).
        for u in links:
            if _url_kind(u) in ("audio", "video"):
                result["audio_url"] = u
                break
        if not result["audio_url"]:
            for u in links:
                ul = u.lower()
                if u.startswith("http") and not any(b in ul for b in _LINK_BLOCKLIST):
                    result["ir_url"] = u
                    break

        print(f"NSE_FETCHER: {sym} resolved audio_url={result['audio_url'] or '-'} ir_url={result['ir_url'] or '-'}", file=sys.stderr)
        return result
    except Exception as e:
        print(f"NSE_FETCHER: error for {sym}: {e}", file=sys.stderr)
        return {}
