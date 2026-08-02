"""
concall_agent.py — Concall Analysis Agent for Kagger AI Agent Marketplace.

Fetches the latest conference call transcript from Screener.in,
produces a structured institutional-grade analysis using Gemini,
and provides a contextual chat interface for follow-up Q&A.
"""

import sys
import re
import time
import asyncio
import mimetypes
import threading
import traceback
from datetime import datetime as _dt
from io import BytesIO

import httpx
from bs4 import BeautifulSoup
from flask import request, jsonify
from google import genai
from google.genai import types

from agents.base import (
    create_agent_job, update_agent_job, get_agent_job,
    store_latest_result, get_latest_result
)
from agents.prompts.concall_prompts import (
    CONCALL_ANALYSIS_PROMPT, CONCALL_CHAT_PROMPT, build_concall_fetch_error
)

import csv
import os

def _get_company_name_from_ticker(ticker: str) -> str:
    """Read the stock master to get the full company name."""
    try:
        master_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'trendlyne_all_stocks_master.csv')
        if os.path.exists(master_file):
            with open(master_file, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader) # skip headers
                for row in reader:
                    if len(row) > 1 and row[1].strip().upper() == ticker.strip().upper():
                        return row[0].strip()
    except Exception as e:
        print(f"CONCALL_AGENT error reading stock master: {e}", file=sys.stderr)
    return ticker

_CORP_SUFFIX_RE = re.compile(
    r'[\s,]+(?:pvt\.?|private|ltd\.?|limited|inc\.?|incorporated|'
    r'corp\.?|corporation|plc|llp)\.?$',
    re.IGNORECASE,
)


def _strip_quotes(text: str) -> str:
    """Drop apostrophes so "Divi's" and "Divis" normalise to the same token."""
    return (text or '').replace("'", "").replace("’", "").replace("‘", "")


def _query_name(company_name: str) -> str:
    """
    Company name trimmed of its corporate suffix for use in a search query.

    Measured against YouTube: "Divi S Laboratories Ltd concall Q1 FY27" returns
    a single unrelated clip, while "Divi S Laboratories concall Q1 FY27" returns
    the correct call as the top two hits. The suffix is what breaks the match.
    Applied repeatedly since "Foo Pvt Ltd" carries two.
    """
    name = (company_name or '').strip()
    for _ in range(3):
        trimmed = _CORP_SUFFIX_RE.sub('', name).strip(' ,.')
        if not trimmed or trimmed == name:
            break
        name = trimmed
    return re.sub(r'\s+', ' ', name).strip() or (company_name or '').strip()


_COMPANY_STOPWORDS = {
    'ltd', 'limited', 'inc', 'incorporated', 'corp', 'corporation',
    'company', 'co', 'holdings', 'holding', 'india', 'indian',
    'industries', 'industrial', 'enterprises', 'enterprise',
    'services', 'service', 'the', 'and', 'pvt', 'private', 'group',
}


def _company_core_groups(company_name: str):
    """
    Distinctive tokens of a company name, each as a set of accepted spellings.

    "Premier Explosives Ltd" -> [{"premier"}, {"explosives"}]

    The stock master mangles possessives — "Divi's Laboratories" is stored as
    "Divi S Laboratories" — so a standalone single letter is folded into the
    preceding token as an *alternative* spelling rather than dropped:
    "Divi S Laboratories Ltd" -> [{"divi", "divis"}, {"laboratories"}].
    That covers both "Divi's Laboratories" and "Divis Laboratories", which are
    the two forms real video titles actually use.
    """
    raw = [t for t in re.split(r'[^a-z0-9]+',
                               _strip_quotes(company_name).lower()) if t]
    groups = []
    last_base = None
    for tok in raw:
        if len(tok) == 1:
            if groups and last_base:
                groups[-1].add(last_base + tok)
            continue
        if tok in _COMPANY_STOPWORDS:
            last_base = None
            continue
        groups.append({tok})
        last_base = tok
    return groups


_Q_TAG_RE = re.compile(r'\bq\s*-?\s*([1-4])\b')
_FY_TAG_RE = re.compile(r'\bfy\s*-?\s*(\d{2,4})\b')


def _title_contradicts_quarter(title_lower: str, target_q_num: str, fy_short: str) -> bool:
    """
    True when a title names a quarter or fiscal year that is NOT the target.

    Deliberately distinguishes "names a different quarter" (evidence it is the
    wrong call) from "names no quarter at all" (simply unknown — a title like
    "Earnings Call August 2026" is not contradicting). Only the former is a
    reason to reject.
    """
    if not target_q_num:
        return False
    q_tags = {m.group(1) for m in _Q_TAG_RE.finditer(title_lower)}
    if q_tags and target_q_num[1:] not in q_tags:
        return True
    fy_tags = {m.group(1)[-2:] for m in _FY_TAG_RE.finditer(title_lower)}
    if fy_tags and fy_short and fy_short not in fy_tags:
        return True
    return False


def _company_title_match(title_lower: str, company_name: str, ticker: str) -> bool:
    """
    STRICT check that a video title refers to the TARGET company. True if EITHER
    the normalized ticker appears, OR every distinctive core token appears as a
    whole word (in any accepted spelling). Rejects confusable siblings (e.g.
    "Premier Energies" when the target is "Premier Explosives"). False negatives
    are cheap here (we fall back to the PDF transcript); false positives are
    catastrophic — they would analyse the wrong company's call.
    """
    title_norm = _strip_quotes(title_lower or '').lower()
    # Branch 1: ticker rescue (length-gated; handles abbreviated titles).
    t_raw = (ticker or '').lower()
    t_norm = t_raw.replace('-', '')
    for cand in (t_raw, t_norm):
        if len(cand) >= 4 and cand in title_norm:
            return True
    # Branch 2: EVERY distinctive core token present as a whole word.
    groups = _company_core_groups(company_name)
    if groups and all(
        any(re.search(r'\b' + re.escape(alt) + r'\b', title_norm) for alt in grp)
        for grp in groups
    ):
        return True
    return False


def _get_q_fy_from_quarter(quarter_str: str) -> str:
    """Convert 'Mar 2026' into 'Q4 FY26' for better search"""
    if not quarter_str: return ""
    parts = quarter_str.split(' ')
    if len(parts) == 2:
        month, year = parts[0], parts[1]
        try:
            year_int = int(year)
            fy = year_int if month in ['Jan', 'Feb', 'Mar'] else year_int + 1
            fy_str = str(fy)[-2:]
            q_map = {'Mar': 'Q4', 'Jun': 'Q1', 'Sep': 'Q2', 'Dec': 'Q3'}
            q_str_part = q_map.get(month, "")
            if q_str_part:
                return f"{q_str_part} FY{fy_str}"
        except:
            pass
    return quarter_str

def _get_current_expected_quarter(as_of=None) -> str:
    """Most likely latest *reported* quarter label, derived from today's date.

    Keyed on the reporting month, not on the quarter itself: Indian results for
    the Jun quarter are published across Jul–Sep, so in August the latest
    reported quarter is Q1, not Q2.
      2026-08-02 -> 'Q1 FY27'   (Jun 2026 quarter)
      2026-01-10 -> 'Q3 FY26'   (Dec 2025 quarter)
    """
    from datetime import datetime
    now = as_of or datetime.now()
    m, y = now.month, now.year
    if m in (1, 2, 3):    return f"Q3 FY{str(y)[-2:]}"        # Dec quarter
    if m in (4, 5, 6):    return f"Q4 FY{str(y)[-2:]}"        # Mar quarter
    if m in (7, 8, 9):    return f"Q1 FY{str(y + 1)[-2:]}"    # Jun quarter
    return f"Q2 FY{str(y + 1)[-2:]}"                          # Sep quarter


def _expected_results_quarters(as_of=None):
    """
    (primary, previous) quarter-end labels in Screener's 'Mon YYYY' form.

    The reporting-lag heuristic is a month or so early at the start of a season
    — in early July many companies have not filed Q1 yet — so callers should
    accept either label rather than commit to the primary.
      2026-08-02 -> ('Jun 2026', 'Mar 2026')
    """
    from datetime import datetime
    now = as_of or datetime.now()
    m, y = now.month, now.year
    if m in (1, 2, 3):
        primary, prev = f"Dec {y - 1}", f"Sep {y - 1}"
    elif m in (4, 5, 6):
        primary, prev = f"Mar {y}", f"Dec {y - 1}"
    elif m in (7, 8, 9):
        primary, prev = f"Jun {y}", f"Mar {y}"
    else:
        primary, prev = f"Sep {y}", f"Jun {y}"
    return primary, prev

def _parse_iso_duration(duration_str: str) -> int:
    """Parse ISO 8601 duration (e.g. PT48M39S) to seconds."""
    import re as _re
    if not duration_str:
        return 0
    m = _re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not m:
        return 0
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)


def _youtube_data_api_search(query: str, api_key: str, max_results: int = 5) -> list:
    """
    Search YouTube using the Data API v3.
    Returns list of dicts: {url, title, channel, published_at, duration_secs}
    Falls back to empty list on any error.
    """
    try:
        search_resp = httpx.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "q": query, "type": "video", "part": "snippet",
                "maxResults": max_results, "key": api_key, "order": "relevance",
            },
            timeout=10,
        )
        search_resp.raise_for_status()
        items = search_resp.json().get("items", [])
        if not items:
            return []

        video_ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]

        # Fetch durations in one call
        durations = {}
        if video_ids:
            dur_resp = httpx.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"id": ",".join(video_ids), "part": "contentDetails", "key": api_key},
                timeout=10,
            )
            if dur_resp.status_code == 200:
                for vi in dur_resp.json().get("items", []):
                    durations[vi["id"]] = _parse_iso_duration(
                        vi.get("contentDetails", {}).get("duration", "")
                    )

        results = []
        for it in items:
            vid_id = it.get("id", {}).get("videoId", "")
            if not vid_id:
                continue
            snippet = it.get("snippet", {})
            results.append({
                "url": f"https://www.youtube.com/watch?v={vid_id}",
                "title": snippet.get("title", ""),
                "channel": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt", ""),   # ISO 8601 e.g. "2026-05-11T10:30:00Z"
                "duration_secs": durations.get(vid_id, 0),
            })
        return results

    except Exception as e:
        print(f"CONCALL_AGENT: YouTube Data API search failed for '{query}': {e}", file=sys.stderr)
        return []


def _resolve_youtube_upload_date(video_url: str):
    """
    Fetch a YouTube video page to get upload date when yt-dlp returns unknown.
    Returns (datetime | None, relative_text str) e.g. (datetime(2026,5,11), "20 hours ago")
    """
    import re as _re
    from datetime import datetime as _datetime
    try:
        resp = httpx.get(
            video_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=8,
            follow_redirects=True,
        )
        text = resp.text
        # ISO date embedded in page JSON or meta tags
        m = (_re.search(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})', text)
             or _re.search(r'"publishDate"\s*:\s*"(\d{4}-\d{2}-\d{2})', text)
             or _re.search(r'itemprop="datePublished"\s+content="(\d{4}-\d{2}-\d{2})', text))
        # Relative text e.g. "20 hours ago" or "3 days ago"
        r = _re.search(r'"dateText"\s*:\s*\{"simpleText"\s*:\s*"([^"]+)"', text)
        relative_text = r.group(1) if r else ""
        if m:
            dt = _datetime.strptime(m.group(1), "%Y-%m-%d")
            return dt, relative_text
    except Exception as e:
        print(f"CONCALL_AGENT: _resolve_youtube_upload_date failed for {video_url}: {e}", file=sys.stderr)
    return None, ""


def _search_youtube_concall(company_name: str, ticker: str, quarter: str = '') -> str:
    """
    Search YouTube for the latest earnings call video using yt-dlp ytsearch.
    Uses a single target quarter for focused search.  Falls back to Gemini
    (gemini-3.1-flash-lite) for disambiguation when no clear winner is found.
    Returns: YouTube URL or empty string if not found.
    """
    import yt_dlp
    import os as _os
    from datetime import datetime, timedelta

    recency_cutoff = datetime.now() - timedelta(days=10)
    YOUTUBE_API_KEY = _os.environ.get("YOUTUBE_API_KEY", "")

    # ── Determine the SINGLE target quarter ──
    q_fy = _get_q_fy_from_quarter(quarter) if quarter else ""
    current_q = _get_current_expected_quarter()
    target_quarter = q_fy if q_fy else current_q
    if not target_quarter:
        target_quarter = "latest"

    print(f"CONCALL_AGENT: Target quarter for YouTube search: {target_quarter}", file=sys.stderr)

    # Extract quarter number + FY variants for precise query generation and title matching
    # e.g. "Q4 FY26" -> target_q_num="q4", fy_short="26", fy_variants=["fy26", "fy2026", "fy 2026", "fy2025-26", "fy 2025-26", ...]
    target_q_num = ""
    fy_short = ""
    fy_variants = []

    if target_quarter and target_quarter != "latest":
        import re as _re
        match = _re.search(r'Q([1-4])\s+FY(\d{2})', target_quarter, _re.IGNORECASE)
        if match:
            target_q_num = f"q{match.group(1)}"
            fy_short = match.group(2)
            try:
                yy = int(fy_short)
                prev_yy = yy - 1
                fy_variants = [
                    f"fy{yy}",                          # fy26
                    f"fy20{yy}",                        # fy2026
                    f"fy 20{yy}",                       # fy 2026
                    f"fy20{prev_yy:02d}-{yy}",          # fy2025-26
                    f"fy 20{prev_yy:02d}-{yy}",         # fy 2025-26
                    f"fy20{prev_yy:02d}-20{yy}",        # fy2025-2026
                    f"fy{prev_yy:02d}-{yy}",            # fy25-26
                    f"fy {prev_yy:02d}-{yy}",           # fy 25-26
                    f"20{prev_yy:02d}-20{yy}",          # 2025-2026
                    f"20{prev_yy:02d}-{yy}",            # 2025-26
                ]
            except Exception:
                pass

    # ── Build focused search queries — including FY variants with priority ──
    # Priority: target_quarter (FY26) first, then FY2025-26, then secondary phrases
    q_search_variants = [target_quarter]
    if target_q_num and fy_short:
        try:
            yy = int(fy_short)
            prev_yy = yy - 1
            q_upper = target_q_num.upper()
            
            # Priority 2: QX FY2025-26
            q_search_variants.append(f"{q_upper} FY20{prev_yy:02d}-{yy}")
            
            # Secondary phrases:
            q_search_variants.extend([
                f"{q_upper} FY20{yy}",
                f"{q_upper} FY 20{yy}",
                f"{q_upper} FY 20{prev_yy:02d}-{yy}",
            ])
        except Exception:
            pass

    # Queries are emitted in priority tiers because the list is capped below —
    # the highest-yield forms must survive the cut.
    q_name = _query_name(company_name)
    has_name = bool(q_name) and q_name.upper() != ticker.upper()
    primary_q = q_search_variants[0] if q_search_variants else ''

    search_queries = []

    # Tier 1 — company name + primary quarter label. Indian concall uploads are
    # titled by company name, never by NSE symbol, so this form leads.
    if has_name and primary_q:
        search_queries.append(f"{q_name} concall {primary_q}")
        search_queries.append(f"{q_name} earnings call {primary_q}")

    # Tier 2 — undated. Titles like "Earnings Call August 2026" carry no quarter
    # token at all and are invisible to every quarter-tagged query.
    if has_name:
        search_queries.append(f"{q_name} earnings call")
        search_queries.append(f"{q_name} concall")

    # Tier 3 — ticker form, for the channels that do use the NSE symbol.
    if primary_q:
        search_queries.append(f"{ticker} concall {primary_q}")
        search_queries.append(f"{ticker} earnings call {primary_q}")

    # Tier 4 — remaining FY spellings (FY2025-26, FY 2026, ...).
    for q_var in q_search_variants[1:]:
        if has_name:
            search_queries.append(f"{q_name} concall {q_var}")
        search_queries.append(f"{ticker} concall {q_var}")

    # Deduplicate while preserving order
    seen = set()
    unique_queries = []
    for q in search_queries:
        q_lower = q.lower()
        if q_lower not in seen:
            seen.add(q_lower)
            unique_queries.append(q)

    # Cap to keep YouTube API / search requests bounded
    unique_queries = unique_queries[:10]

    proxy_url = _os.environ.get("RESIDENTIAL_PROXY_URL")

    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'extract_flat': True,
    }
    if proxy_url:
        ydl_opts['proxy'] = proxy_url
    
    cookie_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), 'youtube_cookies.txt')
    if _os.path.exists(cookie_path):
        ydl_opts['cookiefile'] = cookie_path

    # ── Helper functions ──
    def _parse_upload_date(vid):
        """Parse upload_date from video metadata.  Returns datetime or None."""
        upload_date_str = vid.get('upload_date')
        if upload_date_str and len(upload_date_str) == 8:
            try:
                return datetime.strptime(upload_date_str, "%Y%m%d")
            except Exception:
                pass
        return None

    def _title_matches_quarter(title_lower):
        """Check if video title contains the *specific* target quarter and fiscal year."""
        if not target_q_num:
            return False
            
        # Quarter indicators (e.g. "q4", "quarter 4", "q-4")
        q_indicators = [
            target_q_num,
            target_q_num.replace("q", "quarter "),
            target_q_num.replace("q", "q-"),
        ]
        has_quarter = any(ind in title_lower for ind in q_indicators)
        
        # FY indicators (e.g. "fy26", "fy 2026", "2025-26", etc.)
        has_fy = False
        if fy_variants:
            has_fy = any(var in title_lower for var in fy_variants)
        else:
            # If no target quarter format parsed, fallback to matching full target quarter string
            has_fy = target_quarter.lower() in title_lower
            
        return has_quarter and has_fy

    def _is_concall_video(title_lower):
        """Check if the title suggests an earnings / conference call."""
        return any(kw in title_lower for kw in [
            'earning', 'concall', 'conference call', 'con call', 'analyst meet'
        ])

    def _is_preferred_channel(channel):
        """Check if channel is a known preferred concall aggregator."""
        if not channel:
            return False
        chan_lower = channel.lower()
        return ("trendlyne" in chan_lower or "alphastreet" in chan_lower
                or "concall" in chan_lower or "alfafinder" in chan_lower)

    def _title_matches_company(title_lower):
        return _company_title_match(title_lower, company_name, ticker)

    # ── Collect candidates across all queries ──
    all_candidates = []       # list of dicts with metadata
    seen_urls = set()

    for query in unique_queries:
        print(f"CONCALL_AGENT: YouTube search: '{query}'", file=sys.stderr)

        # ── Primary: YouTube Data API v3 ──
        api_results = []
        if YOUTUBE_API_KEY:
            api_results = _youtube_data_api_search(query, YOUTUBE_API_KEY, max_results=5)

        # ── Fallback: yt-dlp (if API returned nothing) ──
        ytdlp_entries = []
        if not api_results:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    results = ydl.extract_info(f"ytsearch5:{query}", download=False)
                    ytdlp_entries = results.get('entries', []) or []
                except Exception as e:
                    print(f"CONCALL_AGENT yt-dlp fallback error for '{query}': {e}", file=sys.stderr)

        # ── Process YouTube Data API results ──
        for item in api_results:
            url = item['url']
            if not url or url in seen_urls:
                continue
            duration = item['duration_secs']
            if duration and duration < 1200:
                continue
            seen_urls.add(url)

            title = item['title']
            title_lower = title.lower()
            channel = item['channel']

            upload_dt = None
            published_at = item.get('published_at', '')
            if published_at:
                try:
                    upload_dt = datetime.strptime(published_at[:10], "%Y-%m-%d")
                except Exception:
                    pass

            is_recent = (upload_dt >= recency_cutoff) if upload_dt else None
            matches_quarter = _title_matches_quarter(title_lower)
            is_concall = _is_concall_video(title_lower)
            is_preferred = _is_preferred_channel(channel)
            matches_company = _title_matches_company(title_lower)

            if is_recent and matches_quarter and is_concall and is_preferred and matches_company:
                print(f"CONCALL_AGENT: ✓ Perfect match (YouTube API, preferred): {url}  (title: {title})", file=sys.stderr)
                return url

            all_candidates.append({
                'url': url,
                'title': title,
                'upload_date': upload_dt.strftime('%Y%m%d') if upload_dt else 'unknown',
                'relative_text': '',
                'duration_mins': round(duration / 60, 1) if duration else 'unknown',
                'channel': channel,
                'is_recent': is_recent,
                'matches_quarter': matches_quarter,
                'is_concall': is_concall,
                'is_preferred': is_preferred,
                'matches_company': matches_company,
            })

        # ── Process yt-dlp fallback results ──
        for vid in ytdlp_entries:
            if not vid:
                continue
            url = vid.get('webpage_url', vid.get('url', ''))
            if not url or url in seen_urls:
                continue
            duration = vid.get('duration', 0)
            if duration and duration < 1200:
                continue
            seen_urls.add(url)

            title = vid.get('title', '')
            title_lower = title.lower()
            upload_dt = _parse_upload_date(vid)
            is_recent = (upload_dt >= recency_cutoff) if upload_dt else None
            matches_quarter = _title_matches_quarter(title_lower)
            is_concall = _is_concall_video(title_lower)
            channel = vid.get('channel', vid.get('uploader', 'unknown'))
            is_preferred = _is_preferred_channel(channel)
            matches_company = _title_matches_company(title_lower)

            if is_recent and matches_quarter and is_concall and is_preferred and matches_company:
                print(f"CONCALL_AGENT: ✓ Perfect match (yt-dlp, preferred): {url}  (title: {title})", file=sys.stderr)
                return url

            all_candidates.append({
                'url': url,
                'title': title,
                'upload_date': vid.get('upload_date', 'unknown'),
                'relative_text': '',
                'duration_mins': round(duration / 60, 1) if duration else 'unknown',
                'channel': channel,
                'is_recent': is_recent,
                'matches_quarter': matches_quarter,
                'is_concall': is_concall,
                'is_preferred': is_preferred,
                'matches_company': matches_company,
            })

    # ── Resolve missing upload dates via video page fetch ──
    for c in all_candidates:
        if c['is_recent'] is None:
            resolved_dt, relative_text = _resolve_youtube_upload_date(c['url'])
            if resolved_dt:
                c['upload_date'] = resolved_dt.strftime('%Y%m%d')
                c['relative_text'] = relative_text
                c['is_recent'] = resolved_dt >= recency_cutoff
                print(
                    f"CONCALL_AGENT: Resolved date for {c['url']}: "
                    f"{resolved_dt.date()} ({relative_text})", file=sys.stderr
                )

    # ── No perfect match — ask Gemini to disambiguate ──
    if not all_candidates:
        print("CONCALL_AGENT: No suitable YouTube video found across all queries", file=sys.stderr)
        return ""

    # ── Pre-filter candidates before sending to Gemini (enforced in code, not by prompt) ──

    # Rule 0: Drop candidates whose TITLE does not refer to the target company.
    #         Prevents wrong-company videos (e.g. "Premier Energies" when searching
    #         "Premier Explosives") from ever reaching Gemini. Runs before Rule A/B.
    company_matched = [c for c in all_candidates if c.get('matches_company')]
    if not company_matched:
        print(
            f"CONCALL_AGENT: No candidate title matches company "
            f"'{company_name}' ({ticker}) — falling back to PDF transcript",
            file=sys.stderr
        )
        return ""
    if len(company_matched) != len(all_candidates):
        print(
            f"CONCALL_AGENT: Rule 0 dropped "
            f"{len(all_candidates) - len(company_matched)} wrong-company candidate(s)",
            file=sys.stderr
        )
    all_candidates = company_matched

    # Rule A: If any non-ALFAFINDER candidate is a concall, exclude ALFAFINDER entirely.
    #         ALFAFINDER is last resort — only considered when nothing else is available.
    non_alfa = [c for c in all_candidates if "alfafinder" not in (c['channel'] or '').lower()]
    if non_alfa and any(c['is_concall'] for c in non_alfa):
        print("CONCALL_AGENT: Non-ALFAFINDER concall candidate exists — excluding ALFAFINDER", file=sys.stderr)
        all_candidates = non_alfa

    # Rule B: If any candidate matches the target quarter, only send those to Gemini.
    #         Prevents wrong-quarter videos from being selected when the correct quarter exists.
    quarter_matched = [c for c in all_candidates if c['matches_quarter']]
    if quarter_matched:
        print(f"CONCALL_AGENT: Pre-filtered to {len(quarter_matched)} quarter-matching candidate(s)", file=sys.stderr)
        all_candidates = quarter_matched
    elif target_q_num:
        # Nothing confirms the target quarter. Previously the best remaining
        # candidate was returned anyway and the caller then LABELLED it with the
        # target quarter — a confidently wrong answer. Keep only candidates that
        # do not contradict the target and are recent enough to be plausible;
        # if none survive, return '' so the caller falls through to the exchange
        # filing and IR-website sources instead of guessing.
        plausible = [
            c for c in all_candidates
            if not _title_contradicts_quarter((c['title'] or '').lower(), target_q_num, fy_short)
            and c.get('is_recent') is True
        ]
        if not plausible:
            print(
                f"CONCALL_AGENT: No YouTube candidate confirms {target_quarter} "
                f"— skipping YouTube so other sources can be tried", file=sys.stderr
            )
            return ""
        print(
            f"CONCALL_AGENT: No explicit {target_quarter} match; keeping "
            f"{len(plausible)} recent candidate(s) that don't contradict it", file=sys.stderr
        )
        all_candidates = plausible

    print(
        f"CONCALL_AGENT: {len(all_candidates)} candidate(s) going to Gemini for final selection...",
        file=sys.stderr
    )

    candidates_text = ""
    for i, c in enumerate(all_candidates, 1):
        chan_lower = c['channel'].lower() if c['channel'] else ""
        if "trendlyne" in chan_lower or "alphastreet" in chan_lower or "concall" in chan_lower:
            pref_label = " [Preferred: Trendlyne/AlphaStreet/Concall]"
        elif "alfafinder" in chan_lower:
            pref_label = " [ALFAFINDER]"
        else:
            pref_label = ""

        date_display = c.get('relative_text') or c['upload_date']
        recency_note = ""
        if c['is_recent'] is True:
            recency_note = " ✓ within last 10 days"
        elif c['is_recent'] is False:
            recency_note = " (older than 10 days)"

        candidates_text += (
            f"Video {i}:\n"
            f"  Title: {c['title']}\n"
            f"  Upload: {date_display}{recency_note}\n"
            f"  Duration: {c['duration_mins']} minutes\n"
            f"  Channel: {c['channel']}{pref_label}\n"
            f"  URL: {c['url']}\n\n"
        )

    print(f"CONCALL_AGENT: Candidates Text:\n{candidates_text}", file=sys.stderr)

    gemini_prompt = (
        "You are helping identify the correct earnings conference call (concall) "
        "video from YouTube search results.\n\n"
        f"Company: {company_name} (Ticker: {ticker})\n"
        f"Target Quarter: {target_quarter}\n\n"
        f"Here are the candidate videos found:\n\n{candidates_text}\n"
        f"Which video is most likely the {target_quarter} earnings conference call "
        f"for {company_name}?\n\n"
        "Rules:\n"
        "1. The video MUST be an earnings call / concall / conference call / analyst "
        "meet — NOT a news clip, analysis, or commentary.\n"
        f"2. It should match the target quarter ({target_quarter}) as closely as possible.\n"
        "3. Prefer videos from known financial channels: Trendlyne (@trendlyne), "
        "AlphaStreet India (@AlphaStreetIndia), Concall (@concall_in), or the "
        "company's official channel.\n"
        "4. The video title MUST refer to the SAME company. It may use the full "
        "company name even though only the ticker is given here, but you MUST "
        "REJECT a video for a DIFFERENT company that merely shares a word or prefix. "
        "E.g. for 'Premier Explosives Ltd', REJECT a 'Premier Energies' video — "
        "these are different companies. If unsure, respond 'NONE'.\n"
        "5. When multiple videos are equally plausible, prefer the most recently "
        "uploaded one (marked '✓ within last 10 days').\n"
        "6. Only respond 'NONE' if no video could plausibly be an earnings call for "
        "this company.\n\n"
        'Respond with ONLY the video number (e.g., "1") or "NONE".  '
        "No explanation needed."
    )

    def _deterministic_pick(reason):
        """
        Best candidate by the code-enforced signals alone.

        Used whenever the LLM tie-break is unavailable or unusable. Everything
        here has already cleared Rule 0 (company match) and, when any matched,
        Rule B (quarter match) — so it is a sound choice, and far better than
        discarding a correct video because one API call hiccuped. Only an
        explicit "NONE" from the model is treated as a real rejection.
        """
        if not all_candidates:   # unreachable today (Rule 0 returns early), but
            return ""            # this runs inside an except handler — stay total

        def _sort_key(c):
            raw = str(c.get('upload_date') or '')
            uploaded = raw if (len(raw) == 8 and raw.isdigit()) else ''
            return (
                bool(c.get('matches_quarter')),
                bool(c.get('is_concall')),
                bool(c.get('is_preferred')),
                c.get('is_recent') is True,
                uploaded,
            )

        best = sorted(all_candidates, key=_sort_key, reverse=True)[0]
        print(
            f"CONCALL_AGENT: {reason} — using top-ranked candidate: "
            f"{best['url']}  (title: {best['title']})", file=sys.stderr
        )
        return best['url']

    try:
        GOOGLE_API_KEY = _os.getenv("GOOGLE_API_KEY")
        if not GOOGLE_API_KEY:
            return _deterministic_pick("No API key for Gemini tie-break")

        _genai_client = genai.Client(api_key=GOOGLE_API_KEY)
        response = _genai_client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=[types.Part.from_text(text=gemini_prompt)],
        )

        answer = (response.text.strip() if response.text else "").upper()
        print(f"CONCALL_AGENT: Gemini picked: '{answer}'", file=sys.stderr)

        if answer == "NONE":
            # A deliberate rejection by the model — respect it.
            print("CONCALL_AGENT: Gemini says none of the candidates match", file=sys.stderr)
            return ""

        # Parse the video number from Gemini's response
        video_num = None
        try:
            video_num = int(answer.strip().rstrip('.'))
        except ValueError:
            match = re.search(r'\b(\d+)\b', answer)
            if match:
                video_num = int(match.group(1))

        if video_num and 1 <= video_num <= len(all_candidates):
            chosen = all_candidates[video_num - 1]
            print(
                f"CONCALL_AGENT: Gemini selected video {video_num}: "
                f"{chosen['url']}  (title: {chosen['title']})", file=sys.stderr
            )
            return chosen['url']

        return _deterministic_pick(f"Could not parse Gemini response '{answer}'")

    except Exception as e:
        return _deterministic_pick(f"Gemini tie-break failed ({e})")


def _quarter_end_date(results_quarter: str):
    """'Jun 2026' -> date(2026, 6, 30). None when unparseable."""
    from datetime import date
    m = re.match(r'([A-Za-z]{3})\s+(\d{4})$', (results_quarter or '').strip())
    if not m:
        return None
    month = {'mar': (3, 31), 'jun': (6, 30), 'sep': (9, 30), 'dec': (12, 31)}.get(
        m.group(1).lower())
    if not month:
        return None
    try:
        return date(int(m.group(2)), month[0], month[1])
    except ValueError:
        return None


async def _probe_wordpress_media(origin: str, homepage_html: str = '') -> list:
    """
    Ask a WordPress site's media library directly for earnings-call files.

    Many Indian IR sites run WordPress, which exposes every uploaded file — with
    its **upload date** — at /wp-json/wp/v2/media. That date is the signal the
    HTML crawl fundamentally lacks: crawling only ever sees a filename, so a
    file named 'earnings_call.mp3' is unverifiable, whereas an upload date can
    be checked against the quarter end.

    Returns [{url, date, mime}] newest-first, or [] when the site isn't
    WordPress or the endpoint is disabled.
    """
    from urllib.parse import urlparse

    if homepage_html and not any(
            marker in homepage_html.lower()
            for marker in ('wp-content', 'wp-json', 'wp-includes')):
        return []

    parsed = urlparse(origin)
    if not parsed.scheme or not parsed.netloc:
        return []
    base = f"{parsed.scheme}://{parsed.netloc}"

    found = {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
               "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            for term in ('earnings', 'concall', 'conference call'):
                try:
                    r = await client.get(
                        f"{base}/wp-json/wp/v2/media",
                        params={"search": term, "per_page": 50,
                                "orderby": "date", "order": "desc"},
                        headers=headers,
                    )
                    if r.status_code != 200:
                        continue
                    items = r.json()
                except Exception:
                    continue
                if not isinstance(items, list):
                    continue
                for it in items:
                    mime = str(it.get('mime_type', ''))
                    url = it.get('source_url') or ''
                    if not url or not (mime.startswith('audio/') or mime.startswith('video/')):
                        continue
                    found[url] = {'url': url, 'date': str(it.get('date', ''))[:10], 'mime': mime}
    except Exception as e:
        print(f"CONCALL_AGENT: WordPress media probe failed: {e}", file=sys.stderr)
        return []

    out = sorted(found.values(), key=lambda x: x['date'], reverse=True)
    if out:
        print(f"CONCALL_AGENT: WordPress media library exposed {len(out)} audio/video file(s)",
              file=sys.stderr)
    return out


async def _fetch_ir_website_audio(ticker: str, company_name: str = '',
                                  results_quarter: str = '', seed_url: str = None) -> dict:
    """
    Best-effort: find a downloadable earnings-call audio file on the company's
    Investor Relations website. Starts from `seed_url` (e.g. an IR page link
    pulled from an NSE filing — the most reliable seed) or, failing that,
    discovers the company website via Screener. Company-correct by construction
    — it never leaves the company's own domain.

    Returns ``{url, quarter_confirmed, via}``. ``quarter_confirmed`` says whether
    the target quarter was actually evidenced (by the filename or by an upload
    date after the quarter end) rather than merely assumed — the caller uses it
    to decide whether this outranks YouTube or falls below it.
    """
    from urllib.parse import urljoin, urlparse

    empty = {'url': '', 'quarter_confirmed': False, 'via': ''}
    start_url = seed_url or ""
    if not start_url:
        try:
            from fetchers.screener_fetcher import fetch_company_website_async
            start_url = await fetch_company_website_async(ticker)
        except Exception as e:
            print(f"CONCALL_AGENT: IR website discovery failed: {e}", file=sys.stderr)
            start_url = ""
    if not start_url:
        return empty

    from fetchers.screener_fetcher import _stealth_get_html_async

    # Quarter tokens for ranking candidates (best-effort).
    # Split into two strengths on purpose. IR sites keep every past call in the
    # same directory, so a loose token silently ties the right file with the
    # wrong one and the pick ends up riding on stable-sort order:
    #   * q_tokens  — quarter AND fiscal year together ("q1fy27"): decisive.
    #   * fy_tokens — fiscal year only ("fy27"): weak, since it matches all four
    #                 quarters of that year.
    q_tokens = []
    fy_tokens = []
    try:
        q_fy = _get_q_fy_from_quarter(results_quarter) if results_quarter else ""
        m = re.search(r'Q([1-4])\s*FY(\d{2})', q_fy or "", re.IGNORECASE)
        if m:
            qn, yy = m.group(1), int(m.group(2))
            q_tokens = [f"q{qn}fy{yy}", f"q{qn}-fy{yy}", f"q{qn}_fy{yy}",
                        f"q{qn} fy{yy}", f"q{qn}fy20{yy}", f"q{qn}_fy20{yy}",
                        f"q{qn}-fy20{yy}"]
            fy_tokens = [f"fy{yy}", f"fy20{yy}", f"20{yy-1}-{yy}", f"20{yy-1}-20{yy}"]
    except Exception:
        pass

    # Any quarter tag at all — used to demote a prior quarter's recording.
    _other_q_re = re.compile(r'q([1-4])\s*[-_]?\s*fy\s*(\d{2,4})', re.IGNORECASE)

    base_netloc = urlparse(start_url).netloc
    seen = set()
    audio_found = []
    homepage_html = ['']

    async def _scan(url, depth):
        if not url or url in seen or len(seen) >= 6 or depth > 2:
            return
        seen.add(url)
        try:
            text, final_url, status = await _stealth_get_html_async(url, follow_redirects=True)
            if status != 200 or not text:
                return
        except Exception:
            return
        if depth == 0:
            homepage_html[0] = text          # used to sniff for WordPress
        soup = BeautifulSoup(text, 'html.parser')
        follow = []
        for a in soup.find_all('a', href=True):
            href = urljoin(final_url or url, a['href'].strip())
            label = ((a.get_text() or '') + ' ' + href).lower()
            kind = _get_url_type(href)
            if kind in ('audio', 'video'):
                audio_found.append(href)
            elif (urlparse(href).netloc == base_netloc and any(
                    k in label for k in ('investor', 'financial', 'earnings call',
                                         'concall', 'audio', 'recording'))):
                follow.append(href)
        for f in follow[:3]:
            await _scan(f, depth + 1)

    try:
        await _scan(start_url, 0)
    except Exception as e:
        print(f"CONCALL_AGENT: IR crawl error: {e}", file=sys.stderr)

    def _score(u):
        ul = u.lower()
        s = 0
        if q_tokens and any(t in ul for t in q_tokens):
            s += 10                                  # exact quarter + fiscal year
        elif q_tokens and _other_q_re.search(ul):
            s -= 10                                  # tagged with a DIFFERENT quarter
        elif fy_tokens and any(t in ul for t in fy_tokens):
            s += 3                                   # right year, quarter unstated
        if any(k in ul for k in ('earning', 'concall', 'conference', 'investor', 'audio', 'recording')):
            s += 1
        return s

    # WordPress media library — one request, and unlike the crawl it returns
    # upload dates, so a file the filename can't vouch for can still be pinned
    # to the quarter. Tried before falling back to filename-only scoring.
    q_end = _quarter_end_date(results_quarter)
    wp_items = await _probe_wordpress_media(start_url, homepage_html[0])
    for item in wp_items:
        if item['url'] not in audio_found:
            audio_found.append(item['url'])
    if q_end and wp_items:
        from datetime import date as _date
        for item in wp_items:
            if _score(item['url']) < 0:
                continue                              # filename says another quarter
            try:
                y, mo, d = (int(x) for x in item['date'].split('-'))
                uploaded = _date(y, mo, d)
            except Exception:
                continue
            # Published after the quarter closed and within a normal reporting
            # window — that is real evidence, not a filename guess.
            if 0 <= (uploaded - q_end).days <= 120:
                print(
                    f"CONCALL_AGENT: IR audio confirmed by upload date "
                    f"({item['date']}, quarter ended {q_end}): {item['url']}", file=sys.stderr
                )
                return {'url': item['url'], 'quarter_confirmed': True, 'via': 'wp_media'}

    if not audio_found:
        return empty

    audio_found.sort(key=_score, reverse=True)
    best = audio_found[0]
    best_score = _score(best)

    # Every candidate is tagged with a quarter that is not the one we want —
    # usually because this quarter's recording is not published yet. Returning
    # the closest match would hand the caller a previous quarter's call which it
    # would then label as the target quarter. No answer is the honest answer.
    if best_score < 0:
        print(
            f"CONCALL_AGENT: IR website has {len(audio_found)} recording(s) but none "
            f"for {results_quarter or 'the target quarter'} (best: {best})",
            file=sys.stderr
        )
        return empty

    # >= 10 means the filename carried the exact quarter AND fiscal year.
    confirmed = best_score >= 10
    print(
        f"CONCALL_AGENT: IR website audio found: {best} "
        f"(score {best_score}, quarter {'confirmed' if confirmed else 'UNconfirmed'}, "
        f"{len(audio_found)} candidate(s))", file=sys.stderr
    )
    return {'url': best, 'quarter_confirmed': confirmed, 'via': 'crawl'}


async def _fetch_nse_transcript_candidate(ticker: str, results_quarter: str = '') -> dict:
    """Written concall transcript filed with NSE -> {url, quarter}."""
    try:
        from fetchers.nse_fetcher import fetch_nse_concall_transcript_async
        rec = await fetch_nse_concall_transcript_async(ticker, results_quarter)
    except Exception as e:
        print(f"CONCALL_AGENT: NSE transcript lookup failed: {e}", file=sys.stderr)
        return {'url': '', 'quarter': ''}
    return {'url': rec.get('pdf_url', ''), 'quarter': rec.get('quarter', '')}


async def _fetch_bse_transcript_candidate(ticker: str, results_quarter: str = '') -> dict:
    """Written concall transcript filed with BSE -> {url, quarter}."""
    try:
        from fetchers.bse_fetcher import fetch_bse_concall_docs_async
        cands = await fetch_bse_concall_docs_async(ticker, results_quarter)
    except Exception as e:
        print(f"CONCALL_AGENT: BSE transcript lookup failed: {e}", file=sys.stderr)
        return {'url': '', 'quarter': ''}
    for c in cands:
        if c.get('kind') == 'transcript_pdf':
            return {'url': c['url'], 'quarter': c.get('quarter', '')}
    return {'url': '', 'quarter': ''}


# Injected at route-registration time; None when Perplexity isn't configured.
_call_perplexity_search_fn = None


async def _web_search_concall_media(ticker: str, company_name: str = '',
                                    results_quarter: str = '') -> str:
    """
    Last-resort search for a concall recording, restricted to the company's OWN
    domain.

    Every other source in the cascade is company-correct by construction — keyed
    to an exchange symbol or confined to the company's website. A web search is
    not, and an open query can confidently return a competitor's file. Pinning
    the search to the company's own domain keeps that guarantee; without a
    resolvable domain this step is skipped entirely rather than run unrestricted.
    """
    if _call_perplexity_search_fn is None:
        print("CONCALL_AGENT: web search not configured — skipping", file=sys.stderr)
        return ""

    from urllib.parse import urlparse
    try:
        from fetchers.screener_fetcher import fetch_company_website_async
        site = await fetch_company_website_async(ticker)
    except Exception:
        site = ""
    domain = urlparse(site).netloc.lower().lstrip('www.') if site else ""
    if not domain:
        print("CONCALL_AGENT: no company domain — skipping web search", file=sys.stderr)
        return ""

    q_fy = _get_q_fy_from_quarter(results_quarter) if results_quarter else ""
    query = f"{company_name or ticker} {q_fy} earnings conference call audio recording".strip()

    after = None
    q_end = _quarter_end_date(results_quarter)
    if q_end:
        after = f"{q_end.month}/{q_end.day}/{q_end.year}"

    try:
        results = await asyncio.to_thread(
            _call_perplexity_search_fn, query,
            [domain],          # search_domain_filter
            after,             # search_after_date_filter
            None,              # search_recency_filter
            10,                # max_results
        )
    except Exception as e:
        print(f"CONCALL_AGENT: Perplexity search failed: {e}", file=sys.stderr)
        return ""

    for r in (results or []):
        url = (r.get('url') or '').strip()
        if not url:
            continue
        # Only accept a real media file, and only on the company's own domain.
        if urlparse(url).netloc.lower().lstrip('www.') != domain:
            continue
        if _get_url_type(url) in ('audio', 'video'):
            print(f"CONCALL_AGENT: web search found media on {domain}: {url}", file=sys.stderr)
            return url

    print(f"CONCALL_AGENT: web search on {domain} returned no media files", file=sys.stderr)
    return ""


# =====================================================================
# QUARTER MAPPING HELPERS
# =====================================================================

# Maps publication month → financial quarter end month
# e.g., results published in Nov are for the Sep quarter
_MONTH_TO_QUARTER = {
    1: 'Dec', 2: 'Dec', 3: 'Mar', 4: 'Mar',
    5: 'Mar', 6: 'Jun', 7: 'Jun', 8: 'Jun',
    9: 'Sep', 10: 'Sep', 11: 'Sep', 12: 'Dec'
}


def _date_to_quarter(date_str: str) -> str:
    """
    Convert a Screener.in document date (e.g., 'Nov 2025') into its
    corresponding financial quarter label (e.g., 'Sep 2025').
    Returns empty string on failure.
    """
    if not date_str:
        return ''
    # Clean common artifacts
    clean = date_str.replace(u'\xa0', ' ').strip()
    for fmt in ['%b %Y', '%B %Y', '%d %b %Y', '%b %d, %Y']:
        try:
            parsed = _dt.strptime(clean, fmt)
            q_month = _MONTH_TO_QUARTER[parsed.month]
            # If the quarter month is Dec but pub month is Jan/Feb, it's the previous year
            q_year = parsed.year
            if parsed.month in (1, 2) and q_month == 'Dec':
                q_year -= 1
            return f"{q_month} {q_year}"
        except ValueError:
            continue
    return ''


async def _fetch_screener_quarter_and_name(ticker: str):
    """
    One pass over Screener.in for the two things the pipeline needs up front:
      * the latest quarterly results column header (e.g. 'Jun 2026')
      * the canonical company name from the page <h1>

    Checks BOTH consolidated and standalone URLs and keeps whichever quarter is
    newer. Downloads no PDFs — HTML only.

    The name matters because the local stock master mangles possessives
    ("Divi's Laboratories" is stored as "Divi S Laboratories") whereas Screener
    renders them cleanly ("Divis Laboratories Ltd") — and since this page is
    being fetched anyway, the better name is free.

    Returns (quarter, company_name); either may be '' on failure.
    """
    try:
        from fetchers.screener_fetcher import _stealth_get_html_async

        consolidated_url = f"https://www.screener.in/company/{ticker}/consolidated/"
        standalone_url = f"https://www.screener.in/company/{ticker}/"
        quarter_pattern = re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}$', re.IGNORECASE)

        async def _parse_page(url):
            """-> (quarter, company_name), both '' on failure."""
            try:
                # Screener sits behind Cloudflare; the stealth ladder
                # (httpx -> curl_cffi -> curl_cffi+proxy) is what every other
                # Screener call in the codebase uses. A bare httpx GET here
                # silently returned '' from datacenter IPs, which then cascaded
                # into a wrong derived quarter downstream.
                text, _final_url, status = await _stealth_get_html_async(url)
                if status != 200 or not text:
                    return "", ""
                soup = BeautifulSoup(text, 'html.parser')

                name = ""
                h1 = soup.find('h1')
                if h1:
                    name = re.sub(r'\s+', ' ', h1.get_text(strip=True)).strip()

                quarter = ""
                quarters_section = soup.find('section', id='quarters')
                if quarters_section:
                    table = quarters_section.find('table')
                    header_row = table.find('thead') if table else None
                    if header_row:
                        headers_list = [th.get_text(strip=True) for th in header_row.find_all('th')]
                        quarter_headers = [h for h in headers_list if quarter_pattern.match(h.strip())]
                        if quarter_headers:
                            quarter = quarter_headers[-1].strip()
                return quarter, name
            except Exception:
                return "", ""

        # Fetch both in parallel for speed
        (consol_q, consol_name), (standalone_q, standalone_name) = await asyncio.gather(
            _parse_page(consolidated_url),
            _parse_page(standalone_url)
        )

        company_name = consol_name or standalone_name
        if company_name:
            print(f"CONCALL_AGENT: Screener company name for {ticker}: {company_name}", file=sys.stderr)

        # Compare and pick the newest
        best = ""
        if consol_q and standalone_q:
            from fetchers.screener_fetcher import _compare_quarter_strings
            if _compare_quarter_strings(standalone_q, consol_q) > 0:
                best = standalone_q
                print(f"CONCALL_AGENT: Latest results quarter for {ticker}: {best} (from standalone, newer than consolidated {consol_q})", file=sys.stderr)
            else:
                best = consol_q
                print(f"CONCALL_AGENT: Latest results quarter for {ticker}: {best} (from consolidated)", file=sys.stderr)
        elif consol_q:
            best = consol_q
            print(f"CONCALL_AGENT: Latest results quarter for {ticker}: {best} (consolidated only)", file=sys.stderr)
        elif standalone_q:
            best = standalone_q
            print(f"CONCALL_AGENT: Latest results quarter for {ticker}: {best} (standalone only)", file=sys.stderr)
        else:
            print(f"CONCALL_AGENT: Could not determine latest results quarter for {ticker}", file=sys.stderr)

        return best, company_name
    except Exception as e:
        print(f"CONCALL_AGENT: _fetch_screener_quarter_and_name failed for {ticker}: {e}", file=sys.stderr)
        return '', ''


async def _extract_text_from_webpage(url: str, max_chars: int = 80000) -> str:
    """
    Extract text content from a webpage URL (for concall transcripts
    hosted on sites like Trendlyne, MoneyControl, etc.).
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, headers=headers, timeout=30.0)
            response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        # Remove script and style elements
        for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
            tag.decompose()

        # Get text
        text = soup.get_text(separator='\n', strip=True)

        # Clean up excessive whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        cleaned = '\n'.join(lines)

        return cleaned[:max_chars]
    except Exception as e:
        print(f"CONCALL_AGENT: Failed to extract text from webpage {url}: {e}", file=sys.stderr)
        return ''


# Audio/video extensions for URL type detection
_AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.ogg', '.aac', '.flac', '.wma', '.opus'}
_VIDEO_EXTENSIONS = {'.mp4', '.webm', '.mkv', '.avi', '.mov', '.flv', '.wmv'}


def _get_url_type(url: str) -> str:
    """
    Determine the type of content at a URL based on its extension.
    Returns: 'pdf', 'audio', 'video', 'youtube', or 'webpage'
    """
    # Check for YouTube URLs first (they don't have file extensions)
    lower = url.lower()
    if any(p in lower for p in ['youtube.com/watch', 'youtu.be/', 'youtube.com/live']):
        return 'youtube'

    # Strip query params and fragments for extension detection
    clean = url.split('?')[0].split('#')[0].lower()
    last_part = clean.split('/')[-1]
    ext = '.' + last_part.rsplit('.', 1)[1] if '.' in last_part else ''

    if ext == '.pdf':
        return 'pdf'
    elif ext in _AUDIO_EXTENSIONS:
        return 'audio'
    elif ext in _VIDEO_EXTENSIONS:
        return 'video'
    else:
        return 'webpage'


_FFMPEG_EXE = None


def _get_ffmpeg() -> str:
    """
    Path to an ffmpeg binary, or '' when none is available. Prefers a system
    install (present in the container image) and falls back to the static build
    shipped by imageio-ffmpeg (which covers the zip/Oryx deployment). Probed
    once per process.
    """
    global _FFMPEG_EXE
    if _FFMPEG_EXE is None:
        import shutil
        exe = shutil.which('ffmpeg') or ''
        if not exe:
            try:
                import imageio_ffmpeg
                exe = imageio_ffmpeg.get_ffmpeg_exe() or ''
            except Exception as e:
                print(f"CONCALL_AGENT: imageio-ffmpeg unavailable: {e}", file=sys.stderr)
        _FFMPEG_EXE = exe
        print(
            f"CONCALL_AGENT: ffmpeg {'found at ' + exe if exe else 'NOT available'}",
            file=sys.stderr
        )
    return _FFMPEG_EXE


def _demux_to_audio(src_path: str):
    """
    Strip the video track and re-encode to mono 16 kHz.

    Company IR sites publish earnings calls as .webm/.mp4 containers carrying a
    video track. Gemini would bill those as an hour of video frames when the
    audio is the only thing we need — this typically cuts a 45 MB webm to a few MB.

    Returns (path, mime_type), or ('', '') when ffmpeg is missing or the
    conversion fails; the caller then uploads the original file unchanged.
    """
    ffmpeg = _get_ffmpeg()
    if not ffmpeg:
        return '', ''

    import subprocess

    base = os.path.splitext(src_path)[0]
    # libopus is present in both the Debian and imageio-ffmpeg builds, but fall
    # back to AAC rather than give up if this build lacks the encoder.
    attempts = [
        (base + '.audio.ogg', ['-c:a', 'libopus', '-b:a', '24k'], 'audio/ogg'),
        (base + '.audio.m4a', ['-c:a', 'aac', '-b:a', '32k'], 'audio/mp4'),
    ]

    for out_path, codec_args, mime in attempts:
        cmd = [ffmpeg, '-y', '-loglevel', 'error', '-i', src_path,
               '-vn', '-ac', '1', '-ar', '16000'] + codec_args + [out_path]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=600)
        except Exception as e:
            print(f"CONCALL_AGENT: ffmpeg demux errored ({e})", file=sys.stderr)
            continue

        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1024:
            src_mb = os.path.getsize(src_path) / (1024 * 1024)
            out_mb = os.path.getsize(out_path) / (1024 * 1024)
            print(
                f"CONCALL_AGENT: ✓ Demuxed to audio ({mime}): "
                f"{src_mb:.1f} MB → {out_mb:.1f} MB", file=sys.stderr
            )
            return out_path, mime

        err = (proc.stderr or b'').decode('utf-8', 'replace').strip()[:300]
        print(f"CONCALL_AGENT: ffmpeg demux to {mime} failed: {err}", file=sys.stderr)

    return '', ''


def _extract_video_id(url: str) -> str:
    """Extract YouTube video ID from various URL formats."""
    import re as _re
    patterns = [
        r'(?:youtube\.com/watch\?.*v=)([a-zA-Z0-9_-]{11})',
        r'(?:youtu\.be/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com/live/)([a-zA-Z0-9_-]{11})',
        r'(?:youtube\.com/embed/)([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = _re.search(pattern, url)
        if match:
            return match.group(1)
    return ''


# A freshly uploaded Gemini file is not immediately usable — it sits in
# PROCESSING until the backend finishes decoding it, and files.upload() does NOT
# block for that. Long recordings must be polled to ACTIVE before generate_content,
# which otherwise rejects them with "File is not in an ACTIVE state".
# NOTE: FileState subclasses (str, Enum), so str(state) renders as
# 'FileState.PROCESSING' — compare on .name, never on str().
_GEMINI_FILE_POLL_SECS = 3
_GEMINI_FILE_MAX_WAIT_SECS = 300


def _file_state_name(uploaded) -> str:
    """Uppercased state name of an uploaded Gemini file ('' when unknown)."""
    state = getattr(uploaded, 'state', None)
    if state is None:
        return ''
    return str(getattr(state, 'name', state)).upper()


def _wait_for_gemini_file_active(client, uploaded, label: str = ''):
    """
    Poll an uploaded Gemini file until it leaves PROCESSING.
    Returns the refreshed file when it is usable, or None when it FAILED or
    did not finish within the budget (callers must not transcribe in that case).
    """
    import time as _time

    waited = 0
    while _file_state_name(uploaded) == 'PROCESSING' and waited < _GEMINI_FILE_MAX_WAIT_SECS:
        _time.sleep(_GEMINI_FILE_POLL_SECS)
        waited += _GEMINI_FILE_POLL_SECS
        try:
            uploaded = client.files.get(name=uploaded.name)
        except Exception as e:
            print(f"CONCALL_AGENT: files.get failed while waiting for {label}: {e}", file=sys.stderr)
            return None
        if waited % 30 == 0:
            print(f"CONCALL_AGENT: Waiting for Gemini to process {label}... ({waited}s)", file=sys.stderr)

    state = _file_state_name(uploaded)
    # An unknown/absent state is treated as usable — only an explicit non-ACTIVE
    # state (FAILED, or still PROCESSING at timeout) aborts.
    if state in ('ACTIVE', ''):
        if waited:
            print(f"CONCALL_AGENT: Gemini file {label} ready after {waited}s", file=sys.stderr)
        return uploaded

    print(
        f"CONCALL_AGENT: ❌ Gemini file {label} is {state} after {waited}s "
        f"— skipping transcription", file=sys.stderr
    )
    # Don't leave the orphan behind on the Files API.
    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass
    return None


async def _extract_youtube_transcript(url: str, max_chars: int = 80000) -> str:
    """
    Extract transcript from a YouTube video.
    Strategy:
      1. Try youtube-transcript-api (fast, uses existing captions)
      2. Fallback: yt-dlp to download audio → Gemini transcription

    Args:
        url: YouTube video URL
        max_chars: Maximum characters to return

    Returns:
        Transcript text or empty string on failure
    """
    import os as _os
    video_id = _extract_video_id(url)
    if not video_id:
        print(f"CONCALL_AGENT: Could not extract YouTube video ID from {url}", file=sys.stderr)
        return ''

    # ── Strategy 1: Try captions via youtube-transcript-api ──
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        print(f"CONCALL_AGENT: Trying YouTube captions for {video_id}...", file=sys.stderr)

        def _fetch_captions():
            proxy_url = _os.environ.get("RESIDENTIAL_PROXY_URL")
            proxy_config = None
            if proxy_url:
                try:
                    from youtube_transcript_api.proxies import GenericProxyConfig
                    proxy_config = GenericProxyConfig(
                        http_url=proxy_url,
                        https_url=proxy_url,
                    )
                    print(f"CONCALL_AGENT: Using residential proxy for YouTube captions", file=sys.stderr)
                except ImportError:
                    print(f"CONCALL_AGENT: GenericProxyConfig not available, fetching captions without proxy", file=sys.stderr)

            ytt_api = YouTubeTranscriptApi(proxy_config=proxy_config) if proxy_config else YouTubeTranscriptApi()
            transcript_list = ytt_api.fetch(video_id)
            # Join all caption snippets into a single transcript
            lines = []
            for snippet in transcript_list:
                text = snippet.text.strip()
                if text:
                    lines.append(text)
            return '\n'.join(lines)

        captions = await asyncio.to_thread(_fetch_captions)

        if captions and len(captions.strip()) > 200:
            print(f"CONCALL_AGENT: Got {len(captions)} chars from YouTube captions", file=sys.stderr)
            return captions[:max_chars]
        else:
            print(f"CONCALL_AGENT: Captions too short ({len(captions) if captions else 0} chars), trying audio fallback", file=sys.stderr)

    except Exception as e:
        print(f"CONCALL_AGENT: YouTube captions failed ({e}), trying audio fallback", file=sys.stderr)

    # ── Strategy 2: yt-dlp audio download → Gemini transcription ──
    GOOGLE_API_KEY = _os.getenv("GOOGLE_API_KEY")
    if not GOOGLE_API_KEY:
        print("CONCALL_AGENT: No API key for Gemini transcription fallback", file=sys.stderr)
        return ''

    try:
        import tempfile

        def _download_and_transcribe():
            with tempfile.TemporaryDirectory() as tmp_dir:
                output_path = f"{tmp_dir}/audio.%(ext)s"

                import yt_dlp
                import os as _os
                proxy_url = _os.environ.get("RESIDENTIAL_PROXY_URL")
                
                ydl_opts = {
                    'format': 'bestaudio[ext=m4a]/bestaudio/best',
                    'outtmpl': output_path,
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': False,
                }
                if proxy_url:
                    ydl_opts['proxy'] = proxy_url

                cookie_path = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), 'youtube_cookies.txt')
                if _os.path.exists(cookie_path):
                    ydl_opts['cookiefile'] = cookie_path

                print(f"CONCALL_AGENT: Downloading YouTube audio via yt-dlp...", file=sys.stderr)
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    downloaded_file = ydl.prepare_filename(info)

                # Find the actual downloaded file (extension might differ)
                import glob
                audio_files = glob.glob(f"{tmp_dir}/audio.*")
                if not audio_files:
                    print(f"CONCALL_AGENT: No audio file found after yt-dlp download", file=sys.stderr)
                    return ''

                audio_path = audio_files[0]
                file_size_mb = _os.path.getsize(audio_path) / (1024 * 1024)
                print(f"CONCALL_AGENT: Downloaded {file_size_mb:.1f} MB audio", file=sys.stderr)

                # Read file and upload to Gemini
                with open(audio_path, 'rb') as f:
                    audio_bytes = f.read()

                ext = _os.path.splitext(audio_path)[1].lower()
                mime_map = {'.m4a': 'audio/mp4', '.webm': 'audio/webm', '.mp3': 'audio/mpeg',
                            '.ogg': 'audio/ogg', '.opus': 'audio/opus', '.wav': 'audio/wav'}
                mime_type = mime_map.get(ext, 'audio/mp4')

                _genai_client = genai.Client(api_key=GOOGLE_API_KEY)

                print(f"CONCALL_AGENT: Uploading audio to Gemini Files API ({mime_type})...", file=sys.stderr)
                uploaded = _genai_client.files.upload(
                    file=BytesIO(audio_bytes),
                    config=types.UploadFileConfig(
                        display_name=f'youtube_{video_id}{ext}',
                        mime_type=mime_type
                    )
                )

                # Wait for Gemini to finish decoding before transcribing
                uploaded = _wait_for_gemini_file_active(
                    _genai_client, uploaded, label=f'youtube_{video_id}{ext}')
                if uploaded is None:
                    return ''

                prompt = """You are a financial transcription specialist. This audio is from an earnings conference call (concall) for a publicly traded company.

Your task:
1. Transcribe ALL spoken words accurately and completely
2. Preserve speaker attributions where possible
3. Focus especially on the Q&A session
4. Capture all financial figures, percentages, and guidance numbers precisely
5. Output the transcript as clean, readable text

DO NOT summarize or analyze — just transcribe accurately.
Return ONLY the transcript text."""

                print(f"CONCALL_AGENT: Transcribing YouTube audio with Gemini...", file=sys.stderr)
                response = _genai_client.models.generate_content(
                    model='gemini-3-flash-preview',
                    contents=[
                        types.Part.from_text(text=prompt),
                        uploaded
                    ]
                )

                try:
                    _genai_client.files.delete(name=uploaded.name)
                except Exception:
                    pass

                return response.text if response.text else ''

        transcript = await asyncio.to_thread(_download_and_transcribe)

        if transcript:
            print(f"CONCALL_AGENT: Transcribed {len(transcript)} chars from YouTube audio", file=sys.stderr)
        return transcript[:max_chars] if transcript else ''

    except Exception as e:
        print(f"CONCALL_AGENT: YouTube audio fallback failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return ''


async def _transcribe_audio_video_from_url(url: str, media_type: str = 'audio',
                                           max_chars: int = 80000) -> str:
    """
    Download an audio/video file from a URL, upload to Gemini Files API,
    and transcribe it. Cleans up the uploaded file afterwards.

    Args:
        url: URL to the audio/video file
        media_type: 'audio' or 'video'
        max_chars: Maximum characters to return

    Returns:
        Transcribed text or empty string on failure
    """
    import os as _os
    GOOGLE_API_KEY = _os.getenv("GOOGLE_API_KEY")
    if not GOOGLE_API_KEY:
        print("CONCALL_AGENT: GOOGLE_API_KEY not set, cannot transcribe audio/video", file=sys.stderr)
        return ''

    import shutil as _shutil
    import tempfile as _tempfile

    tmp_dir = _tempfile.mkdtemp(prefix='concall_media_')

    try:
        print(f"CONCALL_AGENT: Downloading {media_type} from {url}", file=sys.stderr)

        clean_url = url.split('?')[0].split('#')[0]
        suffix = os.path.splitext(clean_url)[1][:8] or '.bin'
        src_path = os.path.join(tmp_dir, 'concall' + suffix)

        # Download the file — try httpx first, then curl_cffi fallback.
        # Streamed to disk: a full-length call runs to tens of MB and buffering
        # the whole body in memory is needless pressure on the worker.
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "audio/mpeg,audio/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": url.rsplit('/', 1)[0] + "/",
        }

        def _downloaded_bytes():
            return os.path.getsize(src_path) if os.path.exists(src_path) else 0

        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                async with client.stream('GET', url, headers=headers, timeout=300.0) as response:
                    response.raise_for_status()
                    with open(src_path, 'wb') as fh:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            fh.write(chunk)
        except Exception as httpx_err:
            print(f"CONCALL_AGENT: httpx audio download failed ({httpx_err}), trying curl_cffi...", file=sys.stderr)
            try:
                def _cffi_audio_download(audio_url, dest, use_proxy=False):
                    from curl_cffi import requests as cffi_requests
                    import os as _os2
                    proxies = None
                    if use_proxy:
                        proxy_url = _os2.environ.get("RESIDENTIAL_PROXY_URL")
                        if proxy_url:
                            proxies = {"http": proxy_url, "https": proxy_url}
                        else:
                            return False
                    session = cffi_requests.Session(impersonate="chrome110", proxies=proxies)
                    r = session.get(
                        audio_url,
                        headers={"Referer": audio_url.rsplit('/', 1)[0] + "/",
                                 "Accept": "audio/mpeg,audio/*,*/*;q=0.8"},
                        allow_redirects=True, timeout=300
                    )
                    if r.status_code == 200 and r.content:
                        with open(dest, 'wb') as fh:
                            fh.write(r.content)
                        return True
                    return False

                # Attempt 1: Direct (no proxy)
                ok = await asyncio.to_thread(_cffi_audio_download, url, src_path, False)
                if not ok:
                    # Attempt 2: With proxy
                    ok = await asyncio.to_thread(_cffi_audio_download, url, src_path, True)
                if ok:
                    print(f"CONCALL_AGENT: ✓ curl_cffi audio download succeeded ({_downloaded_bytes()} bytes)", file=sys.stderr)
            except Exception as cffi_err:
                print(f"CONCALL_AGENT: curl_cffi audio fallback also failed: {cffi_err}", file=sys.stderr)

        if _downloaded_bytes() <= 0:
            print(f"CONCALL_AGENT: ❌ All audio download methods failed for {url}", file=sys.stderr)
            return ''

        file_size_mb = _downloaded_bytes() / (1024 * 1024)
        print(f"CONCALL_AGENT: Downloaded {file_size_mb:.1f} MB of {media_type}", file=sys.stderr)

        # Determine mime type
        mime_type = mimetypes.guess_type(clean_url)[0]
        if not mime_type:
            mime_type = f"{media_type}/mp4" if media_type == 'video' else f"{media_type}/mpeg"

        # A video container means Gemini would bill video frames for what is really
        # just a conference call, so strip the video track when ffmpeg is available.
        upload_path = src_path
        if _get_url_type(url) == 'video' or mime_type.startswith('video/'):
            audio_path, audio_mime = _demux_to_audio(src_path)
            if audio_path:
                upload_path, mime_type = audio_path, audio_mime
            else:
                # Without ffmpeg, keep the true video mime: Gemini handles it
                # (just far more expensively), whereas claiming audio/webm for a
                # video container is not an accepted type and hard-fails.
                print(
                    f"CONCALL_AGENT: ⚠️ No ffmpeg — uploading as {mime_type}; "
                    f"install ffmpeg or imageio-ffmpeg to cut cost substantially",
                    file=sys.stderr
                )

        # Upload to Gemini Files API (blocking — run in thread)
        def _blocking_transcribe(path, mime, filename):
            _genai_client = genai.Client(api_key=GOOGLE_API_KEY)

            print(f"CONCALL_AGENT: Uploading {media_type} to Gemini Files API ({mime})...", file=sys.stderr)
            uploaded = _genai_client.files.upload(
                file=path,
                config=types.UploadFileConfig(
                    display_name=filename,
                    mime_type=mime
                )
            )
            print(f"CONCALL_AGENT: Uploaded as '{uploaded.name}'", file=sys.stderr)

            # Wait for Gemini to finish decoding — long recordings take minutes
            uploaded = _wait_for_gemini_file_active(_genai_client, uploaded, label=filename)
            if uploaded is None:
                return ''

            prompt = f"""You are a financial transcription specialist. This {media_type} contains an earnings conference call (concall) for a publicly traded company.

Your task:
1. Transcribe ALL spoken words accurately and completely
2. Preserve speaker attributions where possible (e.g., "Management:", "Analyst:", "Moderator:")
3. Focus especially on the Q&A session
4. Capture all financial figures, percentages, and guidance numbers precisely
5. Output the transcript as clean, readable text

DO NOT summarize or analyze — just transcribe the spoken content as accurately as possible.
Return ONLY the transcript text."""

            print(f"CONCALL_AGENT: Sending {media_type} to Gemini for transcription...", file=sys.stderr)
            response = _genai_client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=[
                    types.Part.from_text(text=prompt),
                    uploaded
                ]
            )

            # Clean up uploaded file
            try:
                _genai_client.files.delete(name=uploaded.name)
                print(f"CONCALL_AGENT: Cleaned up uploaded file {uploaded.name}", file=sys.stderr)
            except Exception as cleanup_err:
                print(f"CONCALL_AGENT: Warning - failed to clean up file {uploaded.name}: {cleanup_err}", file=sys.stderr)

            return response.text if response.text else ''

        filename = url.split('/')[-1].split('?')[0] or f'concall.{media_type}'
        transcript = await asyncio.to_thread(
            _blocking_transcribe, upload_path, mime_type, filename
        )

        if transcript:
            print(f"CONCALL_AGENT: Transcribed {len(transcript)} chars from {media_type}", file=sys.stderr)

        return transcript[:max_chars] if transcript else ''

    except Exception as e:
        print(f"CONCALL_AGENT: Failed to transcribe {media_type} from {url}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return ''
    finally:
        _shutil.rmtree(tmp_dir, ignore_errors=True)


def register_concall_routes(app, call_gemini_api_fn, fetch_documents_fn, get_pdf_text_fn,
                            call_perplexity_search_fn=None):
    """
    Register all Concall Agent API routes with the Flask app.

    Args:
        app: Flask app instance
        call_gemini_api_fn: Reference to call_gemini_api from handler.py
        fetch_documents_fn: Reference to fetch_latest_documents_async from screener_fetcher
        get_pdf_text_fn: Reference to get_text_from_pdf_url_async from screener_fetcher
        call_perplexity_search_fn: Optional reference to call_perplexity_search_api.
            Only used for the last-resort, domain-restricted media search; when
            omitted that step is skipped rather than run unrestricted.
    """
    global _call_perplexity_search_fn
    _call_perplexity_search_fn = call_perplexity_search_fn

    @app.route('/agent/concall/analyze', methods=['POST'])
    def agent_concall_analyze():
        """
        Start a Concall Agent analysis job.
        Takes { ticker: str } and returns { job_id: str }.
        Uses background thread + polling pattern.
        """
        try:
            data = request.get_json(force=True)
            ticker = data.get('ticker', '').strip().upper()

            if not ticker:
                return jsonify({'error': 'Ticker is required'}), 400

            print(f"CONCALL_AGENT: Starting analysis for {ticker}", file=sys.stderr)

            # Check if we already have a recent result for this ticker
            existing = get_latest_result('concall', ticker)
            if existing and data.get('force_refresh') is not True:
                age_minutes = (time.time() - existing['stored_at']) / 60
                if age_minutes < 60:  # Less than 1 hour old
                    print(f"CONCALL_AGENT: Returning cached result for {ticker} ({age_minutes:.0f}m old)", file=sys.stderr)
                    return jsonify({
                        'status': 'complete',
                        'result': existing['result'],
                        'cached': True,
                        'age_minutes': round(age_minutes)
                    })

            # Create background job
            job_id = create_agent_job('concall', ticker)

            # Start background analysis thread
            thread = threading.Thread(
                target=_run_concall_analysis,
                args=(job_id, ticker, call_gemini_api_fn, fetch_documents_fn, get_pdf_text_fn)
            )
            thread.daemon = True
            thread.start()

            return jsonify({
                'job_id': job_id,
                'status': 'processing',
                'message': f'Concall analysis started for {ticker}.'
            })

        except Exception as e:
            print(f"CONCALL_AGENT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({'error': str(e)}), 500

    @app.route('/agent/concall/<job_id>/status', methods=['GET'])
    def agent_concall_status(job_id):
        """Poll endpoint for Concall Agent job status."""
        job = get_agent_job(job_id)

        if not job:
            return jsonify({'status': 'error', 'error': 'Job not found or expired'})

        if job['status'] == 'processing':
            return jsonify({
                'status': 'processing',
                'progress': job['progress'],
                'sources_tried': job.get('sources_tried', []),
                'elapsed_seconds': int(time.time() - job['started_at'])
            })

        elif job['status'] == 'complete':
            return jsonify({
                'status': 'complete',
                'result': job['result']
            })

        elif job['status'] == 'error':
            return jsonify({
                'status': 'error',
                'error': job['error'],
                'auto_fetch_failed': job.get('auto_fetch_failed', False),
                'quarter_mismatch': job.get('quarter_mismatch', False),
                'sources_tried': job.get('sources_tried', []),
                'target_quarter': job.get('target_quarter', ''),
                'quarter_source': job.get('quarter_source', ''),
            })

        return jsonify({'error': 'Unknown job status'}), 500

    @app.route('/agent/concall/chat', methods=['POST'])
    def agent_concall_chat():
        """
        Chat with the Concall Agent about a completed analysis.
        Takes { ticker, question, analysis_context, transcript_context }.
        """
        try:
            data = request.get_json(force=True)
            question = data.get('question', '').strip()
            ticker = data.get('ticker', '').strip().upper()
            analysis_context = data.get('analysis_context', '').strip()
            transcript_context = data.get('transcript_context', '').strip()
            company_name = data.get('company_name', ticker)

            if not question:
                return jsonify({'error': 'Question is required'}), 400

            if not analysis_context:
                return jsonify({'error': 'Analysis context is required. Run the analysis first.'}), 400

            print(f"CONCALL_AGENT_CHAT: Question for {ticker}: {question[:80]}...", file=sys.stderr)

            # Build the chat prompt
            chat_prompt = CONCALL_CHAT_PROMPT.format(
                company_name=company_name,
                ticker=ticker,
                analysis=analysis_context[:40000],  # Limit context size
                transcript=transcript_context[:50000] if transcript_context else "Original transcript not available."
            )

            messages = [
                {"role": "system", "content": chat_prompt},
                {"role": "user", "content": question}
            ]

            # Call Gemini Flash for fast response
            answer = call_gemini_api_fn(
                messages,
                model="gemini-3-flash-preview",
                temperature=1,
                thinking_level='HIGH'
            )

            return jsonify({
                'answer': answer,
                'status': 'success'
            })

        except Exception as e:
            print(f"CONCALL_AGENT_CHAT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({'error': str(e)}), 500

    @app.route('/agent/concall/latest', methods=['GET'])
    def agent_concall_latest():
        """
        Get the latest cached result for a ticker (for persistence across tab switches).
        """
        ticker = request.args.get('ticker', '').strip().upper()
        if not ticker:
            return jsonify({'error': 'Ticker is required'}), 400

        result = get_latest_result('concall', ticker)
        if result:
            return jsonify({
                'status': 'complete',
                'result': result['result'],
                'age_minutes': round((time.time() - result['stored_at']) / 60)
            })

        return jsonify({'status': 'none', 'message': 'No cached result found'})

    # =================================================================
    # ANALYZE FROM USER-PROVIDED URL
    # =================================================================
    @app.route('/agent/concall/analyze-url', methods=['POST'])
    def agent_concall_analyze_url():
        """
        Analyze a concall transcript from a user-provided URL.
        Accepts { ticker, url, results_quarter }.
        Supports PDF and webpage URLs.
        """
        try:
            data = request.get_json(force=True)
            ticker = data.get('ticker', '').strip().upper()
            url = data.get('url', '').strip()
            results_quarter = data.get('results_quarter', '').strip()

            if not ticker:
                return jsonify({'error': 'Ticker is required'}), 400
            if not url:
                return jsonify({'error': 'URL is required'}), 400

            print(f"CONCALL_AGENT: Analyzing user-provided URL for {ticker}: {url}", file=sys.stderr)

            # Create a background job
            job_id = create_agent_job('concall', ticker, metadata={'url': url})

            thread = threading.Thread(
                target=_run_concall_url_analysis,
                args=(job_id, ticker, url, results_quarter,
                      call_gemini_api_fn, get_pdf_text_fn)
            )
            thread.daemon = True
            thread.start()

            return jsonify({
                'job_id': job_id,
                'status': 'processing',
                'message': f'Analyzing concall from provided URL for {ticker}.'
            })

        except Exception as e:
            print(f"CONCALL_AGENT_URL ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({'error': str(e)}), 500


def _run_concall_analysis(job_id, ticker, call_gemini_api_fn, fetch_documents_fn, get_pdf_text_fn):
    """
    Background function that runs the full Concall Agent analysis pipeline:
    1. Fetch the latest concall transcript from Screener.in
    2. Determine quarter mismatch (lightweight)
    3. Send to Gemini for structured analysis
    4. Store the result (including quarter info)
    """
    start_time = time.time()

    try:
        update_agent_job(job_id, {'progress': f'Fetching concall transcript for {ticker}...'})
        print(f"CONCALL_AGENT: Step 1 — Fetching documents for {ticker}", file=sys.stderr)

        # Step 1: Fetch documents + latest results quarter in parallel
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Ask for the concall document only. The results-presentation summary
        # costs a Gemini request and this agent never reads it — it only ever
        # looks at the doc whose type is 'Concall'.
        try:
            docs_coro = fetch_documents_fn(ticker, include_presentation=False)
        except TypeError:
            # Older/other injected fetcher without the flag — still works.
            docs_coro = fetch_documents_fn(ticker)

        try:
            # Run both fetches concurrently for speed
            documents, (results_quarter, screener_name) = loop.run_until_complete(
                asyncio.gather(
                    docs_coro,
                    _fetch_screener_quarter_and_name(ticker)
                )
            )
        finally:
            loop.close()

        # Screener's <h1> is cleaner than the local stock master (which mangles
        # possessives), so prefer it and fall back to the CSV.
        company_name = screener_name or _get_company_name_from_ticker(ticker)

        # Find the concall transcript
        concall_doc = None
        concall_text = None

        if documents:
            for doc in documents:
                if doc.get('type') == 'Concall':
                    concall_doc = doc
                    concall_text = doc.get('content_summary', '')
                    break

        concall_link = concall_doc.get('link', '') if concall_doc else ''
        concall_label = concall_doc.get('text', 'Latest Concall') if concall_doc else 'Latest Concall'
        concall_raw_date = concall_doc.get('date', '') if concall_doc else ''

        # Determine concall quarter from its publication date
        concall_quarter = _date_to_quarter(concall_raw_date)
        quarter_mismatch = False
        if results_quarter and concall_quarter:
            quarter_mismatch = (concall_quarter != results_quarter)

        # Where the target quarter came from, so the UI can qualify its claims.
        quarter_source = 'screener' if results_quarter else 'derived'
        expected_primary, expected_previous = _expected_results_quarters()

        # When Screener is unreachable, results_quarter is '' and the mismatch
        # check above is vacuously False — which used to mean a stale prior-quarter
        # transcript was analysed silently. Fall back to the date-derived quarter
        # instead, accepting either the primary or previous label because the
        # reporting-lag heuristic runs early at the start of a season.
        stale_vs_derived = (
            not results_quarter
            and concall_quarter
            and concall_quarter not in (expected_primary, expected_previous)
        )

        auto_fetch_failed = False
        auto_fetch_source = None

        # Per-source attempt log. Every discovery step appends exactly one entry
        # so a failure can be diagnosed from the result instead of from stderr.
        # status: found | not_found | wrong_quarter | transcription_failed | blocked | error
        sources_tried = []

        def _log_source(source, status, url='', quarter='', detail=''):
            sources_tried.append({
                'source': source, 'status': status,
                'url': url, 'quarter': quarter, 'detail': detail,
            })
            update_agent_job(job_id, {'sources_tried': list(sources_tried)})

        transcript_missing = (not concall_text or len(concall_text.strip()) < 200)

        if transcript_missing or quarter_mismatch or not concall_quarter or stale_vs_derived:
            if quarter_mismatch:
                print(f"CONCALL_AGENT: ⚠️ Quarter mismatch! Concall={concall_quarter}, Results={results_quarter}", file=sys.stderr)
            elif stale_vs_derived:
                print(
                    f"CONCALL_AGENT: ⚠️ Screener quarter unavailable; concall quarter "
                    f"{concall_quarter} is older than expected "
                    f"({expected_primary}/{expected_previous}) — searching for a newer call",
                    file=sys.stderr
                )
            elif not concall_quarter:
                print(f"CONCALL_AGENT: ⚠️ Concall document has no usable date for {ticker}", file=sys.stderr)
            else:
                print(f"CONCALL_AGENT: ⚠️ No concall transcript found for {ticker}", file=sys.stderr)

            update_agent_job(job_id, {'progress': 'Transcript missing or outdated. Auto-searching for latest concall audio...'})

            _log_source(
                'screener_transcript',
                'not_found' if transcript_missing else 'wrong_quarter',
                url=concall_link, quarter=concall_quarter,
                detail=('no transcript PDF on Screener' if transcript_missing
                        else f'transcript is for {concall_quarter or "an unknown quarter"}')
            )

            new_audio_url = ""
            new_source = ""

            # Helper to attempt transcription within a loop
            async def _try_transcribe(audio_url):
                is_yt = 'youtube' in audio_url.lower() or 'youtu.be' in audio_url.lower()
                text = ""
                if is_yt:
                    print("CONCALL_AGENT: Trying _extract_youtube_transcript", file=sys.stderr)
                    text = await _extract_youtube_transcript(audio_url, max_chars=80000)

                if not text or text.startswith("Error"):
                    print(f"CONCALL_AGENT: Falling back to _transcribe_audio_video_from_url via Gemini for {audio_url}", file=sys.stderr)
                    update_agent_job(job_id, {'progress': 'Running deep transcription via Gemini AI...'})
                    text = await _transcribe_audio_video_from_url(audio_url, 'audio')
                return text

            # Screener REC link. fetch_latest_documents_async already puts a
            # 'rec_link' on the concall doc, so reuse it and only re-fetch the
            # (Cloudflare-gated) page when it is absent.
            rec_info = {}
            doc_rec_link = concall_doc.get('rec_link', '') if concall_doc else ''
            if doc_rec_link:
                rec_info = {"url": doc_rec_link, "date": concall_raw_date}
                print(f"CONCALL_AGENT: Reusing REC link from documents fetch: {doc_rec_link}", file=sys.stderr)
            else:
                from fetchers.screener_fetcher import fetch_concall_rec_url_async
                loop2 = asyncio.new_event_loop()
                asyncio.set_event_loop(loop2)
                try:
                    rec_info = loop2.run_until_complete(fetch_concall_rec_url_async(ticker))
                finally:
                    loop2.close()

            # One event loop for the whole discovery cascade.
            loop3 = asyncio.new_event_loop()
            asyncio.set_event_loop(loop3)

            def _commit(source, url, label, quarter, text):
                """Accept a transcript and record it as the winning source."""
                nonlocal concall_text, concall_link, concall_label
                nonlocal concall_quarter, quarter_mismatch, auto_fetch_source, new_audio_url
                concall_text = text
                concall_link = url
                concall_label = label
                concall_quarter = quarter or results_quarter or "Latest"
                quarter_mismatch = False
                auto_fetch_source = source
                new_audio_url = url

            def _try_media(source, url, label, quarter=''):
                """
                Download + transcribe `url`; commit on success. Returns True when
                it worked. Every outcome is written to the attempt log, so a
                failure here is diagnosable rather than silent.
                """
                if not url:
                    return False
                print(f"CONCALL_AGENT: [{source}] trying {url}", file=sys.stderr)
                try:
                    text = loop3.run_until_complete(_try_transcribe(url))
                except Exception as e:
                    print(f"CONCALL_AGENT: [{source}] transcription raised: {e}", file=sys.stderr)
                    _log_source(source, 'transcription_failed', url=url,
                                quarter=quarter, detail=str(e)[:200])
                    return False
                if text and not text.startswith("Error") and len(text.strip()) >= 200:
                    _commit(source, url, label, quarter, text)
                    _log_source(source, 'found', url=url, quarter=concall_quarter,
                                detail=f'transcribed {len(text)} chars')
                    return True
                _log_source(source, 'transcription_failed', url=url, quarter=quarter,
                            detail='transcription returned nothing usable')
                return False

            def _quarter_ok(q):
                """Does quarter label `q` match what we are looking for?"""
                if not q:
                    return False
                if results_quarter:
                    return q == results_quarter
                return q in (expected_primary, expected_previous)

            try:
                # ---------------------------------------------------------------
                # Source order is deliberate: prefer whatever proves BOTH the
                # company and the quarter. A written transcript filed with an
                # exchange proves both and costs nothing to use, so it leads.
                # A company-hosted file whose quarter cannot be verified is the
                # weakest evidence and therefore ranks BELOW YouTube, which does
                # check the company name, quarter tag, upload date and duration.
                # ---------------------------------------------------------------

                # 1. Written transcript filed with NSE / BSE.
                update_agent_job(job_id, {'progress': 'Checking exchange filings for a written transcript...'})
                for src_name, fetch_fn in (
                    ('nse_transcript', _fetch_nse_transcript_candidate),
                    ('bse_transcript', _fetch_bse_transcript_candidate),
                ):
                    if new_audio_url:
                        break
                    try:
                        cand = loop3.run_until_complete(fetch_fn(ticker, results_quarter))
                    except Exception as e:
                        print(f"CONCALL_AGENT: {src_name} lookup failed: {e}", file=sys.stderr)
                        _log_source(src_name, 'blocked', detail=str(e)[:200])
                        continue
                    if not cand.get('url'):
                        _log_source(src_name, 'not_found', detail='no transcript filed yet')
                        continue
                    if not _quarter_ok(cand.get('quarter')):
                        _log_source(src_name, 'wrong_quarter', url=cand['url'],
                                    quarter=cand.get('quarter'),
                                    detail=f"transcript is for {cand.get('quarter') or 'an unknown quarter'}")
                        continue
                    try:
                        text = loop3.run_until_complete(
                            get_pdf_text_fn(cand['url'], max_chars_to_return=80000))
                    except Exception as e:
                        _log_source(src_name, 'blocked', url=cand['url'], detail=str(e)[:200])
                        continue
                    if text and len(text.strip()) >= 200:
                        _commit(src_name, cand['url'], 'Exchange-Filed Concall Transcript',
                                cand.get('quarter'), text)
                        _log_source(src_name, 'found', url=cand['url'],
                                    quarter=concall_quarter, detail=f'{len(text)} chars from PDF')
                    else:
                        _log_source(src_name, 'parse_error', url=cand['url'],
                                    detail='transcript PDF yielded no text')

                # 2. Audio the company itself filed with an exchange. Company-correct
                #    by construction and carries a filing date. BSE often hosts the
                #    mp3 directly, which is far smaller than a company-site video.
                ir_seed_url = ""
                bse_cands = []
                if not new_audio_url:
                    update_agent_job(job_id, {'progress': 'Searching exchange filings for concall audio...'})
                    try:
                        from fetchers.bse_fetcher import fetch_bse_concall_docs_async
                        bse_cands = loop3.run_until_complete(
                            fetch_bse_concall_docs_async(ticker, results_quarter))
                    except Exception as e:
                        print(f"CONCALL_AGENT: BSE filing fetch failed: {e}", file=sys.stderr)
                        _log_source('bse_audio', 'blocked', detail=str(e)[:200])
                    bse_audio = next(
                        (c for c in bse_cands
                         if c['kind'] in ('audio', 'video') and _quarter_ok(c.get('quarter'))),
                        None)
                    if bse_audio:
                        _try_media('bse_audio', bse_audio['url'],
                                   'Exchange-Filed Concall Audio (BSE)', bse_audio.get('quarter'))
                    elif any(c['kind'] in ('audio', 'video') for c in bse_cands):
                        _log_source('bse_audio', 'wrong_quarter',
                                    detail='only older-quarter recordings filed')
                    elif bse_cands is not None:
                        _log_source('bse_audio', 'not_found', detail='no audio filing on BSE')
                    if not new_audio_url:
                        ir_seed_url = next(
                            (c['url'] for c in bse_cands if c['kind'] == 'ir_page'), "")

                if not new_audio_url:
                    nse_blocked = False
                    rec = {}
                    try:
                        from fetchers.nse_fetcher import fetch_nse_concall_recording_async
                        rec = loop3.run_until_complete(
                            fetch_nse_concall_recording_async(ticker, results_quarter))
                    except Exception as e:
                        print(f"CONCALL_AGENT: NSE filing fetch failed: {e}", file=sys.stderr)
                        nse_blocked = True
                        _log_source('nse_filing', 'blocked', detail=str(e)[:200])
                    ir_seed_url = ir_seed_url or (rec.get('ir_url') or "")
                    nse_audio_url = rec.get('audio_url') or ""
                    if nse_audio_url and _quarter_ok(rec.get('quarter') or results_quarter):
                        _try_media('nse_filing', nse_audio_url,
                                   'Exchange-Filed Concall Audio (NSE)',
                                   rec.get('quarter') or results_quarter)
                    elif nse_audio_url:
                        _log_source('nse_filing', 'wrong_quarter', url=nse_audio_url,
                                    quarter=rec.get('quarter'),
                                    detail=f"recording is for {rec.get('quarter') or 'an unknown quarter'}")
                    elif not nse_blocked:
                        _log_source('nse_filing', 'not_found',
                                    detail=('filing found but no audio link in it'
                                            if ir_seed_url else 'no recording filing found'))

                # 3. Company IR website. Resolved once, used twice: a file whose
                #    quarter is CONFIRMED (by upload date, or by an explicit tag in
                #    the filename) outranks YouTube; an unconfirmed one does not.
                ir_result = {'url': '', 'quarter_confirmed': False, 'via': ''}
                if not new_audio_url:
                    update_agent_job(job_id, {'progress': 'Searching company IR page for concall audio...'})
                    try:
                        ir_result = loop3.run_until_complete(
                            _fetch_ir_website_audio(ticker, company_name, results_quarter,
                                                    seed_url=ir_seed_url or None))
                    except Exception as e:
                        print(f"CONCALL_AGENT: IR website audio fetch failed: {e}", file=sys.stderr)
                        _log_source('ir_website', 'blocked', detail=str(e)[:200])
                        ir_result = {'url': '', 'quarter_confirmed': False, 'via': 'error'}

                if not new_audio_url and ir_result.get('url') and ir_result.get('quarter_confirmed'):
                    _try_media('ir_website', ir_result['url'],
                               'Company IR Website Audio', results_quarter)

                # 4. Screener REC link — quarter-gated against the results quarter.
                if not new_audio_url:
                    if rec_info and rec_info.get("url"):
                        rec_quarter = _date_to_quarter(rec_info.get("date", ""))
                        if _quarter_ok(rec_quarter):
                            _try_media('screener_rec', rec_info["url"],
                                       'Auto-Fetched Audio Transcription', rec_quarter)
                        else:
                            print(
                                f"CONCALL_AGENT: Skipping Screener REC ({rec_quarter or 'undated'}) "
                                f"- target is {results_quarter or expected_primary}", file=sys.stderr
                            )
                            _log_source('screener_rec', 'wrong_quarter', url=rec_info["url"],
                                        quarter=rec_quarter,
                                        detail=f"recording is for {rec_quarter or 'an unknown quarter'}")
                    else:
                        _log_source('screener_rec', 'not_found', detail='no REC link on Screener')

                # 5. YouTube - third-party, but the best-verified of the remaining
                #    options (company name, quarter tag, upload date, duration).
                if not new_audio_url:
                    update_agent_job(job_id, {'progress': 'Searching YouTube for latest earnings call...'})
                    yt_audio_url = _search_youtube_concall(
                        company_name, ticker, results_quarter if results_quarter else "")
                    if yt_audio_url:
                        _try_media('youtube_search', yt_audio_url,
                                   'Auto-Fetched YouTube Transcription', results_quarter)
                    else:
                        _log_source('youtube_search', 'not_found',
                                    detail='no video matched this company and quarter')

                # 6. Company IR file whose quarter could NOT be confirmed. Last
                #    deterministic resort - ranked below YouTube because here the
                #    quarter is an assumption rather than evidence.
                if not new_audio_url and ir_result.get('url') and not ir_result.get('quarter_confirmed'):
                    print("CONCALL_AGENT: Falling back to IR file with unconfirmed quarter",
                          file=sys.stderr)
                    _try_media('ir_website', ir_result['url'],
                               'Company IR Website Audio (quarter unconfirmed)', results_quarter)
                elif not new_audio_url and not ir_result.get('url'):
                    _log_source('ir_website', 'not_found',
                                detail='no matching recording on the company site')

                # 7. Web search, restricted to the company's own domain. Last resort
                #    only: unlike every source above, it is not company-correct by
                #    construction, so an open query could return a competitor's file.
                if not new_audio_url:
                    update_agent_job(job_id, {'progress': 'Last resort: searching the company domain...'})
                    ws_url = ""
                    try:
                        ws_url = loop3.run_until_complete(
                            _web_search_concall_media(ticker, company_name, results_quarter))
                    except Exception as e:
                        print(f"CONCALL_AGENT: web search failed: {e}", file=sys.stderr)
                        _log_source('web_search', 'blocked', detail=str(e)[:200])
                    if ws_url:
                        _try_media('web_search', ws_url,
                                   'Concall Audio (web search)', results_quarter)
                    else:
                        _log_source('web_search', 'not_found',
                                    detail='nothing found on the company domain')

            finally:
                loop3.close()
                if not new_audio_url:
                    auto_fetch_failed = True

        if not concall_text or len(concall_text.strip()) < 200:
            elapsed = int(time.time() - start_time)
            print(f"CONCALL_AGENT: No concall transcript found for {ticker} after {elapsed}s", file=sys.stderr)
            for entry in sources_tried:
                print(f"CONCALL_AGENT:   · {entry['source']}: {entry['status']}"
                      f"{' — ' + entry['detail'] if entry.get('detail') else ''}", file=sys.stderr)

            update_agent_job(job_id, {
                'status': 'error',
                'error': build_concall_fetch_error(
                    sources_tried, results_quarter or expected_primary),
                'auto_fetch_failed': auto_fetch_failed,
                'quarter_mismatch': quarter_mismatch,
                'sources_tried': sources_tried,
                'target_quarter': results_quarter or expected_primary,
                'quarter_source': quarter_source,
            })
            return

        elapsed_fetch = int(time.time() - start_time)
        print(f"CONCALL_AGENT: Step 1 complete — Got {len(concall_text)} chars of transcript in {elapsed_fetch}s", file=sys.stderr)

        # Step 2: Send to Gemini for analysis
        update_agent_job(job_id, {
            'progress': f'Analyzing transcript with AI ({len(concall_text)} chars)...'
        })
        print(f"CONCALL_AGENT: Step 2 — Sending to Gemini for analysis", file=sys.stderr)

        # Include quarter info in the analysis prompt so Gemini mentions it
        quarter_note = ''
        if concall_quarter:
            quarter_note = f"\n## Quarter: {concall_quarter}\n"

        analysis_prompt = f"""{CONCALL_ANALYSIS_PROMPT}

## Company: {ticker}
## Transcript Source: {concall_label}
{quarter_note}
## Full Transcript:
{concall_text}
"""

        messages = [{"role": "user", "content": analysis_prompt}]

        analysis_result = call_gemini_api_fn(
            messages,
            model="gemini-3-flash-preview",
            temperature=1,
            thinking_level='HIGH'
        )

        elapsed_total = int(time.time() - start_time)
        print(f"CONCALL_AGENT: Step 2 complete — Got {len(analysis_result)} chars of analysis in {elapsed_total}s", file=sys.stderr)

        # Step 3: Store result with quarter info
        result_data = {
            'analysis': analysis_result,
            'ticker': ticker,
            'concall_label': concall_label,
            'concall_link': concall_link,
            'concall_quarter': concall_quarter,
            'results_quarter': results_quarter,
            'quarter_mismatch': quarter_mismatch,
            'auto_fetch_source': auto_fetch_source,
            'quarter_source': quarter_source,
            'sources_tried': sources_tried,
            'transcript_text': concall_text,
            'analyzed_at': time.time(),
            'analysis_time_seconds': elapsed_total
        }

        store_latest_result('concall', ticker, result_data)

        update_agent_job(job_id, {
            'status': 'complete',
            'progress': 'Analysis complete!',
            'result': result_data,
            'completed_at': time.time(),
            'total_time': elapsed_total
        })

        print(f"CONCALL_AGENT: ✓ Analysis complete for {ticker} in {elapsed_total}s ({len(analysis_result)} chars)", file=sys.stderr)

    except Exception as e:
        elapsed = int(time.time() - start_time)
        print(f"CONCALL_AGENT ERROR: Job {job_id} failed after {elapsed}s: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        update_agent_job(job_id, {
            'status': 'error',
            'error': f'Analysis failed: {str(e)}',
            'failed_at': time.time(),
            'elapsed': elapsed
        })


def _run_concall_url_analysis(job_id, ticker, url, results_quarter,
                              call_gemini_api_fn, get_pdf_text_fn):
    """
    Background function that analyzes a concall transcript from a
    user-provided URL (PDF or webpage).
    """
    start_time = time.time()

    try:
        update_agent_job(job_id, {'progress': f'Fetching transcript from provided URL...'})
        print(f"CONCALL_AGENT_URL: Fetching content from {url}", file=sys.stderr)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # Determine URL type and extract text
            url_type = _get_url_type(url)
            print(f"CONCALL_AGENT_URL: Detected URL type: {url_type}", file=sys.stderr)

            if url_type == 'pdf':
                update_agent_job(job_id, {'progress': 'Extracting text from PDF...'})
                concall_text = loop.run_until_complete(get_pdf_text_fn(url))
            elif url_type == 'youtube':
                update_agent_job(job_id, {'progress': 'Extracting transcript from YouTube video...'})
                concall_text = loop.run_until_complete(_extract_youtube_transcript(url))
            elif url_type in ('audio', 'video'):
                update_agent_job(job_id, {'progress': f'Downloading and transcribing {url_type}... (this may take a few minutes)'})
                concall_text = loop.run_until_complete(
                    _transcribe_audio_video_from_url(url, media_type=url_type)
                )
            else:
                update_agent_job(job_id, {'progress': 'Extracting text from webpage...'})
                concall_text = loop.run_until_complete(_extract_text_from_webpage(url))
        finally:
            loop.close()

        if not concall_text or len(concall_text.strip()) < 200:
            elapsed = int(time.time() - start_time)
            print(f"CONCALL_AGENT_URL: Insufficient text extracted from {url} ({len(concall_text) if concall_text else 0} chars)", file=sys.stderr)
            update_agent_job(job_id, {
                'status': 'error',
                'error': f'Could not extract enough text from the provided URL. '
                         f'Got {len(concall_text) if concall_text else 0} characters '
                         f'(minimum 200 needed). Please check the URL and try again.'
            })
            return

        elapsed_fetch = int(time.time() - start_time)
        print(f"CONCALL_AGENT_URL: Got {len(concall_text)} chars in {elapsed_fetch}s", file=sys.stderr)

        # Step 2: Send to Gemini for analysis
        update_agent_job(job_id, {
            'progress': f'Analyzing transcript with AI ({len(concall_text)} chars)...'
        })

        quarter_note = ''
        if results_quarter:
            quarter_note = f"\n## Quarter: {results_quarter}\n"

        concall_label = f"User-provided Concall Transcript"
        if results_quarter:
            concall_label = f"Concall Transcript ({results_quarter})"

        analysis_prompt = f"""{CONCALL_ANALYSIS_PROMPT}

## Company: {ticker}
## Transcript Source: {concall_label}
{quarter_note}
## Full Transcript:
{concall_text}
"""

        messages = [{"role": "user", "content": analysis_prompt}]

        analysis_result = call_gemini_api_fn(
            messages,
            model="gemini-3-flash-preview",
            temperature=1,
            thinking_level='HIGH'
        )

        elapsed_total = int(time.time() - start_time)
        print(f"CONCALL_AGENT_URL: Analysis complete in {elapsed_total}s ({len(analysis_result)} chars)", file=sys.stderr)

        # Step 3: Store result
        result_data = {
            'analysis': analysis_result,
            'ticker': ticker,
            'concall_label': concall_label,
            'concall_link': url,
            'concall_quarter': results_quarter,
            'results_quarter': results_quarter,
            'quarter_mismatch': False,  # User explicitly provided the right URL
            'transcript_text': concall_text,
            'analyzed_at': time.time(),
            'analysis_time_seconds': elapsed_total,
            'source': 'user_provided_url'
        }

        store_latest_result('concall', ticker, result_data)

        update_agent_job(job_id, {
            'status': 'complete',
            'progress': 'Analysis complete!',
            'result': result_data,
            'completed_at': time.time(),
            'total_time': elapsed_total
        })

        print(f"CONCALL_AGENT_URL: ✓ Done for {ticker} in {elapsed_total}s", file=sys.stderr)

    except Exception as e:
        elapsed = int(time.time() - start_time)
        print(f"CONCALL_AGENT_URL ERROR: Job {job_id} failed after {elapsed}s: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        update_agent_job(job_id, {
            'status': 'error',
            'error': f'Analysis from URL failed: {str(e)}',
            'failed_at': time.time(),
            'elapsed': elapsed
        })
