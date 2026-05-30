"""
analyst_agent.py — Analyst Report Agent for the Kagger AI Agent Marketplace.

Fetches domestic analyst reports from Trendlyne (via Scrapling) and global
reports (via Perplexity Search API), downloads and summarises full PDFs
with Gemini, and synthesises everything into a single comprehensive view.
"""

import sys
import os
import re
import time
import asyncio
import threading
import traceback
import json
import re

def slugify(text):
    text = text.lower()
    return re.sub(r'[^a-z0-9]', '_', text)
import datetime
from io import BytesIO

from flask import request, jsonify
from google import genai
from google.genai import types

from agents.base import (
    create_agent_job, update_agent_job, get_agent_job,
    store_latest_result, get_latest_result,
)
from agents.prompts.analyst_prompts import (
    ANALYST_SYNTHESIS_PROMPT, ANALYST_CHAT_PROMPT, ANALYST_PDF_SUMMARY_PROMPT,
)


# =====================================================================
# CONSTANTS
# =====================================================================
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
MAX_PDF_DOWNLOADS = 3  # Max PDFs to fully download + summarise


# =====================================================================
# HELPERS
# =====================================================================

def _summarise_pdf_bytes(pdf_bytes: bytes, brokerage: str, ticker: str) -> str:
    """
    Upload PDF bytes to Gemini Files API, summarise, clean up.
    Runs SYNCHRONOUSLY (call via asyncio.to_thread).
    """
    if not GOOGLE_API_KEY:
        return "Error: Google API Key not configured."

    try:
        client = genai.Client(api_key=GOOGLE_API_KEY)

        uploaded = client.files.upload(
            file=BytesIO(pdf_bytes),
            config=types.UploadFileConfig(
                display_name=f"{brokerage}_{ticker}_report.pdf",
                mime_type="application/pdf",
            ),
        )

        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[
                types.Part.from_text(text=ANALYST_PDF_SUMMARY_PROMPT),
                uploaded,
            ],
        )

        # Clean up
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass

        return response.text or ""
    except Exception as e:
        print(f"ANALYST_AGENT: PDF summary failed for {brokerage}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return f"Error summarising {brokerage} report: {str(e)}"


def _fetch_global_reports(ticker: str, company_name: str,
                          call_gemini_api_fn, call_perplexity_search_api_fn) -> list[dict]:
    """
    Fetch global research reports using Perplexity Search API + Gemini
    extraction.  Mirrors the logic in handler.py api_global_analyst_reports().
    """
    search_query = (
        f"latest research reports on {company_name} OR {ticker} stock only by "
        "Morgan Stanley, Goldman Sachs, Jefferies, CLSA, Nomura, Macquarie, "
        "UBS, Nuvama, Bernstein, BofA target price buy sell rating"
    )

    search_domains = [
        "bloomberg.com", "reuters.com", "cnbc.com", "moneycontrol.com",
        "trendlyne.com", "economictimes.indiatimes.com", "livemint.com",
        "business-standard.com", "financialexpress.com", "ndtvprofit.com",
        "bqprime.com", "investing.com", "jefferies.com",
        "morganstanley.com", "goldmansachs.com", "macquarie.com",
    ]

    try:
        from dateutil.relativedelta import relativedelta

        six_months_ago = datetime.datetime.now() - relativedelta(months=6)
        date_filter = (
            six_months_ago.strftime("%#m/%#d/%Y")
            if sys.platform == "win32"
            else six_months_ago.strftime("%-m/%-d/%Y")
        )

        search_results = call_perplexity_search_api_fn(
            query=search_query,
            search_domain_filter=search_domains,
            search_after_date_filter=date_filter,
            max_results=10,
            max_tokens_per_page=1024,
            timeout=120,
        )

        if not search_results:
            print(f"ANALYST_AGENT: No Perplexity results for {ticker}", file=sys.stderr)
            return []

        # Build context block
        context_block = "SEARCH RESULTS:\n"
        for idx, res in enumerate(search_results):
            context_block += f"[{idx+1}] Title: {res.get('title')}\n"
            context_block += f"URL: {res.get('url')}\n"
            if res.get("date"):
                context_block += f"Published Date: {res.get('date')}\n"
            if res.get("last_updated"):
                context_block += f"Last Updated: {res.get('last_updated')}\n"
            context_block += f"Snippet: {res.get('snippet')}\n\n"

        current_date_str = datetime.datetime.now().strftime("%B %Y")

        extraction_prompt = f"""You are an elite financial data extraction assistant working with strict JSON structures.
I will provide you with a list of real-time search results about equity research reports for the Indian stock {company_name} ({ticker}).
Review these search results carefully and extract EVERY mentioned analyst report from reputable global or global-affiliated research houses.

CRITICAL INSTRUCTION 1: You MUST ONLY include reports from the following GLOBAL research houses: Morgan Stanley, Goldman Sachs, Jefferies, CLSA, Nomura, Macquarie, UBS, Nuvama, Bernstein, BofA Securities, Citi, HSBC, JPMorgan.
DO NOT include any domestic Indian brokerages.

CRITICAL INSTRUCTION 2: Only include reports published within the LAST 6 MONTHS. The current month is {current_date_str}.

CRITICAL INSTRUCTION 3: Extract the EXACT PUBLICATION DATE from the "Published Date" metadata.

{context_block}

Return the extracted reports ONLY as a valid JSON array of objects. Do not include markdown formatting.
If no relevant reports found, return [].

[
  {{
    "brokerage": "Morgan Stanley",
    "recommendation": "Overweight / Buy",
    "target_price": "2400",
    "upside": "15%",
    "date": "15 Feb 2026",
    "summary": "Set a target of Rs. 1435 ...",
    "detailed_summary": "In an extensive report...",
    "source_url": "https://example.com/actual-source-link"
  }}
]

Make sure target_price is a clean number string (no symbols).
For "summary", provide 2-3 sentence overview.
For "detailed_summary", provide 2-3 paragraphs.
For "source_url", use the EXACT URL from the search results."""

        messages = [{"role": "user", "content": extraction_prompt}]
        response_text = call_gemini_api_fn(
            messages, model="gemini-3-flash-preview",
            temperature=0.1, thinking_level="MEDIUM",
        )

        cleaned = response_text.strip()
        start_idx = cleaned.find("[")
        end_idx = cleaned.rfind("]")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            cleaned = cleaned[start_idx : end_idx + 1]

        reports = json.loads(cleaned)
        if not isinstance(reports, list):
            return []

        # Sort newest first
        from dateutil import parser as date_parser
        def _sort_key(r):
            try:
                return date_parser.parse(r.get("date", ""), fuzzy=True)
            except Exception:
                return datetime.datetime(1970, 1, 1)

        reports.sort(key=_sort_key, reverse=True)
        print(f"ANALYST_AGENT: Got {len(reports)} global reports for {ticker}", file=sys.stderr)
        return reports

    except Exception as e:
        print(f"ANALYST_AGENT: Global reports fetch failed: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return []


# =====================================================================
# BACKGROUND PIPELINE
# =====================================================================

def _run_analyst_analysis(
    job_id: str,
    ticker: str,
    company_name: str,
    call_gemini_api_fn,
    call_perplexity_search_api_fn,
    fetch_analyst_reports_async_fn,
):
    """
    Background thread: full analyst report analysis pipeline.
    Steps:
        1. Fetch domestic reports from Trendlyne (Scrapling → cache fallback)
        2. Download and summarise top domestic PDFs via Gemini
        3. Fetch global reports via Perplexity + Gemini
        4. Synthesise everything into a comprehensive view
    """
    start_time = time.time()

    try:
        # ── Step 1: Domestic reports from Trendlyne ─────────────────
        update_agent_job(job_id, {"progress": f"Fetching domestic analyst reports for {ticker}..."})
        print(f"ANALYST_AGENT: Step 1 — Fetching domestic reports for {ticker}", file=sys.stderr)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # Try existing fetcher first (hits cache)
            domestic_reports = []

            from analyst_reports.trendlyne_fetcher import get_post_url
            post_url = get_post_url(ticker)

            try:
                domestic_reports = loop.run_until_complete(
                    fetch_analyst_reports_async_fn(ticker)
                )
                if domestic_reports:
                    print(f"ANALYST_AGENT: Got {len(domestic_reports)} domestic reports from cache/fallback", file=sys.stderr)
            except Exception as cache_err:
                print(f"ANALYST_AGENT: Cache fetch failed: {cache_err}", file=sys.stderr)

            # If empty (not in cache), try Scrapling live fetch
            if not domestic_reports:
                if post_url:
                    try:
                        from analyst_reports.scrapling_fetcher import fetch_reports_with_scrapling
                        domestic_reports = loop.run_until_complete(
                            fetch_reports_with_scrapling(ticker, post_url)
                        )
                        if domestic_reports:
                            print(f"ANALYST_AGENT: Scrapling got {len(domestic_reports)} domestic reports", file=sys.stderr)
                    except Exception as scrape_err:
                        print(f"ANALYST_AGENT: Scrapling failed: {scrape_err}, falling back", file=sys.stderr)

            print(f"ANALYST_AGENT: Step 1 done — {len(domestic_reports)} domestic reports", file=sys.stderr)
        except Exception as e:
            print(f"ANALYST_AGENT: Step 1 error: {e}", file=sys.stderr)
            domestic_reports = []

        # ── Step 2: Download + summarise top domestic PDFs ──────────
        pdf_summaries = {}
        reports_with_pdf = [r for r in domestic_reports if r.get("pdf_url")]
        pdfs_to_process = reports_with_pdf[:MAX_PDF_DOWNLOADS]

        if pdfs_to_process:
            update_agent_job(job_id, {
                "progress": f"Downloading & analyzing {len(pdfs_to_process)} domestic report PDFs..."
            })
            print(f"ANALYST_AGENT: Step 2 — Processing {len(pdfs_to_process)} PDFs", file=sys.stderr)

            async def process_single_pdf(i, report, total):
                brokerage = report.get("brokerage", f"Broker_{i}")
                pdf_url = report["pdf_url"]
                
                # We can't safely call `update_agent_job` from multiple async tasks concurrently without race conditions 
                # causing flickering in the UI. We'll just emit prints.
                print(f"ANALYST_AGENT: Analyzing PDF {i}/{total}: {brokerage}...", file=sys.stderr)

                try:
                    pdf_bytes = None
                    try:
                        from analyst_reports.scrapling_fetcher import download_pdf_with_scrapling
                        pdf_bytes = await download_pdf_with_scrapling(pdf_url)
                    except Exception as scrape_pdf_err:
                        print(f"ANALYST_AGENT: Scrapling PDF failed for {brokerage}: {scrape_pdf_err}", file=sys.stderr)

                    if not pdf_bytes:
                        try:
                            from analyst_reports.pdf_summarizer_cookies import download_analyst_pdf_with_cookies
                            # download_analyst_pdf_with_cookies is synchronous, so run in thread if we make it async
                            # Wait, the current version in master says it's an async fn if it was awaited via run_until_complete?
                            # Let's wrap it in to_thread just in case to be perfectly safe, since we are in an async context.
                            from analyst_reports.pdf_summarizer_cookies import download_analyst_pdf_with_cookies
                            if asyncio.iscoroutinefunction(download_analyst_pdf_with_cookies):
                                pdf_bytes = await download_analyst_pdf_with_cookies(pdf_url)
                            else:
                                pdf_bytes = await asyncio.to_thread(download_analyst_pdf_with_cookies, pdf_url)
                        except Exception as cookie_err:
                            print(f"ANALYST_AGENT: Cookie PDF download failed for {brokerage}: {cookie_err}", file=sys.stderr)

                    if pdf_bytes and len(pdf_bytes) > 1000:
                        import os
                        local_filename = f"report_{ticker}_{slugify(brokerage)}.pdf"
                        local_dir = os.path.join(os.getcwd(), "temp_reports")
                        if not os.path.exists(local_dir):
                            os.makedirs(local_dir, exist_ok=True)
                        
                        local_path = os.path.join(local_dir, local_filename)
                        with open(local_path, "wb") as f:
                            f.write(pdf_bytes)
                        
                        report["pdf_url"] = f"/temp_reports/{local_filename}"

                        summary = await asyncio.to_thread(
                            _summarise_pdf_bytes, pdf_bytes, brokerage, ticker
                        )
                        if summary and not summary.startswith("Error"):
                            print(f"ANALYST_AGENT: PDF summary done for {brokerage} ({len(summary)} chars)", file=sys.stderr)
                            return brokerage, summary
                        else:
                            print(f"ANALYST_AGENT: PDF summary failed for {brokerage}: {summary[:100] if summary else 'empty'}", file=sys.stderr)
                    else:
                        print(f"ANALYST_AGENT: No PDF bytes for {brokerage}", file=sys.stderr)

                except Exception as pdf_err:
                    print(f"ANALYST_AGENT: PDF processing error for {brokerage}: {pdf_err}", file=sys.stderr)
                
                return brokerage, None

            update_agent_job(job_id, {"progress": f"Downloading & concurrently analyzing {len(pdfs_to_process)} domestic report PDFs..."})
            
            # Run all PDF downloads and summaries CONCURRENTLY
            tasks = [process_single_pdf(i, r, len(pdfs_to_process)) for i, r in enumerate(pdfs_to_process, 1)]
            results = loop.run_until_complete(asyncio.gather(*tasks))
            
            for brokr, summ in results:
                if summ:
                    pdf_summaries[brokr] = summ

        # ── Step 3: Global reports via Perplexity ───────────────────
        update_agent_job(job_id, {"progress": f"Fetching global research reports for {ticker}..."})
        print(f"ANALYST_AGENT: Step 3 — Fetching global reports", file=sys.stderr)

        global_reports = []
        try:
            global_reports = _fetch_global_reports(
                ticker, company_name,
                call_gemini_api_fn, call_perplexity_search_api_fn,
            )
        except Exception as global_err:
            print(f"ANALYST_AGENT: Global reports failed: {global_err}", file=sys.stderr)

        # ── Step 4: Synthesise everything ───────────────────────────
        update_agent_job(job_id, {"progress": "Fetching live CMP and synthesizing reports..."})
        print(f"ANALYST_AGENT: Step 4 — Synthesising", file=sys.stderr)

        # Fetch current market price (CMP) using yfinance
        cmp_text = "Not available"
        try:
            import yfinance as yf
            yf_ticker = ticker if (ticker.endswith(".NS") or ticker.endswith(".BO")) else f"{ticker}.NS"
            ticker_obj = yf.Ticker(yf_ticker)
            hist = ticker_obj.history(period="1d")
            if not hist.empty:
                cmp_val = float(hist['Close'].iloc[-1])
                cmp_text = f"₹{cmp_val:,.2f}"
            else:
                # Fallback to info
                info = ticker_obj.info
                if info and "currentPrice" in info:
                    cmp_text = f"₹{float(info['currentPrice']):,.2f}"
        except Exception as e:
            print(f"ANALYST_AGENT: Error fetching CMP for {ticker}: {e}", file=sys.stderr)

        # Build context for synthesis
        domestic_context = ""
        for r in domestic_reports:
            brokerage = r.get("brokerage", "Unknown")
            domestic_context += f"\n### {brokerage}\n"
            domestic_context += f"- Recommendation: {r.get('recommendation', 'N/A')}\n"
            domestic_context += f"- Target Price: ₹{r.get('target_price', 'N/A')}\n"
            domestic_context += f"- Upside: {r.get('upside', 'N/A')}\n"
            domestic_context += f"- Date: {r.get('date', 'N/A')}\n"
            domestic_context += f"- Report URL: {r.get('pdf_url') or r.get('report_url', 'Not Available')}\n"
            domestic_context += f"- Summary: {r.get('summary', '')}\n"
            if brokerage in pdf_summaries:
                domestic_context += f"\n**Full PDF Analysis:**\n{pdf_summaries[brokerage]}\n"

        global_context = ""
        for r in global_reports:
            brokerage = r.get("brokerage", "Unknown")
            global_context += f"\n### {brokerage}\n"
            global_context += f"- Recommendation: {r.get('recommendation', 'N/A')}\n"
            global_context += f"- Target Price: ₹{r.get('target_price', 'N/A')}\n"
            global_context += f"- Upside: {r.get('upside', 'N/A')}\n"
            global_context += f"- Date: {r.get('date', 'N/A')}\n"
            global_context += f"- Report URL: {r.get('source_url', 'Not Available')}\n"
            global_context += f"- Summary: {r.get('summary', '')}\n"
            if r.get("detailed_summary"):
                global_context += f"- Detailed: {r['detailed_summary']}\n"

        if not domestic_context and not global_context:
            update_agent_job(job_id, {
                "status": "error",
                "error": f"No analyst reports found for {ticker}. This stock may not have recent brokerage coverage.",
            })
            loop.close()
            return

        synthesis_input = f"""{ANALYST_SYNTHESIS_PROMPT}

## Company: {company_name} ({ticker})
## Current Market Price (CMP): {cmp_text}

## DOMESTIC RESEARCH REPORTS:
{domestic_context if domestic_context else "No domestic reports found."}

## GLOBAL RESEARCH REPORTS:
{global_context if global_context else "No global reports found."}
"""

        messages = [{"role": "user", "content": synthesis_input}]
        analysis_result = call_gemini_api_fn(
            messages, model="gemini-3-flash-preview",
            temperature=1, thinking_level="HIGH",
        )

        elapsed_total = int(time.time() - start_time)
        print(f"ANALYST_AGENT: Step 4 done — {len(analysis_result)} chars in {elapsed_total}s", file=sys.stderr)

        # ── Store result ────────────────────────────────────────────
        result_data = {
            "analysis": analysis_result,
            "ticker": ticker,
            "company_name": company_name,
            "domestic_reports": domestic_reports,
            "global_reports": global_reports,
            "pdf_summaries": pdf_summaries,
            "domestic_count": len(domestic_reports),
            "global_count": len(global_reports),
            "pdfs_analyzed": len(pdf_summaries),
            "analyzed_at": time.time(),
            "analysis_time_seconds": elapsed_total,
        }

        store_latest_result("analyst", ticker, result_data)

        update_agent_job(job_id, {
            "status": "complete",
            "progress": "Analysis complete!",
            "result": result_data,
            "completed_at": time.time(),
            "total_time": elapsed_total,
        })

        loop.close()
        print(f"ANALYST_AGENT: ✓ Complete for {ticker} in {elapsed_total}s", file=sys.stderr)

    except Exception as e:
        print(f"ANALYST_AGENT ERROR: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        update_agent_job(job_id, {
            "status": "error",
            "error": f"Analysis failed: {str(e)}",
        })


def await_in_thread(fn, *args):
    """Run a synchronous function, useful for calling blocking code from the background thread."""
    return fn(*args)


# =====================================================================
# FLASK ROUTE REGISTRATION
# =====================================================================

def register_analyst_routes(
    app,
    call_gemini_api_fn,
    call_perplexity_search_api_fn,
    fetch_analyst_reports_async_fn,
):
    """
    Register all Analyst Report Agent API routes.

    Args:
        app: Flask app instance
        call_gemini_api_fn: Reference to call_gemini_api from handler.py
        call_perplexity_search_api_fn: Reference to call_perplexity_search_api
        fetch_analyst_reports_async_fn: Reference to fetch_analyst_reports_async
    """

    # --- Helper: resolve company name from ticker ---
    def _get_company_name(ticker: str) -> str:
        """Look up company name from the trendlyne master CSV mapping."""
        try:
            from analyst_reports.trendlyne_fetcher import TRENDLYNE_DATA
            data = TRENDLYNE_DATA.get(ticker.upper())
            if data:
                return data.get("stock_name", ticker)
        except Exception:
            pass
        return ticker

    # =================================================================
    # POST /agent/analyst/analyze
    # =================================================================
    @app.route("/agent/analyst/analyze", methods=["POST"])
    def agent_analyst_analyze():
        """Start an Analyst Report Agent analysis job."""
        try:
            data = request.get_json(force=True)
            ticker = data.get("ticker", "").strip().upper()

            if not ticker:
                return jsonify({"error": "Ticker is required"}), 400

            company_name = _get_company_name(ticker)
            print(f"ANALYST_AGENT: Starting analysis for {ticker} ({company_name})", file=sys.stderr)

            # Check cached result
            existing = get_latest_result("analyst", ticker)
            if existing and data.get("force_refresh") is not True:
                age_minutes = (time.time() - existing["stored_at"]) / 60
                if age_minutes < 60:
                    print(f"ANALYST_AGENT: Returning cached result ({age_minutes:.0f}m old)", file=sys.stderr)
                    return jsonify({
                        "status": "complete",
                        "result": existing["result"],
                        "cached": True,
                        "age_minutes": round(age_minutes),
                    })

            job_id = create_agent_job("analyst", ticker)

            thread = threading.Thread(
                target=_run_analyst_analysis,
                args=(
                    job_id, ticker, company_name,
                    call_gemini_api_fn, call_perplexity_search_api_fn,
                    fetch_analyst_reports_async_fn,
                ),
            )
            thread.daemon = True
            thread.start()

            return jsonify({
                "job_id": job_id,
                "status": "processing",
                "message": f"Analyst report analysis started for {ticker}.",
            })

        except Exception as e:
            print(f"ANALYST_AGENT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500

    # =================================================================
    # GET /agent/analyst/<job_id>/status
    # =================================================================
    @app.route("/agent/analyst/<job_id>/status", methods=["GET"])
    def agent_analyst_status(job_id):
        """Poll endpoint for Analyst Agent job status."""
        job = get_agent_job(job_id)
        if not job:
            return jsonify({"error": "Job not found or expired"}), 404

        if job["status"] == "processing":
            return jsonify({
                "status": "processing",
                "progress": job["progress"],
                "elapsed_seconds": int(time.time() - job["started_at"]),
            })
        elif job["status"] == "complete":
            return jsonify({"status": "complete", "result": job["result"]})
        elif job["status"] == "error":
            return jsonify({"status": "error", "error": job["error"]})

        return jsonify({"error": "Unknown job status"}), 500

    # =================================================================
    # POST /agent/analyst/chat
    # =================================================================
    @app.route("/agent/analyst/chat", methods=["POST"])
    def agent_analyst_chat():
        """Chat about the completed analyst report analysis."""
        try:
            data = request.get_json(force=True)
            question = data.get("question", "").strip()
            ticker = data.get("ticker", "").strip().upper()
            analysis_context = data.get("analysis_context", "").strip()
            company_name = data.get("company_name", _get_company_name(ticker))

            if not question:
                return jsonify({"error": "Question is required"}), 400
            if not analysis_context:
                return jsonify({"error": "Analysis context is required. Run the analysis first."}), 400

            # Build domestic/global report text for chat context
            domestic_text = data.get("domestic_reports_text", "Not available")
            global_text = data.get("global_reports_text", "Not available")

            chat_prompt = ANALYST_CHAT_PROMPT.format(
                company_name=company_name,
                ticker=ticker,
                analysis=analysis_context[:40000],
                domestic_reports=domestic_text[:20000],
                global_reports=global_text[:20000],
            )

            messages = [
                {"role": "system", "content": chat_prompt},
                {"role": "user", "content": question},
            ]

            answer = call_gemini_api_fn(
                messages, model="gemini-3-flash-preview",
                temperature=1, thinking_level="HIGH",
            )

            return jsonify({"answer": answer, "status": "success"})

        except Exception as e:
            print(f"ANALYST_AGENT_CHAT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500

    # =================================================================
    # GET /agent/analyst/latest
    # =================================================================
    @app.route("/agent/analyst/latest", methods=["GET"])
    def agent_analyst_latest():
        """Get the latest cached result for persistence across tab switches."""
        ticker = request.args.get("ticker", "").strip().upper()
        if not ticker:
            return jsonify({"error": "Ticker is required"}), 400

        result = get_latest_result("analyst", ticker)
        if result:
            return jsonify({
                "status": "complete",
                "result": result["result"],
                "age_minutes": round((time.time() - result["stored_at"]) / 60),
            })

        return jsonify({"status": "none", "message": "No cached result found"})
