"""
bse_fetcher.py — BSE corporate-announcement source for the Concall Agent.

Mirrors nse_fetcher.py, keyed by BSE scrip code instead of NSE symbol, and adds
the thing neither source exposed before: the *written transcript* PDF that
companies file under Regulation 30. That document is free to use (no audio
download, no AI transcription), exact-company by construction (filed under the
scrip code) and carries a filing date, which makes it the strongest evidence of
both company and quarter available anywhere in the pipeline.

Notes from probing the live API (2026-08):
  * The endpoint is AnnSubCategoryGetData, NOT AnnGetData. The latter answers
    200 with the string "No Record Found!" for every query.
  * `strType='C'`, `pageno` and `subcategory` are all required; dropping any of
    them yields zero rows.
  * The useful text is in HEADLINE and SUBCATNAME. NEWSSUB is generic LODR
    boilerplate ("Announcement under Regulation 30 (LODR)-Analyst / Investor
    Meet - Outcome") and is nearly useless on its own.
  * SUBCATNAME == "Earnings Call Transcript" is an exact category marker.
  * AUDIO_VIDEO_FILE sometimes holds a direct media URL (BSE hosts a 7 MB mp3
    of the call) and sometimes holds a PDF — always check the extension.
  * Attachments live under AttachLive when recent and AttachHis once archived;
    both must be tried.
"""

import re
import sys
import csv
import os
import asyncio
from io import BytesIO

import httpx

ANN_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
ATTACH_PATHS = ("AttachLive", "AttachHis")
ATTACH_BASE = "https://www.bseindia.com/xml-data/corpfiling/{path}/{name}"

BSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.bseindia.com/corporates/ann.html",
}

# Exact BSE sub-category for a filed transcript — far more reliable than
# keyword-matching the headline.
_TRANSCRIPT_SUBCAT = "earnings call transcript"

_TRANSCRIPT_KEYWORDS = (
    "transcript of", "earnings call transcript", "conference call transcript",
    "transcript of the earnings", "submission of transcript",
)
_REC_KEYWORDS = (
    "audio recording", "link of recording", "recording of the",
    "earnings call audio", "audio link",
)

_AUDIO_EXT = {'.mp3', '.wav', '.m4a', '.ogg', '.aac', '.flac', '.wma', '.opus'}
_VIDEO_EXT = {'.mp4', '.webm', '.mkv', '.avi', '.mov', '.flv', '.wmv'}

# Publication month → financial quarter-end month (mirrors concall_agent and
# nse_fetcher; duplicated to avoid a circular import).
_MONTH_TO_QUARTER = {
    1: 'Dec', 2: 'Dec', 3: 'Mar', 4: 'Mar', 5: 'Mar', 6: 'Jun',
    7: 'Jun', 8: 'Jun', 9: 'Sep', 10: 'Sep', 11: 'Sep', 12: 'Dec',
}

# "Q1FY27", "Q4 FY 26", "Q3-FY2026"
_QFY_RE = re.compile(r'\bq\s*-?\s*([1-4])\s*[-/ ]?\s*fy\s*-?\s*(\d{2,4})\b', re.IGNORECASE)

_BSE_MAP = None


def load_bse_map() -> dict:
    """{NSE ticker -> BSE scrip code} from the stock master. Cached per process."""
    global _BSE_MAP
    if _BSE_MAP is not None:
        return _BSE_MAP
    mapping = {}
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'trendlyne_all_stocks_master.csv')
        with open(path, 'r', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                nse = str(row.get('Ticker', '')).strip().upper()
                bse = str(row.get('BSE Ticker', '')).strip()
                if nse and bse:
                    mapping[nse] = bse
    except Exception as e:
        print(f"BSE_FETCHER: could not load BSE ticker map: {e}", file=sys.stderr)
    _BSE_MAP = mapping
    return mapping


def _quarter_from_qfy(text: str) -> str:
    """
    'Intimation of Q1FY27 earnings call audio recording' -> 'Jun 2026'.

    FY<yy> ends in calendar 20<yy>, so Q1/Q2/Q3 fall in the previous calendar
    year and Q4 in 20<yy> itself.
    """
    m = _QFY_RE.search(text or '')
    if not m:
        return ''
    qn = int(m.group(1))
    yy = int(m.group(2)[-2:])
    fy_end_year = 2000 + yy
    if qn == 4:
        return f"Mar {fy_end_year}"
    month = {1: 'Jun', 2: 'Sep', 3: 'Dec'}[qn]
    return f"{month} {fy_end_year - 1}"


# A date written into the headline — "held on May 23, 2026", "hosted by ... on
# May 30, 2026". These headlines describe the CALL, so the first date in them is
# the call date, which beats the filing date: a Q4 call held on 30 May can have
# its transcript filed on 2 June, and mapping that filing date lands it in the
# wrong quarter.
_CALL_DATE_RE = re.compile(
    r'(?:(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})'          # 23 May 2026
    r'|([A-Za-z]{3,9})\s+(\d{1,2})\s*,?\s*(\d{4}))',     # May 23, 2026
    re.IGNORECASE)

_MONTH_NAMES = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
     'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], start=1)}


def _quarter_from_call_date(text: str) -> str:
    """'...Conference Call held on May 23, 2026' -> 'Mar 2026'. '' when absent."""
    for m in _CALL_DATE_RE.finditer(text or ''):
        month_name = (m.group(2) or m.group(4) or '')[:3].lower()
        month = _MONTH_NAMES.get(month_name)
        if not month:
            continue
        year = int(m.group(3) or m.group(6))
        q_month = _MONTH_TO_QUARTER[month]
        if month in (1, 2) and q_month == 'Dec':
            year -= 1
        return f"{q_month} {year}"
    return ''


def _quarter_from_date(news_dt: str) -> str:
    """'2026-08-01T15:52:22.6' -> 'Jun 2026' (the quarter such a filing discusses)."""
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', (news_dt or '').strip())
    if not m:
        return ''
    year, month = int(m.group(1)), int(m.group(2))
    q_month = _MONTH_TO_QUARTER[month]
    if month in (1, 2) and q_month == 'Dec':
        year -= 1
    return f"{q_month} {year}"


def _url_kind(url: str) -> str:
    """'audio', 'video', 'pdf' or 'webpage' from the URL's extension."""
    clean = (url or '').split('?')[0].split('#')[0].lower()
    last = clean.rsplit('/', 1)[-1]
    ext = '.' + last.rsplit('.', 1)[1] if '.' in last else ''
    if ext in _AUDIO_EXT:
        return 'audio'
    if ext in _VIDEO_EXT:
        return 'video'
    if ext == '.pdf':
        return 'pdf'
    return 'webpage'


async def _bse_get_json(params: dict):
    """GET the announcements API as JSON. httpx first, then curl_cffi. Returns
    the row list, or None on failure. A bare string body ("No Record Found!")
    is treated as an empty result, not an error."""
    def _rows(payload):
        if isinstance(payload, dict):
            return payload.get("Table") or []
        return []

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=25.0) as client:
            r = await client.get(ANN_API, params=params, headers=BSE_HEADERS)
            if r.status_code == 200:
                try:
                    return _rows(r.json())
                except Exception:
                    # BSE answers 200 with a bare JSON string when there is nothing.
                    return []
    except Exception as e:
        print(f"BSE_FETCHER: httpx failed ({e}), trying curl_cffi...", file=sys.stderr)

    def _cffi():
        from curl_cffi import requests as cffi_requests
        proxy = os.environ.get("RESIDENTIAL_PROXY_URL")
        proxies = {"http": proxy, "https": proxy} if proxy else None
        s = cffi_requests.Session(impersonate="chrome110", proxies=proxies)
        r = s.get(ANN_API, params=params, headers=BSE_HEADERS, timeout=30)
        if r.status_code != 200:
            return None
        try:
            return _rows(r.json())
        except Exception:
            return []

    try:
        return await asyncio.to_thread(_cffi)
    except Exception as e:
        print(f"BSE_FETCHER: curl_cffi failed: {e}", file=sys.stderr)
        return None


async def _fetch_attachment(name: str) -> tuple:
    """Download a BSE attachment, trying the live then the archive path.
    Returns (pdf_bytes, url) or (None, '')."""
    if not name:
        return None, ''
    from fetchers.screener_fetcher import _stealth_download_pdf_async
    for path in ATTACH_PATHS:
        url = ATTACH_BASE.format(path=path, name=name)
        try:
            content, _final, status = await _stealth_download_pdf_async(url)
        except Exception:
            content, status = None, 0
        if content and content[:5] == b'%PDF-':
            return content, url
    return None, ''


async def fetch_bse_concall_docs_async(ticker: str, results_quarter: str = '',
                                       lookback_days: int = 150) -> list:
    """
    Find earnings-call documents filed with BSE for `ticker`.

    Returns a list of candidate dicts, newest first:
        {kind: 'transcript_pdf' | 'audio' | 'video' | 'ir_page',
         url, date, quarter, headline, source: 'bse_transcript'|'bse_audio'}

    Never raises — returns [] on any failure.
    """
    scrip = load_bse_map().get((ticker or '').strip().upper())
    if not scrip:
        print(f"BSE_FETCHER: no BSE scrip code for {ticker}", file=sys.stderr)
        return []

    from datetime import datetime, timedelta
    now = datetime.now()
    params = {
        "strCat": "-1",
        "strPrevDate": (now - timedelta(days=lookback_days)).strftime("%Y%m%d"),
        "strToDate": now.strftime("%Y%m%d"),
        "strScrip": scrip,
        "strSearch": "P",
        "strType": "C",
        "pageno": 1,
        "subcategory": "-1",
    }

    rows = await _bse_get_json(params)
    if rows is None:
        print(f"BSE_FETCHER: announcements request failed for {ticker} ({scrip})", file=sys.stderr)
        return []
    if not rows:
        print(f"BSE_FETCHER: no announcements for {ticker} ({scrip})", file=sys.stderr)
        return []

    candidates = []
    for row in rows:
        headline = (row.get("HEADLINE") or "").strip()
        subcat = (row.get("SUBCATNAME") or "").strip()
        newssub = (row.get("NEWSSUB") or "").strip()
        blob = f"{headline} {subcat} {newssub}".lower()
        news_dt = row.get("NEWS_DT") or ""

        is_transcript = (subcat.lower() == _TRANSCRIPT_SUBCAT
                         or any(k in blob for k in _TRANSCRIPT_KEYWORDS))
        is_recording = any(k in blob for k in _REC_KEYWORDS)
        if not (is_transcript or is_recording):
            continue

        # Evidence order: an explicit "Q1FY27", then the stated call date
        # ("held on May 23, 2026"), and only then the filing date — which is a
        # week or so late for transcripts and so can land in the wrong quarter.
        quarter = (_quarter_from_qfy(f"{headline} {newssub}")
                   or _quarter_from_call_date(f"{headline} {newssub}")
                   or _quarter_from_date(news_dt))
        date_str = news_dt[:10]

        if is_transcript:
            content, url = await _fetch_attachment(row.get("ATTACHMENTNAME") or "")
            if url:
                candidates.append({
                    'kind': 'transcript_pdf', 'url': url, 'date': date_str,
                    'quarter': quarter, 'headline': headline, 'source': 'bse_transcript',
                })
            continue

        # Recording filing. BSE sometimes hosts the media itself — that is the
        # cheapest possible source (a plain mp3, no video track to strip).
        av = (row.get("AUDIO_VIDEO_FILE") or "").strip()
        if av and _url_kind(av) in ('audio', 'video'):
            candidates.append({
                'kind': _url_kind(av), 'url': av, 'date': date_str,
                'quarter': quarter, 'headline': headline, 'source': 'bse_audio',
            })
            continue

        # Otherwise the media link is a clickable annotation inside the
        # intimation PDF — same trick nse_fetcher uses.
        content, _url = await _fetch_attachment(row.get("ATTACHMENTNAME") or "")
        if not content:
            continue
        try:
            from fetchers.nse_fetcher import _extract_pdf_hyperlinks, _LINK_BLOCKLIST
            links = _extract_pdf_hyperlinks(content)
        except Exception as e:
            print(f"BSE_FETCHER: link extraction failed: {e}", file=sys.stderr)
            continue

        media = [u for u in links if _url_kind(u) in ('audio', 'video')]
        if media:
            candidates.append({
                'kind': _url_kind(media[0]), 'url': media[0], 'date': date_str,
                'quarter': quarter, 'headline': headline, 'source': 'bse_audio',
            })
            continue
        page = [u for u in links
                if u.startswith('http') and not any(b in u.lower() for b in _LINK_BLOCKLIST)]
        if page:
            candidates.append({
                'kind': 'ir_page', 'url': page[0], 'date': date_str,
                'quarter': quarter, 'headline': headline, 'source': 'bse_audio',
            })

    if results_quarter:
        candidates.sort(key=lambda c: (c.get('quarter') == results_quarter,
                                       c.get('date', '')), reverse=True)
    else:
        candidates.sort(key=lambda c: c.get('date', ''), reverse=True)

    for c in candidates:
        print(f"BSE_FETCHER: {c['source']} [{c['kind']}] {c['quarter']} — {c['headline'][:70]}",
              file=sys.stderr)
    return candidates
