"""
cosmic_agent.py — Cosmic Financial Analyst Agent for the Kagger AI Agent Marketplace.

Combines geopolitical intelligence, macroeconomic indicators, and astrological/planetary
analysis to produce institutional-grade global market forecasts using GPT-5.5.

Pipeline:
  1. Perplexity sonar-pro (pro_search) → Geopolitical intelligence
  2. Perplexity sonar-pro (pro_search) → Astrological/cosmic data
  3. Perplexity /search API → Economic indicators
  4. GPT-5.5 synthesis → Full 11-section cosmic macro report
"""

import sys
import os
import time
import threading
import traceback
import json
import datetime
from agents.utils.ephemeris import generate_cosmic_data_report
from agents.utils.market_data import get_live_market_data

from flask import request, jsonify

from agents.base import (
    create_agent_job, update_agent_job, get_agent_job,
    store_latest_result, get_latest_result,
)
from agents.prompts.cosmic_prompts import (
    COSMIC_SYNTHESIS_PROMPT, COSMIC_CHAT_PROMPT,
    COSMIC_ASTRO_FRAMEWORK, COSMIC_PDF_AUGMENTATION_TEXT,
    COSMIC_GEOPOLITICAL_QUERY, COSMIC_ASTRO_QUERY, COSMIC_ECONOMIC_QUERY,
)


# =====================================================================
# CONSTANTS
# =====================================================================
COSMIC_CACHE_TTL_HOURS = 16  # Cache results for 16 hours
COSMIC_CACHE_KEY = "GLOBAL"  # Non-ticker agent uses a fixed key


# =====================================================================
# DATA FETCHING HELPERS
# =====================================================================

def _fetch_geopolitical_intelligence(call_perplexity_api_fn):
    """
    Step 1: Fetch live geopolitical events via Perplexity sonar-pro with Pro Search.
    Returns the raw text response with citations.
    """
    messages = [
        {"role": "system", "content": "You are a geopolitical intelligence analyst. Provide comprehensive, factual, data-rich analysis of current global events. Include specific dates, numbers, and verified facts."},
        {"role": "user", "content": COSMIC_GEOPOLITICAL_QUERY}
    ]

    try:
        result = call_perplexity_api_fn(
            messages,
            model="sonar-pro",
            temperature=0.3,
            timeout=180,
            use_streaming=True,
            enable_pro_search=True,
        )
        if result:
            print(f"COSMIC_AGENT: Geopolitical intelligence fetched ({len(result)} chars)", file=sys.stderr)
            return result
    except Exception as e:
        print(f"COSMIC_AGENT: Geopolitical fetch error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return "Geopolitical data temporarily unavailable. Use your training knowledge for current events."


def _fetch_astrological_data(call_perplexity_api_fn):
    """
    Step 2: Fetch current astrological/cosmic alignments via Perplexity sonar-pro with Pro Search.
    Returns the raw text response.
    """
    now = datetime.datetime.now()
    # Build date range: current month to +6 months
    end_date = now + datetime.timedelta(days=180)
    date_range = f"{now.strftime('%B %Y')} to {end_date.strftime('%B %Y')}"

    query = COSMIC_ASTRO_QUERY.format(date_range=date_range)

    messages = [
        {"role": "system", "content": "You are an expert astrologer and astronomer. Provide precise planetary positions, transits, retrogrades, eclipses, and astrological events with exact dates and zodiac degrees. Cover both Western and Vedic (sidereal) astrology perspectives."},
        {"role": "user", "content": query}
    ]

    try:
        result = call_perplexity_api_fn(
            messages,
            model="sonar-pro",
            temperature=0.3,
            timeout=180,
            use_streaming=True,
            enable_pro_search=True,
        )
        if result:
            print(f"COSMIC_AGENT: Astrological data fetched ({len(result)} chars)", file=sys.stderr)
            return result
    except Exception as e:
        print(f"COSMIC_AGENT: Astrological fetch error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return "Astrological data temporarily unavailable. Use your training knowledge for current planetary positions."


def _fetch_economic_indicators(call_perplexity_search_api_fn):
    """
    Step 3: Fetch latest economic indicators via Perplexity structured search API.
    Returns formatted text from search results.
    """
    search_domains = [
        "tradingeconomics.com", "worldbank.org", "imf.org",
        "reuters.com", "bloomberg.com", "investing.com",
        "rbi.org.in", "federalreserve.gov", "ecb.europa.eu",
        "boj.or.jp", "bea.gov", "bls.gov",
    ]

    try:
        results = call_perplexity_search_api_fn(
            query=COSMIC_ECONOMIC_QUERY,
            search_domain_filter=search_domains,
            search_recency_filter="week",
            max_results=15,
            max_tokens_per_page=1024,
            timeout=120,
        )

        if results:
            context = "## LATEST ECONOMIC DATA (from verified sources):\n\n"
            for idx, res in enumerate(results):
                context += f"**[{idx+1}] {res.get('title', 'N/A')}**\n"
                context += f"Source: {res.get('url', 'N/A')}\n"
                if res.get('date'):
                    context += f"Date: {res.get('date')}\n"
                context += f"{res.get('snippet', '')}\n\n"

            print(f"COSMIC_AGENT: Economic indicators fetched ({len(results)} sources, {len(context)} chars)", file=sys.stderr)
            return context
    except Exception as e:
        print(f"COSMIC_AGENT: Economic indicators fetch error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

    return "Economic indicator data temporarily unavailable. Use your training knowledge for latest rates and data."


# =====================================================================
# BACKGROUND PIPELINE
# =====================================================================

def _run_cosmic_analysis(
    job_id,
    region_focus,
    call_openai_api_fn,
    call_perplexity_api_fn,
    call_perplexity_search_api_fn,
):
    """
    Background thread: full cosmic analysis pipeline.
    Steps:
        1. Fetch geopolitical intelligence (Perplexity sonar-pro, pro_search)
        2. Fetch astrological/cosmic data (Perplexity sonar-pro, pro_search)
        3. Fetch economic indicators (Perplexity /search API)
        4. Synthesize everything via GPT-5.5
    """
    start_time = time.time()

    try:
        # ── Step 1: Geopolitical Intelligence ─────────────────────
        update_agent_job(job_id, {"progress": "🌍 Gathering global geopolitical intelligence..."})
        print(f"COSMIC_AGENT: Step 1 — Fetching geopolitical intelligence", file=sys.stderr)

        geopolitical_context = _fetch_geopolitical_intelligence(call_perplexity_api_fn)

        step1_time = int(time.time() - start_time)
        print(f"COSMIC_AGENT: Step 1 done in {step1_time}s", file=sys.stderr)

        # ── Step 2: Astrological/Cosmic Data ──────────────────────
        update_agent_job(job_id, {"progress": "🪐 Analyzing planetary transits and cosmic alignments..."})
        print(f"COSMIC_AGENT: Step 2 — Calculating exact astronomical ephemeris data", file=sys.stderr)

        astro_context = generate_cosmic_data_report()

        step2_time = int(time.time() - start_time)
        print(f"COSMIC_AGENT: Step 2 done in {step2_time}s", file=sys.stderr)

        # ── Step 3: Economic Indicators ───────────────────────────
        update_agent_job(job_id, {"progress": "📊 Fetching latest economic indicators and market data..."})
        print(f"COSMIC_AGENT: Step 3 — Fetching economic indicators", file=sys.stderr)

        economic_context = _fetch_economic_indicators(call_perplexity_search_api_fn)
        
        # Append exact deterministic market prices (including MCX India)
        exact_prices = get_live_market_data()
        economic_context = exact_prices + "\n\n" + economic_context

        step3_time = int(time.time() - start_time)
        print(f"COSMIC_AGENT: Step 3 done in {step3_time}s", file=sys.stderr)

        # ── Step 4: GPT-5.5 Synthesis ─────────────────────────────
        update_agent_job(job_id, {"progress": "🌌 GPT-5.5 synthesizing Cosmic Macro Intelligence Report..."})
        print(f"COSMIC_AGENT: Step 4 — GPT-5.5 synthesis", file=sys.stderr)

        # Build the PDF augmentation section
        pdf_section = ""
        if COSMIC_PDF_AUGMENTATION_TEXT.strip():
            pdf_section = f"""
## ADDITIONAL ASTROLOGICAL REFERENCE (from PDF source):
{COSMIC_PDF_AUGMENTATION_TEXT}
"""

        # Build the system prompt with framework + PDF augmentation
        system_prompt = COSMIC_SYNTHESIS_PROMPT.format(
            astro_framework=COSMIC_ASTRO_FRAMEWORK,
            pdf_augmentation=pdf_section,
            region_focus=region_focus,
        )

        # Build the user message with all gathered data
        current_date = datetime.datetime.now().strftime("%B %d, %Y")
        user_message = f"""## REPORT DATE: {current_date}
## REGION FOCUS: {region_focus}

---

## FEED 1: GEOPOLITICAL INTELLIGENCE

{geopolitical_context}

---

## FEED 2: COSMIC/ASTROLOGICAL DATA

{astro_context}

---

## FEED 3: ECONOMIC INDICATORS

{economic_context}

---

Now produce the complete Cosmic Macro Intelligence Report as a single JSON object following the exact schema specified in your instructions. Be comprehensive, data-driven, and blend astrological reasoning with macroeconomic logic throughout. Return ONLY valid JSON."""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # Call GPT-5.5 for synthesis (temperature 1 for bold crystal ball predictions)
        # use_streaming=True keeps the connection active so Azure's SNAT ~4-min idle
        # timeout doesn't silently drop this long-running call (it otherwise hangs
        # forever on Azure while working fine on localhost).
        analysis_result = call_openai_api_fn(
            messages,
            model="gpt-5.5",
            temperature=1,
            timeout=420,  # 7 minutes — this is a massive synthesis
            use_streaming=True,
        )

        elapsed_total = int(time.time() - start_time)
        print(f"COSMIC_AGENT: Step 4 done — {len(analysis_result)} chars in {elapsed_total}s total", file=sys.stderr)

        # ── Parse JSON response ─────────────────────────────────────
        structured_data = None
        raw_analysis = analysis_result

        # Strip markdown code fences if GPT wraps the JSON
        clean = analysis_result.strip()
        if clean.startswith("```"):
            # Remove opening fence (```json or ```)
            first_newline = clean.index("\n")
            clean = clean[first_newline + 1:]
        if clean.endswith("```"):
            clean = clean[:-3].strip()

        try:
            structured_data = json.loads(clean)
            print(f"COSMIC_AGENT: ✓ JSON parsed successfully — {len(structured_data)} top-level keys", file=sys.stderr)
        except json.JSONDecodeError as jde:
            print(f"COSMIC_AGENT: ⚠ JSON parse failed at pos {jde.pos}: {jde.msg}. Falling back to raw text.", file=sys.stderr)
            # Store as raw markdown fallback
            structured_data = None

        # ── Store result ────────────────────────────────────────────
        result_data = {
            "structured": structured_data,  # Parsed JSON (or None)
            "analysis": raw_analysis,  # Raw GPT output (markdown fallback)
            "region_focus": region_focus,
            "geopolitical_context": geopolitical_context[:50000],
            "astro_context": astro_context[:50000],
            "economic_context": economic_context[:50000],
            "analyzed_at": time.time(),
            "analysis_time_seconds": elapsed_total,
            "model_used": "gpt-5.5",
        }

        store_latest_result("cosmic", COSMIC_CACHE_KEY, result_data)

        update_agent_job(job_id, {
            "status": "complete",
            "progress": "Analysis complete!",
            "result": result_data,
            "completed_at": time.time(),
            "total_time": elapsed_total,
        })

        print(f"COSMIC_AGENT: ✓ Complete in {elapsed_total}s", file=sys.stderr)

    except Exception as e:
        print(f"COSMIC_AGENT ERROR: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        update_agent_job(job_id, {
            "status": "error",
            "error": f"Cosmic analysis failed: {str(e)}",
        })


# =====================================================================
# FLASK ROUTE REGISTRATION
# =====================================================================

def register_cosmic_routes(
    app,
    call_openai_api_fn,
    call_perplexity_api_fn,
    call_perplexity_search_api_fn,
):
    """
    Register all Cosmic Financial Analyst Agent API routes.

    Args:
        app: Flask app instance
        call_openai_api_fn: Reference to call_openai_api from handler.py (for GPT-5.5)
        call_perplexity_api_fn: Reference to call_perplexity_api (sonar-pro chat)
        call_perplexity_search_api_fn: Reference to call_perplexity_search_api (/search)
    """

    # =================================================================
    # POST /agent/cosmic/analyze
    # =================================================================
    @app.route("/agent/cosmic/analyze", methods=["POST"])
    def agent_cosmic_analyze():
        """Start a Cosmic Financial Analyst analysis job."""
        try:
            data = request.get_json(force=True)
            region_focus = data.get("region_focus", "All Regions (Global + India)")
            force_refresh = data.get("force_refresh", False)

            print(f"COSMIC_AGENT: Starting analysis — focus: {region_focus}", file=sys.stderr)

            # Check cached result (6-hour TTL)
            if not force_refresh:
                existing = get_latest_result("cosmic", COSMIC_CACHE_KEY)
                if existing:
                    age_hours = (time.time() - existing["stored_at"]) / 3600
                    if age_hours < COSMIC_CACHE_TTL_HOURS:
                        # Check if same region focus
                        cached_focus = existing.get("result", {}).get("region_focus", "")
                        if cached_focus == region_focus:
                            age_minutes = int(age_hours * 60)
                            print(f"COSMIC_AGENT: Returning cached result ({age_minutes}m old)", file=sys.stderr)
                            return jsonify({
                                "status": "complete",
                                "result": existing["result"],
                                "cached": True,
                                "age_minutes": age_minutes,
                            })

            job_id = create_agent_job("cosmic", COSMIC_CACHE_KEY)

            thread = threading.Thread(
                target=_run_cosmic_analysis,
                args=(
                    job_id,
                    region_focus,
                    call_openai_api_fn,
                    call_perplexity_api_fn,
                    call_perplexity_search_api_fn,
                ),
            )
            thread.daemon = True
            thread.start()

            return jsonify({
                "job_id": job_id,
                "status": "processing",
                "message": f"Cosmic analysis started — focus: {region_focus}",
            })

        except Exception as e:
            print(f"COSMIC_AGENT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500

    # =================================================================
    # GET /agent/cosmic/<job_id>/status
    # =================================================================
    @app.route("/agent/cosmic/<job_id>/status", methods=["GET"])
    def agent_cosmic_status(job_id):
        """Poll endpoint for Cosmic Agent job status."""
        try:
            job = get_agent_job(job_id)
            if not job:
                # Include a `status` field so the client can act on it rather than stall.
                return jsonify({"status": "error", "error": "Job not found or expired"}), 404

            if job["status"] == "processing":
                return jsonify({
                    "status": "processing",
                    "progress": job.get("progress", "Analyzing..."),
                    "elapsed_seconds": int(time.time() - job["started_at"]),
                })
            elif job["status"] == "complete":
                return jsonify({"status": "complete", "result": job["result"]})
            elif job["status"] == "error":
                return jsonify({"status": "error", "error": job["error"]})

            return jsonify({"status": "error", "error": "Unknown job status"}), 500
        except Exception as e:
            # Never let the poll endpoint emit an empty/HTML body — always valid JSON.
            print(f"COSMIC_AGENT_STATUS ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({"status": "error", "error": f"Status check failed: {str(e)}"}), 500

    # =================================================================
    # POST /agent/cosmic/chat
    # =================================================================
    @app.route("/agent/cosmic/chat", methods=["POST"])
    def agent_cosmic_chat():
        """Chat about the completed cosmic analysis."""
        try:
            data = request.get_json(force=True)
            question = data.get("question", "").strip()
            analysis_context = data.get("analysis_context", "").strip()

            if not question:
                return jsonify({"error": "Question is required"}), 400
            if not analysis_context:
                return jsonify({"error": "Analysis context is required. Generate the report first."}), 400

            geopolitical_ctx = data.get("geopolitical_context", "Not available")
            astro_ctx = data.get("astro_context", "Not available")
            economic_ctx = data.get("economic_context", "Not available")

            chat_prompt = COSMIC_CHAT_PROMPT.format(
                analysis=analysis_context[:40000],
                geopolitical_context=geopolitical_ctx[:15000],
                astro_context=astro_ctx[:15000],
                economic_context=economic_ctx[:15000],
            )

            messages = [
                {"role": "system", "content": chat_prompt},
                {"role": "user", "content": question},
            ]

            answer = call_openai_api_fn(
                messages,
                model="gpt-5.5",
                temperature=1,
                timeout=180,
            )

            return jsonify({"answer": answer, "status": "success"})

        except Exception as e:
            print(f"COSMIC_AGENT_CHAT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500

    # =================================================================
    # GET /agent/cosmic/latest
    # =================================================================
    @app.route("/agent/cosmic/latest", methods=["GET"])
    def agent_cosmic_latest():
        """Get the latest cached result for persistence across tab switches."""
        result = get_latest_result("cosmic", COSMIC_CACHE_KEY)
        if result:
            return jsonify({
                "status": "complete",
                "result": result["result"],
                "age_minutes": round((time.time() - result["stored_at"]) / 60),
            })

        return jsonify({"status": "none", "message": "No cached result found"})
