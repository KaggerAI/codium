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
    Does NOT download any PDFs — only parses the HTML for table headers.
    Returns the quarter string or empty string on failure.
    """
    try:
        urls = [
            f"https://www.screener.in/company/{ticker}/consolidated/",
            f"https://www.screener.in/company/{ticker}/"
        ]
        headers = {"User-Agent": "Mozilla/5.0"}

        async with httpx.AsyncClient(follow_redirects=True) as client:
            for url in urls:
                try:
                    response = await client.get(url, headers=headers, timeout=15.0)
                    if response.status_code != 200:
                        continue

                    soup = BeautifulSoup(response.text, 'html.parser')
                    # Find the quarterly results section
                    quarters_section = soup.find('section', id='quarters')
                    if not quarters_section:
                        continue

                    # Get the table header row
                    table = quarters_section.find('table')
                    if not table:
                        continue

                    header_row = table.find('thead')
                    if not header_row:
                        continue

                    headers_list = [th.get_text(strip=True) for th in header_row.find_all('th')]
                    # Filter for quarter-like headers (e.g., 'Dec 2025', 'Sep 2025')
                    quarter_pattern = re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}$', re.IGNORECASE)
                    quarter_headers = [h for h in headers_list if quarter_pattern.match(h.strip())]

                    if quarter_headers:
                        latest = quarter_headers[-1].strip()  # Last column = latest quarter
                        print(f"CONCALL_AGENT: Latest results quarter for {ticker}: {latest}", file=sys.stderr)
                        return latest
                except Exception as inner_e:
                    print(f"CONCALL_AGENT: Error fetching quarter from {url}: {inner_e}", file=sys.stderr)
                    continue

        print(f"CONCALL_AGENT: Could not determine latest results quarter for {ticker}", file=sys.stderr)
        return ''
    except Exception as e:
        print(f"CONCALL_AGENT: _fetch_latest_results_quarter failed for {ticker}: {e}", file=sys.stderr)
        return ''


async def _extract_text_from_webpage(url: str, max_chars: int = 80000) -> str:
    """
    Extract text content from a webpage URL (for concall transcripts
    hosted on sites like Trendlyne, MoneyControl, etc.).
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
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

                # Download audio only via yt-dlp
                import yt_dlp
                ydl_opts = {
                    'format': 'bestaudio[ext=m4a]/bestaudio/best',
                    'outtmpl': output_path,
                    'quiet': True,
                    'no_warnings': True,
                    'extract_flat': False,
                }

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

        # Download the file
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, headers=headers, timeout=120.0)
            response.raise_for_status()

        file_bytes = response.content
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
                'error': job['error']
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
                transcript=transcript_context[:30000] if transcript_context else "Original transcript not available."
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

        if not concall_text or len(concall_text.strip()) < 200:
            elapsed = int(time.time() - start_time)
            print(f"CONCALL_AGENT: No concall transcript found for {ticker} after {elapsed}s", file=sys.stderr)
            update_agent_job(job_id, {
                'status': 'error',
                'error': CONCALL_FETCH_ERROR_MSG
            })
            return

        concall_link = concall_doc.get('link', '') if concall_doc else ''
        concall_label = concall_doc.get('text', 'Latest Concall') if concall_doc else 'Latest Concall'
        concall_raw_date = concall_doc.get('date', '') if concall_doc else ''

        # Determine concall quarter from its publication date
        concall_quarter = _date_to_quarter(concall_raw_date)
        quarter_mismatch = False
        if results_quarter and concall_quarter:
            quarter_mismatch = (concall_quarter != results_quarter)
            if quarter_mismatch:
                print(f"CONCALL_AGENT: ⚠️ Quarter mismatch! Concall={concall_quarter}, Results={results_quarter}", file=sys.stderr)
            else:
                print(f"CONCALL_AGENT: ✓ Quarters match: {concall_quarter}", file=sys.stderr)

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
