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
from agents import cosmic_engine

from flask import request, jsonify

from agents.base import (
    create_agent_job, update_agent_job, get_agent_job,
    store_latest_result, get_latest_result, cosmic_model_config,
)
from agents.prompts.cosmic_prompts import (
    COSMIC_SYNTHESIS_PROMPT, COSMIC_CHAT_PROMPT,
    COSMIC_PDF_AUGMENTATION_TEXT,
    COSMIC_GEOPOLITICAL_QUERY, COSMIC_ECONOMIC_QUERY,
)


# =====================================================================
# CONSTANTS
# =====================================================================
COSMIC_CACHE_TTL_HOURS = 16  # Cache results for 16 hours
COSMIC_CACHE_KEY = "GLOBAL"  # Non-ticker agent uses a fixed key

# Synthesis model / reasoning effort, shared with the micro agent via agents/base.py so the two
# cannot drift apart. Defaults are gpt-5.4 at xhigh effort — see the rationale in base.py, in
# short: gpt-5.4 is covered by the complimentary daily token allowance and gpt-5.5 is not.
# Overridable with COSMIC_SYNTHESIS_MODEL / COSMIC_REASONING_EFFORT.
COSMIC_SYNTHESIS_MODEL, COSMIC_REASONING_EFFORT = cosmic_model_config()

# Prompt-path caps for the two live feeds. Without these the user message is unbounded:
# the Perplexity /search economic feed alone can reach ~60k chars at max_tokens_per_page
# =1024 across 15 results, which is the main source of run-to-run token variance. The
# [:50000] slices further down apply only to the STORED copy, not to the prompt.
COSMIC_GEOPOLITICAL_PROMPT_CAP = 12000
COSMIC_ECONOMIC_PROMPT_CAP = 24000


def _extract_json_object(text):
    """
    Best-effort extraction of a single JSON object from an LLM response.

    Handles the two ways GPT commonly breaks strict json.loads:
      - wrapping the object in a ```json ... ``` (or plain ```) markdown fence, and
      - adding prose before/after the object.
    Returns the candidate JSON string (caller still parses it, with strict=False so
    literal control characters inside strings are tolerated).
    """
    if not text:
        return ""
    s = text.strip()
    # Pull the body out of a fenced block if one is present (anywhere, not just edges).
    if "```" in s:
        import re
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.DOTALL)
        if m and m.group(1).strip():
            s = m.group(1).strip()
    # Slice to the outermost braces to drop any stray prose around the object.
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start:end + 1]
    return s


def _repair_truncated_json(text):
    """
    Salvage a JSON object that the model stopped writing mid-way.

    A synthesis that runs out of output budget ends mid-token - an unterminated string plus a
    couple of unclosed containers - which fails json.loads outright and drops the entire report
    to the raw-text fallback even though almost all of it arrived intact. One observed run lost
    52 characters of 89,456 and still rendered nothing but a wall of raw JSON.

    This rewinds to the last position at which every open container held only complete values,
    then closes the containers that were open there. Commas and braces inside string literals
    are ignored, as are escaped quotes.

    Returns "" when nothing is salvageable, so the caller falls back to raw text as before.
    """
    if not text:
        return ""

    stack = []
    in_string = False
    escape = False
    cut = -1          # slice end of the last known-complete state
    cut_stack = None  # containers open at that point

    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack:
                stack.pop()
            cut, cut_stack = i + 1, list(stack)
        elif ch == ",":
            # Everything before a comma is a complete element of the current container.
            cut, cut_stack = i, list(stack)

    if cut <= 0 or not cut_stack:
        return ""
    return text[:cut].rstrip().rstrip(",") + "".join(reversed(cut_stack))


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

        # Shadow engine: compute the deterministic chart alongside the existing feed. This does
        # NOT touch the prompt — it exists so we can measure how often the model's asserted
        # D9/D10/pada/sub-sector values were right before anything depends on the engine.
        # See docs/COSMIC_ENGINE_MIGRATION.md section 4.0.1. Disable with COSMIC_ENGINE_MODE=off.
        engine_state = cosmic_engine.run_shadow(datetime.datetime.now(datetime.timezone.utc))

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

        # ── Step 4: Synthesis ─────────────────────────────────────
        update_agent_job(job_id, {"progress": "🌌 Synthesizing Cosmic Macro Intelligence Report..."})
        print(f"COSMIC_AGENT: Step 4 — synthesis "
              f"(model={COSMIC_SYNTHESIS_MODEL}, effort={COSMIC_REASONING_EFFORT or 'default'})",
              file=sys.stderr)

        # Build the PDF augmentation section
        pdf_section = ""
        if COSMIC_PDF_AUGMENTATION_TEXT.strip():
            pdf_section = f"""
## ADDITIONAL ASTROLOGICAL REFERENCE (from PDF source):
{COSMIC_PDF_AUGMENTATION_TEXT}
"""

        # Build the system prompt with the PDF augmentation
        system_prompt = COSMIC_SYNTHESIS_PROMPT.format(
            pdf_augmentation=pdf_section,
            region_focus=region_focus,
        )

        # Build the user message with all gathered data. Both live feeds are capped here —
        # see COSMIC_*_PROMPT_CAP above for why.
        current_date = datetime.datetime.now().strftime("%B %d, %Y")
        user_message = f"""## REPORT DATE: {current_date}
## REGION FOCUS: {region_focus}

---

## FEED 1: GEOPOLITICAL INTELLIGENCE

{geopolitical_context[:COSMIC_GEOPOLITICAL_PROMPT_CAP]}

---

## FEED 2: COSMIC/ASTROLOGICAL DATA

{astro_context}

---

## FEED 3: ECONOMIC INDICATORS

{economic_context[:COSMIC_ECONOMIC_PROMPT_CAP]}

---

Now produce the complete Cosmic Macro Intelligence Report as a single JSON object following the exact schema specified in your instructions. Be comprehensive, data-driven, and blend astrological reasoning with macroeconomic logic throughout. Return ONLY valid JSON."""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # Token accounting for the migration (docs/COSMIC_ENGINE_MIGRATION.md section 7.3):
        # every phase must move this number in the intended direction.
        print(f"COSMIC_AGENT: prompt size — system={len(system_prompt)} ch, "
              f"user={len(user_message)} ch, total={len(system_prompt) + len(user_message)} ch",
              file=sys.stderr)

        # The synthesis is the long pole of the whole run, and almost all of it is silent:
        # gpt-5.4 at xhigh reasoning sits on a ~138k-char prompt and emits NOTHING for minutes
        # before the first content token, then streams the ~92k-char report in about three.
        # Two consequences, both of which bit us:
        #
        #   1. timeout=420 was cutting it far too fine. A measured run went 413s from request
        #      to first token — inside a 420s read timeout by seven seconds. Anything slower
        #      raises APITimeoutError and throws away ~10 minutes of completed work, which is
        #      the intermittent "cosmic agent is broken" failure. 900s restores real headroom;
        #      it costs nothing when the model is quick, since this is a read timeout.
        #   2. Nothing moved in the UI during that silence. The progress line sat frozen on
        #      "Synthesizing..." for 7+ minutes, indistinguishable from a hung job, so runs were
        #      being killed manually before they could ever finish.
        #
        # The ticker below fixes (2). It has to be a wall-clock thread rather than a stream
        # callback: during the reasoning phase there is nothing on the socket at all, so a
        # callback driven by chunk arrival cannot fire. progress_callback then supplies the
        # character count once the model actually starts writing.
        synth_start = time.time()
        synth_chars = {"n": 0}
        synth_done = threading.Event()

        def _synthesis_ticker():
            while not synth_done.wait(10):
                mins, secs = divmod(int(time.time() - synth_start), 60)
                if synth_chars["n"]:
                    progress = (f"🌌 Writing the Cosmic Macro report — {synth_chars['n']:,} characters "
                                f"so far ({mins}m {secs:02d}s)")
                else:
                    progress = (f"🌌 Reasoning across the knowledge base — {mins}m {secs:02d}s elapsed. "
                                f"No output yet; this phase alone usually runs 6-8 minutes.")
                update_agent_job(job_id, {"progress": progress})

        def _synthesis_progress(elapsed_seconds, chars_generated):
            synth_chars["n"] = chars_generated

        threading.Thread(target=_synthesis_ticker, daemon=True,
                         name="cosmic-synthesis-ticker").start()

        # temperature 1 for bold crystal ball predictions.
        # use_streaming=True keeps the connection active so Azure's SNAT ~4-min idle
        # timeout doesn't silently drop this long-running call (it otherwise hangs
        # forever on Azure while working fine on localhost).
        try:
            analysis_result = call_openai_api_fn(
                messages,
                model=COSMIC_SYNTHESIS_MODEL,
                temperature=1,
                timeout=900,  # 15 minutes — see the headroom note above; 420s was marginal
                use_streaming=True,
                reasoning_effort=COSMIC_REASONING_EFFORT,
                progress_callback=_synthesis_progress,
            )
        finally:
            # Stop the ticker on the error path too, so a failed synthesis doesn't leave a
            # thread overwriting the error status with stale progress text.
            synth_done.set()

        elapsed_total = int(time.time() - start_time)
        print(f"COSMIC_AGENT: Step 4 done — {len(analysis_result)} chars in {elapsed_total}s total", file=sys.stderr)

        # ── Parse JSON response ─────────────────────────────────────
        structured_data = None
        raw_analysis = analysis_result

        clean = _extract_json_object(analysis_result)

        try:
            # strict=False tolerates literal control chars (newlines/tabs) inside
            # string values — a very common reason LLM JSON fails strict parsing.
            structured_data = json.loads(clean, strict=False)
            print(f"COSMIC_AGENT: ✓ JSON parsed successfully — {len(structured_data)} top-level keys", file=sys.stderr)
        except (json.JSONDecodeError, TypeError) as jde:
            # Log head/tail + length so we can tell a truncated response (tail not '}')
            # apart from a formatting issue, without dumping the whole payload.
            full = analysis_result or ""
            pos = getattr(jde, "pos", -1)
            head = full[:300].replace("\n", "\\n")
            tail = full[-300:].replace("\n", "\\n")
            print(f"COSMIC_AGENT: ⚠ JSON parse failed ({jde}) at pos {pos}; "
                  f"raw_len={len(full)}. HEAD={head!r} TAIL={tail!r}. Attempting repair.",
                  file=sys.stderr)
            structured_data = None

            # A truncated response still carries almost the whole report, and the raw-text
            # fallback renders it as an unreadable wall of JSON. Close the dangling containers
            # and keep everything that arrived complete rather than losing the lot.
            repaired = _repair_truncated_json(clean)
            if repaired:
                try:
                    structured_data = json.loads(repaired, strict=False)
                    print("COSMIC_AGENT: recovered truncated JSON - %d top-level keys "
                          "from a %d-char truncated response"
                          % (len(structured_data), len(clean)), file=sys.stderr)
                except (json.JSONDecodeError, TypeError) as rde:
                    print("COSMIC_AGENT: truncation repair did not parse either (%s); "
                          "falling back to raw text" % rde, file=sys.stderr)

        # Shadow comparison: how much of what the model just asserted was actually correct?
        # Logs a one-line scorecard per run; never raises.
        engine_comparison = cosmic_engine.log_shadow_comparison(engine_state, structured_data)

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
            "model_used": COSMIC_SYNTHESIS_MODEL,
            "reasoning_effort": COSMIC_REASONING_EFFORT,
            # Shadow-mode only; nothing in the UI reads these. They persist the comparison
            # corpus and let chat reuse the computed state after the Deploy 2 cutover.
            "engine_mode": cosmic_engine.ENGINE_MODE,
            "engine_state": engine_state,
            "engine_comparison": engine_comparison,
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
                model=COSMIC_SYNTHESIS_MODEL,
                temperature=1,
                timeout=180,
                reasoning_effort=COSMIC_REASONING_EFFORT,
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
