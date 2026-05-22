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
    CONCALL_ANALYSIS_PROMPT, CONCALL_CHAT_PROMPT, CONCALL_FETCH_ERROR_MSG
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

def _get_current_expected_quarter() -> str:
    """Derive the most likely latest quarter from today's date.
    e.g. In April 2026, the latest quarter end is Mar 2026 → 'Q4 FY26'"""
    from datetime import datetime
    now = datetime.now()
    m, y = now.month, now.year
    # Which quarter just ended?
    if m in (1, 2, 3, 4):    return f"Q4 FY{str(y)[-2:]}"       # Mar quarter
    elif m in (5, 6, 7):      return f"Q1 FY{str(y+1)[-2:]}"     # Jun quarter
    elif m in (8, 9, 10):     return f"Q2 FY{str(y+1)[-2:]}"     # Sep quarter
    else:                     return f"Q3 FY{str(y+1)[-2:]}"     # Dec quarter

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

    search_queries = []
    for q_var in q_search_variants:
        search_queries.append(f"{ticker} concall {q_var}")
        search_queries.append(f"{ticker} earnings call {q_var}")
        if company_name and company_name.upper() != ticker.upper():
            search_queries.append(f"{company_name} concall {q_var}")

    # Deduplicate while preserving order
    seen = set()
    unique_queries = []
    for q in search_queries:
        q_lower = q.lower()
        if q_lower not in seen:
            seen.add(q_lower)
            unique_queries.append(q)

    # Limit to top 6 queries to keep YouTube API / search requests fast
    unique_queries = unique_queries[:6]

    proxy_url = _os.environ.get("RESIDENTIAL_PROXY_URL")

    ydl_opts = {
        'quiet': True,
        'skip_download': True,
        'extract_flat': False,
    }
    if proxy_url:
        ydl_opts['proxy'] = proxy_url

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
        """Check if channel is 'Trendlyne', 'AlphaStreet India', or 'Concall'."""
        if not channel:
            return False
        chan_lower = channel.lower()
        return "trendlyne" in chan_lower or "alphastreet" in chan_lower or "concall" in chan_lower

    # ── Collect candidates across all queries ──
    all_candidates = []       # list of dicts with metadata
    seen_urls = set()

    for query in unique_queries:
        search_query = f"ytsearch5:{query}"
        print(f"CONCALL_AGENT: YouTube search: '{query}'", file=sys.stderr)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                results = ydl.extract_info(search_query, download=False)
                videos = results.get('entries', [])

                if not videos:
                    continue

                for vid in videos:
                    if not vid:
                        continue
                    url = vid.get('webpage_url', vid.get('url', ''))
                    if not url or url in seen_urls:
                        continue

                    duration = vid.get('duration', 0)
                    if duration and duration < 1200:      # must be > 20 min
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

                    # ── Immediate win: recent + title matches target quarter + is concall + preferred channel ──
                    if is_recent and matches_quarter and is_concall and is_preferred:
                        print(f"CONCALL_AGENT: ✓ Perfect match (preferred channel): {url}  (title: {title})", file=sys.stderr)
                        return url

                    all_candidates.append({
                        'url': url,
                        'title': title,
                        'upload_date': vid.get('upload_date', 'unknown'),
                        'duration_mins': round(duration / 60, 1) if duration else 'unknown',
                        'channel': channel,
                        'is_recent': is_recent,
                        'matches_quarter': matches_quarter,
                        'is_concall': is_concall,
                        'is_preferred': is_preferred,
                    })

            except Exception as e:
                print(f"CONCALL_AGENT YouTube search error for '{query}': {e}", file=sys.stderr)
                continue

    # ── No perfect match — ask Gemini to disambiguate ──
    if not all_candidates:
        print("CONCALL_AGENT: No suitable YouTube video found across all queries", file=sys.stderr)
        return ""

    print(
        f"CONCALL_AGENT: No perfect match. {len(all_candidates)} candidate(s) found, "
        "asking Gemini to pick...", file=sys.stderr
    )

    candidates_text = ""
    for i, c in enumerate(all_candidates, 1):
        pref_suffix = " [Preferred Channel: Trendlyne / AlphaStreet India / Concall]" if c['is_preferred'] else ""
        candidates_text += (
            f"Video {i}:\n"
            f"  Title: {c['title']}\n"
            f"  Upload Date: {c['upload_date']}\n"
            f"  Duration: {c['duration_mins']} minutes\n"
            f"  Channel: {c['channel']}{pref_suffix}\n"
            f"  URL: {c['url']}\n\n"
        )

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
        "3. Prefer videos from preferred financial channels: 'Trendlyne' (@trendlyne), 'AlphaStreet India' (@AlphaStreetIndia) "
        "and 'Concall' (@concall_in), or the company's official channel.\n"
        '4. If none of the videos are a plausible match for the target quarter\'s '
        'concall, respond with "NONE".\n\n'
        'Respond with ONLY the video number (e.g., "1") or "NONE".  '
        "No explanation needed."
    )

    try:
        GOOGLE_API_KEY = _os.getenv("GOOGLE_API_KEY")
        if not GOOGLE_API_KEY:
            print("CONCALL_AGENT: No API key for Gemini fallback", file=sys.stderr)
            return ""

        _genai_client = genai.Client(api_key=GOOGLE_API_KEY)
        response = _genai_client.models.generate_content(
            model='gemini-3.1-flash-lite',
            contents=[types.Part.from_text(text=gemini_prompt)],
        )

        answer = (response.text.strip() if response.text else "").upper()
        print(f"CONCALL_AGENT: Gemini picked: '{answer}'", file=sys.stderr)

        if answer == "NONE":
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

        print(f"CONCALL_AGENT: Could not parse Gemini response: '{answer}'", file=sys.stderr)
        return ""

    except Exception as e:
        print(f"CONCALL_AGENT: Gemini fallback failed: {e}", file=sys.stderr)
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


async def _fetch_latest_results_quarter(ticker: str) -> str:
    """
    Lightweight HTTP call to Screener.in to extract the latest quarterly
    results column header (e.g., 'Dec 2025').
    Checks BOTH consolidated and standalone URLs, returns whichever is newer.
    Does NOT download any PDFs — only parses the HTML for table headers.
    Returns the quarter string or empty string on failure.
    """
    try:
        consolidated_url = f"https://www.screener.in/company/{ticker}/consolidated/"
        standalone_url = f"https://www.screener.in/company/{ticker}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        quarter_pattern = re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}$', re.IGNORECASE)

        async def _get_quarter_from_url(url):
            try:
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    response = await client.get(url, headers=headers, timeout=15.0)
                    if response.status_code != 200:
                        return ""
                    soup = BeautifulSoup(response.text, 'html.parser')
                    quarters_section = soup.find('section', id='quarters')
                    if not quarters_section:
                        return ""
                    table = quarters_section.find('table')
                    if not table:
                        return ""
                    header_row = table.find('thead')
                    if not header_row:
                        return ""
                    headers_list = [th.get_text(strip=True) for th in header_row.find_all('th')]
                    quarter_headers = [h for h in headers_list if quarter_pattern.match(h.strip())]
                    if quarter_headers:
                        return quarter_headers[-1].strip()
            except Exception:
                pass
            return ""

        # Fetch both in parallel for speed
        consol_q, standalone_q = await asyncio.gather(
            _get_quarter_from_url(consolidated_url),
            _get_quarter_from_url(standalone_url)
        )

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

        return best
    except Exception as e:
        print(f"CONCALL_AGENT: _fetch_latest_results_quarter failed for {ticker}: {e}", file=sys.stderr)
        return ''


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
            ytt_api = YouTubeTranscriptApi()
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

                # Wait for processing
                import time as _time
                wait_count = 0
                while uploaded.state and str(uploaded.state) == 'PROCESSING' and wait_count < 60:
                    _time.sleep(2)
                    uploaded = _genai_client.files.get(name=uploaded.name)
                    wait_count += 1

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

    uploaded_file = None

    try:
        print(f"CONCALL_AGENT: Downloading {media_type} from {url}", file=sys.stderr)

        # Download the file — try httpx first, then curl_cffi fallback
        file_bytes = None
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "audio/mpeg,audio/*,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": url.rsplit('/', 1)[0] + "/",
        }
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.get(url, headers=headers, timeout=120.0)
                response.raise_for_status()
            file_bytes = response.content
        except Exception as httpx_err:
            print(f"CONCALL_AGENT: httpx audio download failed ({httpx_err}), trying curl_cffi...", file=sys.stderr)
            try:
                def _cffi_audio_download(audio_url, use_proxy=False):
                    from curl_cffi import requests as cffi_requests
                    import os as _os2
                    proxies = None
                    if use_proxy:
                        proxy_url = _os2.environ.get("RESIDENTIAL_PROXY_URL")
                        if proxy_url:
                            proxies = {"http": proxy_url, "https": proxy_url}
                        else:
                            return None
                    session = cffi_requests.Session(impersonate="chrome110", proxies=proxies)
                    r = session.get(
                        audio_url,
                        headers={"Referer": audio_url.rsplit('/', 1)[0] + "/",
                                 "Accept": "audio/mpeg,audio/*,*/*;q=0.8"},
                        allow_redirects=True, timeout=120
                    )
                    if r.status_code == 200 and r.content:
                        return r.content
                    return None
                # Attempt 1: Direct (no proxy)
                file_bytes = await asyncio.to_thread(_cffi_audio_download, url, False)
                if not file_bytes:
                    # Attempt 2: With proxy
                    file_bytes = await asyncio.to_thread(_cffi_audio_download, url, True)
                if file_bytes:
                    print(f"CONCALL_AGENT: ✓ curl_cffi audio download succeeded ({len(file_bytes)} bytes)", file=sys.stderr)
            except Exception as cffi_err:
                print(f"CONCALL_AGENT: curl_cffi audio fallback also failed: {cffi_err}", file=sys.stderr)

        if not file_bytes:
            print(f"CONCALL_AGENT: ❌ All audio download methods failed for {url}", file=sys.stderr)
            return ''

        file_size_mb = len(file_bytes) / (1024 * 1024)
        print(f"CONCALL_AGENT: Downloaded {file_size_mb:.1f} MB of {media_type}", file=sys.stderr)

        # Determine mime type
        clean_url = url.split('?')[0].split('#')[0]
        mime_type = mimetypes.guess_type(clean_url)[0]
        if not mime_type:
            mime_type = f"{media_type}/mp4" if media_type == 'video' else f"{media_type}/mpeg"

        # Upload to Gemini Files API (blocking — run in thread)
        def _blocking_transcribe(content, mime, filename):
            _genai_client = genai.Client(api_key=GOOGLE_API_KEY)

            print(f"CONCALL_AGENT: Uploading {media_type} to Gemini Files API ({mime})...", file=sys.stderr)
            uploaded = _genai_client.files.upload(
                file=BytesIO(content),
                config=types.UploadFileConfig(
                    display_name=filename,
                    mime_type=mime
                )
            )
            print(f"CONCALL_AGENT: Uploaded as '{uploaded.name}'", file=sys.stderr)

            # Wait for processing if needed (video files may take time)
            import time as _time
            wait_count = 0
            while uploaded.state and str(uploaded.state) == 'PROCESSING' and wait_count < 60:
                _time.sleep(2)
                uploaded = _genai_client.files.get(name=uploaded.name)
                wait_count += 1
                if wait_count % 5 == 0:
                    print(f"CONCALL_AGENT: Waiting for {media_type} processing... ({wait_count * 2}s)", file=sys.stderr)

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
            _blocking_transcribe, file_bytes, mime_type, filename
        )

        if transcript:
            print(f"CONCALL_AGENT: Transcribed {len(transcript)} chars from {media_type}", file=sys.stderr)

        return transcript[:max_chars] if transcript else ''

    except Exception as e:
        print(f"CONCALL_AGENT: Failed to transcribe {media_type} from {url}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return ''


def register_concall_routes(app, call_gemini_api_fn, fetch_documents_fn, get_pdf_text_fn):
    """
    Register all Concall Agent API routes with the Flask app.

    Args:
        app: Flask app instance
        call_gemini_api_fn: Reference to call_gemini_api from handler.py
        fetch_documents_fn: Reference to fetch_latest_documents_async from screener_fetcher
        get_pdf_text_fn: Reference to get_text_from_pdf_url_async from screener_fetcher
    """

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
            return jsonify({'error': 'Job not found or expired'}), 404

        if job['status'] == 'processing':
            return jsonify({
                'status': 'processing',
                'progress': job['progress'],
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
                'quarter_mismatch': job.get('quarter_mismatch', False)
            }), 500

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

        try:
            # Run both fetches concurrently for speed
            documents, results_quarter = loop.run_until_complete(
                asyncio.gather(
                    fetch_documents_fn(ticker),
                    _fetch_latest_results_quarter(ticker)
                )
            )
        finally:
            loop.close()

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
            
        auto_fetch_failed = False
        auto_fetch_source = None

        if (not concall_text or len(concall_text.strip()) < 200) or quarter_mismatch:
            if quarter_mismatch:
                print(f"CONCALL_AGENT: ⚠️ Quarter mismatch! Concall={concall_quarter}, Results={results_quarter}", file=sys.stderr)
            else:
                print(f"CONCALL_AGENT: ⚠️ No concall transcript found for {ticker}", file=sys.stderr)
                
            update_agent_job(job_id, {'progress': 'Transcript missing or outdated. Auto-searching for latest concall audio...'})
            
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

            # 2a. Try to fetch REC link from Screener
            from fetchers.screener_fetcher import fetch_concall_rec_url_async
            loop2 = asyncio.new_event_loop()
            asyncio.set_event_loop(loop2)
            try:
                rec_info = loop2.run_until_complete(fetch_concall_rec_url_async(ticker))
            finally:
                loop2.close()
                
            # Keep a separate loop for transcriptions
            loop3 = asyncio.new_event_loop()
            asyncio.set_event_loop(loop3)
            
            try:
                # First, test Screener REC URL
                if rec_info and rec_info.get("url"):
                    rec_date = rec_info.get("date", "")
                    rec_quarter = _date_to_quarter(rec_date)
                    if not results_quarter or not rec_quarter or (rec_quarter == results_quarter) or (not concall_quarter) or (rec_date != concall_raw_date):
                        new_audio_url = rec_info.get("url")
                        new_source = "screener_rec"
                        print(f"CONCALL_AGENT: Found REC link on Screener: {new_audio_url}", file=sys.stderr)
                        
                        try:
                            # Attempt transcription NOW
                            transcribed_text = loop3.run_until_complete(_try_transcribe(new_audio_url))
                            if transcribed_text and not transcribed_text.startswith("Error"):
                                concall_text = transcribed_text
                                concall_link = new_audio_url
                                concall_label = "Auto-Fetched Audio Transcription"
                                concall_quarter = results_quarter if results_quarter else "Latest"
                                quarter_mismatch = False
                                auto_fetch_source = new_source
                            else:
                                print("CONCALL_AGENT: Transcription returned empty or error for REC url", file=sys.stderr)
                                new_audio_url = "" # Reset to allow fallback
                        except Exception as e:
                            print(f"CONCALL_AGENT: Transcription of REC url failed entirely: {e}", file=sys.stderr)
                            new_audio_url = "" # Reset to allow fallback

                # 2b. If no valid REC link or transcription failed, search YouTube
                if not new_audio_url:
                    update_agent_job(job_id, {'progress': 'Searching YouTube for latest earnings call...'})
                    company_name = _get_company_name_from_ticker(ticker)
                    
                    search_q = results_quarter if results_quarter else ""
                    yt_audio_url = _search_youtube_concall(company_name, ticker, search_q)
                    
                    if yt_audio_url:
                        new_source = "youtube_search"
                        print(f"CONCALL_AGENT: Found YouTube hit: {yt_audio_url}", file=sys.stderr)
                        try:
                            transcribed_text = loop3.run_until_complete(_try_transcribe(yt_audio_url))
                            if transcribed_text and not transcribed_text.startswith("Error"):
                                concall_text = transcribed_text
                                concall_link = yt_audio_url
                                concall_label = "Auto-Fetched YouTube Transcription"
                                concall_quarter = results_quarter if results_quarter else "Latest"
                                quarter_mismatch = False
                                auto_fetch_source = new_source
                                new_audio_url = yt_audio_url
                            else:
                                auto_fetch_failed = True
                        except Exception as e:
                            print(f"CONCALL_AGENT: Transcription of YT url failed: {e}", file=sys.stderr)
                            auto_fetch_failed = True
                    else:
                        auto_fetch_failed = True

            finally:
                loop3.close()
                if not new_audio_url:
                    auto_fetch_failed = True

        if not concall_text or len(concall_text.strip()) < 200:
            elapsed = int(time.time() - start_time)
            print(f"CONCALL_AGENT: No concall transcript found for {ticker} after {elapsed}s", file=sys.stderr)
            
            update_agent_job(job_id, {
                'status': 'error',
                'error': CONCALL_FETCH_ERROR_MSG,
                'auto_fetch_failed': auto_fetch_failed,
                'quarter_mismatch': quarter_mismatch
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
