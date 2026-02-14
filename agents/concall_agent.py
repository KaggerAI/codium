"""
concall_agent.py — Concall Analysis Agent for Kagger AI Agent Marketplace.

Fetches the latest conference call transcript from Screener.in,
produces a structured institutional-grade analysis using Gemini,
and provides a contextual chat interface for follow-up Q&A.
"""

import sys
import time
import asyncio
import threading
import traceback

from flask import request, jsonify

from agents.base import (
    create_agent_job, update_agent_job, get_agent_job,
    store_latest_result, get_latest_result
)
from agents.prompts.concall_prompts import (
    CONCALL_ANALYSIS_PROMPT, CONCALL_CHAT_PROMPT, CONCALL_FETCH_ERROR_MSG
)


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
                thinking_level='LOW'
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


def _run_concall_analysis(job_id, ticker, call_gemini_api_fn, fetch_documents_fn, get_pdf_text_fn):
    """
    Background function that runs the full Concall Agent analysis pipeline:
    1. Fetch the latest concall transcript from Screener.in
    2. Send to Gemini for structured analysis
    3. Store the result
    """
    start_time = time.time()

    try:
        update_agent_job(job_id, {'progress': f'Fetching concall transcript for {ticker}...'})
        print(f"CONCALL_AGENT: Step 1 — Fetching documents for {ticker}", file=sys.stderr)

        # Step 1: Fetch documents using existing screener_fetcher function
        # Run the async function in a new event loop (we're in a background thread)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            documents = loop.run_until_complete(fetch_documents_fn(ticker))
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

        elapsed_fetch = int(time.time() - start_time)
        print(f"CONCALL_AGENT: Step 1 complete — Got {len(concall_text)} chars of transcript in {elapsed_fetch}s", file=sys.stderr)

        # Step 2: Send to Gemini for analysis
        update_agent_job(job_id, {
            'progress': f'Analyzing transcript with AI ({len(concall_text)} chars)...'
        })
        print(f"CONCALL_AGENT: Step 2 — Sending to Gemini for analysis", file=sys.stderr)

        analysis_prompt = f"""{CONCALL_ANALYSIS_PROMPT}

## Company: {ticker}
## Transcript Source: {concall_label}

## Full Transcript:
{concall_text}
"""

        messages = [{"role": "user", "content": analysis_prompt}]

        analysis_result = call_gemini_api_fn(
            messages,
            model="gemini-3-flash-preview",
            temperature=1,
            thinking_level='HIGH'   # Deep reasoning for thorough analysis
        )

        elapsed_total = int(time.time() - start_time)
        print(f"CONCALL_AGENT: Step 2 complete — Got {len(analysis_result)} chars of analysis in {elapsed_total}s", file=sys.stderr)

        # Step 3: Store result
        result_data = {
            'analysis': analysis_result,
            'ticker': ticker,
            'concall_label': concall_label,
            'concall_link': concall_link,
            'transcript_text': concall_text,  # Store for chat context
            'analyzed_at': time.time(),
            'analysis_time_seconds': elapsed_total
        }

        # Save to persistent store (survives tab switching)
        store_latest_result('concall', ticker, result_data)

        # Update job as complete
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
