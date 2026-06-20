"""
technical_agent.py — Technical Agent for the Kagger AI Agent Marketplace.

For a user-entered NSE ticker, this agent orchestrates the existing technical-analysis
engine and the three Advanced Screeners, renders every chart, and feeds the structured
text context + high-signal chart images to GPT-5.4 (reasoning_effort=high) to produce a decisive,
confluence-driven outlook across short (1-3m), medium (3-6m) and long (>6m) horizons.

Pipeline (background thread, polled — mirrors cosmic_agent.py):
  Stage A  evaluate_ticker_signal on 1Y/daily and 5Y/weekly (concurrent) + per-chart summaries
  Stage B  fetch 10y data once, run Momentum / Divergence / Wyckoff screeners for the ticker
  Stage C  build all Plotly figures -> .to_json() (frontend); rasterize high-signal subset to PNG (vision)
  Stage D  assemble the multimodal message (text feeds for all charts + labeled images for the subset)
  Stage E  call GPT-5.4 @ high (3-tier fallback: images+effort -> text+effort -> text plain)
  Stage F  parse JSON, store envelope, mark job complete

Reuses (no changes to those modules):
  calculations.tech_calculations : evaluate_ticker_signal, generate_summary,
      generate_per_chart_summaries, build_{close,hl,ema,rsi,adl,rs,rsi_divergence}_figure
  calculations.screener_calculations : fetch_unified_market_data, run_momentum_screener,
      run_divergence_screener, run_wyckoff_screener, build_wyckoff_plotly_figure
  agents.base : job + latest-result helpers (Redis path json-encodes, so the stored
                envelope is kept strictly JSON-serializable).
"""

import sys
import time
import threading
import traceback
import json
import re
import base64
import datetime
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from flask import request, jsonify

from agents.base import (
    create_agent_job, update_agent_job, get_agent_job,
    store_latest_result, get_latest_result,
)
from agents.prompts.technical_prompts import (
    TECHNICAL_SYSTEM_PROMPT, TECHNICAL_CHAT_PROMPT,
)

# Engine + screeners are pure functions in calculations/ (already imported by handler.py
# elsewhere, so these imports are proven-safe at startup).
from calculations.tech_calculations import (
    evaluate_ticker_signal, generate_summary, generate_per_chart_summaries,
    build_close_figure, build_hl_figure, build_ema_figure, build_rsi_figure,
    build_adl_figure, build_rs_figure, build_rsi_divergence_figure,
)
from calculations.screener_calculations import (
    fetch_unified_market_data, run_momentum_screener, run_divergence_screener,
    run_wyckoff_screener, build_wyckoff_plotly_figure,
)
import yfinance as yf


# =====================================================================
# CONSTANTS
# =====================================================================
AGENT_TYPE = "technical"
MODEL = "gpt-5.4"
REASONING_EFFORT = "high"   # 'high' is near-identical to 'xhigh' on this structured task but markedly faster
LLM_TIMEOUT = 600           # multimodal synthesis can still be long
CHAT_TIMEOUT = 180
TECHNICAL_CACHE_TTL_HOURS = 12
PNG_WIDTH, PNG_HEIGHT, PNG_SCALE = 1000, 560, 1

# (suffix shown after the timeframe, figure builder, per-chart-summary key)
_CHART_SPECS = [
    ("Close & SMA20",            build_close_figure,          "close"),
    ("High/Low Swing Structure", build_hl_figure,             "hl"),
    ("EMA Stack",                build_ema_figure,            "ema"),
    ("RSI",                      build_rsi_figure,            "rsi"),
    ("ADL Accumulation",         build_adl_figure,            "adl"),
    ("RS vs Nifty",              build_rs_figure,             "rs"),
    ("RSI Divergence",           build_rsi_divergence_figure, "rsi_div"),
]

# Suffixes of the charts sent to the model as IMAGES (the visual pass). Every other chart
# still reaches the model as a precise text reading and still renders in the UI — these are
# just the highest-signal ones to look at, which keeps the multimodal payload (and the
# PNG-rasterization + reasoning time) down without losing analytical coverage. Passed-screener
# charts are always included on top of these.
_VISION_SUFFIXES = {"Close & SMA20", "RSI", "ADL Accumulation", "RSI Divergence"}

MOMENTUM_CRITERIA = ("Liquidity: 20- & 50-day average traded value > Rs.5 cr; 20-SMA not below 50-SMA for 20 "
                     "straight days; close above both 10- & 20-SMA; 10-SMA > 20-SMA; up day vs prior close; "
                     "ATR(1) > 60% of ATR(20); close in the upper 60% of the day's range. "
                     "PASS = a fresh short-term momentum breakout is active right now.")
DIVERGENCE_CRITERIA = ("Hidden bullish divergence over a 2-year window: price prints a lower low while RSI(14) "
                       "prints a higher low (swing analysis + ADL accumulation), fired within the last 2 trading "
                       "days. PASS = an early bottoming / reversal signal just triggered.")
WYCKOFF_CRITERIA = ("Multi-year accumulation structure on SMA100: Selling Climax -> Automatic Rally -> Spring/Test "
                    "(Phase C) -> Sign of Strength -> Last Point of Support; buy triggers LPS / SOS2 / JAC. "
                    "PASS = the stock is in a recognizable institutional accumulation base with an active phase.")


# =====================================================================
# HELPERS
# =====================================================================

def _extract_json_object(text):
    """Best-effort extraction of one JSON object from an LLM response (fences/prose tolerant)."""
    if not text:
        return ""
    s = text.strip()
    if "```" in s:
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.DOTALL)
        if m and m.group(1).strip():
            s = m.group(1).strip()
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start:end + 1]
    return s


def _json_safe(obj):
    """Guarantee the stored/returned envelope is JSON-serializable (strips stray numpy etc.)."""
    return json.loads(json.dumps(obj, default=str))


def _strip_html(s):
    """Per-chart summaries embed <strong>/<br>; flatten to clean text for the LLM."""
    if not s:
        return ""
    s = s.replace("<br>", " ").replace("</br>", " ")
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _fmt_table(summary_rows):
    if not summary_rows:
        return "(no summary available)"
    return "\n".join(f"- {r.get('key', '')}: {r.get('value', '')}" for r in summary_rows)


# --- kaleido (server-side Plotly -> PNG) probe + export ---------------
_KALEIDO_OK = None


def _kaleido_available():
    global _KALEIDO_OK
    if _KALEIDO_OK is not None:
        return _KALEIDO_OK
    try:
        import plotly.graph_objects as go
        go.Figure().to_image(format="png", width=10, height=10)
        _KALEIDO_OK = True
    except Exception as e:
        print(f"TECHNICAL_AGENT: kaleido/PNG export unavailable ({e}); running text-only.", file=sys.stderr)
        _KALEIDO_OK = False
    return _KALEIDO_OK


def figure_to_png_b64(fig):
    """Rasterize a Plotly Figure to a base64 PNG data-URI for vision. Returns None on any failure."""
    if not _kaleido_available():
        return None
    try:
        png = fig.to_image(format="png", width=PNG_WIDTH, height=PNG_HEIGHT, scale=PNG_SCALE)
        return f"data:image/png;base64,{base64.b64encode(png).decode('ascii')}"
    except Exception as e:
        print(f"TECHNICAL_AGENT: figure_to_png_b64 failed: {e}", file=sys.stderr)
        return None


def _resolve_company_name(ticker):
    try:
        info = yf.Ticker(f"{ticker}.NS").info
        return info.get("longName") or info.get("shortName") or ticker
    except Exception:
        return ticker


def _render_timeframe(ticker, company_name, interval, years, tf_label):
    """Run the engine for one timeframe and build its 7 figures + summaries.
    Returns None when the timeframe has no/insufficient data."""
    res = evaluate_ticker_signal(ticker, interval=interval, years=years)
    signal = res.get("Signal", "")
    df = res.get("Data")
    if signal in ("NO DATA", "INSUFFICIENT DATA") or df is None or getattr(df, "empty", True):
        return {"ok": False, "signal": signal or "NO DATA"}

    try:
        summary = generate_summary(res)
    except Exception as e:
        print(f"TECHNICAL_AGENT: generate_summary failed ({tf_label}): {e}", file=sys.stderr)
        summary = []
    try:
        per_chart = generate_per_chart_summaries(res)
    except Exception as e:
        print(f"TECHNICAL_AGENT: per-chart summaries failed ({tf_label}): {e}", file=sys.stderr)
        per_chart = {}

    charts = {}  # canonical label -> {"fig": fig, "summary": text}
    for suffix, builder, sumkey in _CHART_SPECS:
        label = f"{tf_label} — {suffix}"
        try:
            fig = builder(df, company_name, years=years)
            charts[label] = {"fig": fig, "summary": _strip_html(per_chart.get(sumkey, ""))}
        except Exception as e:
            print(f"TECHNICAL_AGENT: chart build failed for '{label}': {e}", file=sys.stderr)
    return {"ok": True, "signal": signal, "summary": summary, "charts": charts}


def _build_screener_close_fig(df_raw, company_name):
    """Replicates the screener_background close-chart construction (handles MultiIndex columns)."""
    df_chart = df_raw.copy()
    if isinstance(df_chart["Close"], pd.DataFrame):
        df_chart["Close"] = df_chart["Close"].iloc[:, 0]
        df_chart["High"] = df_chart["High"].iloc[:, 0]
        df_chart["Low"] = df_chart["Low"].iloc[:, 0]
        df_chart["Volume"] = df_chart["Volume"].iloc[:, 0]
    df_chart["SMA20"] = df_chart["Close"].rolling(window=20).mean()
    return build_close_figure(df_chart.dropna(), company_name, years=1)


def _run_screeners(ticker, company_name):
    """Evaluate all three screeners for one ticker. Never raises — failures are recorded."""
    out = {
        "momentum": {"passed": None},
        "divergence": {"passed": None},
        "wyckoff": {"passed": None},
        "charts": {},   # label -> fig
        "errors": {},
    }
    try:
        data = fetch_unified_market_data([ticker], period="10y")
    except Exception as e:
        out["errors"]["fetch"] = str(e)
        print(f"TECHNICAL_AGENT: screener data fetch failed: {e}", file=sys.stderr)
        return out

    df = data.get(ticker)
    if df is None or getattr(df, "empty", True):
        out["errors"]["fetch"] = "no price history returned for screeners"
        return out

    # Momentum
    try:
        matched = run_momentum_screener([ticker], preloaded_data=data)
        passed = ticker in matched
        out["momentum"] = {"passed": passed}
        if passed:
            try:
                out["charts"]["Momentum Breakout — Chart"] = _build_screener_close_fig(df, company_name)
            except Exception as e:
                print(f"TECHNICAL_AGENT: momentum chart failed: {e}", file=sys.stderr)
    except Exception as e:
        out["errors"]["momentum"] = str(e)
        print(f"TECHNICAL_AGENT: momentum screener failed: {e}", file=sys.stderr)

    # Divergence
    try:
        matched = run_divergence_screener([ticker], preloaded_data=data)
        passed = ticker in matched
        out["divergence"] = {"passed": passed}
        if passed:
            try:
                out["charts"]["Divergence Bottom — Chart"] = _build_screener_close_fig(df, company_name)
            except Exception as e:
                print(f"TECHNICAL_AGENT: divergence chart failed: {e}", file=sys.stderr)
    except Exception as e:
        out["errors"]["divergence"] = str(e)
        print(f"TECHNICAL_AGENT: divergence screener failed: {e}", file=sys.stderr)

    # Wyckoff
    try:
        wy = run_wyckoff_screener([ticker], preloaded_data=data)
        if wy:
            m = wy[0]
            out["wyckoff"] = {"passed": True, "reason": m.get("reason", "")}
            try:
                out["charts"]["Wyckoff Institutional Base — Chart"] = build_wyckoff_plotly_figure(
                    m["df_6y"], m["waves"], m["setup"], ticker)
            except Exception as e:
                print(f"TECHNICAL_AGENT: wyckoff chart failed: {e}", file=sys.stderr)
        else:
            out["wyckoff"] = {"passed": False}
    except Exception as e:
        out["errors"]["wyckoff"] = str(e)
        print(f"TECHNICAL_AGENT: wyckoff screener failed: {e}", file=sys.stderr)

    return out


def _verdict_word(passed):
    if passed is True:
        return "PASS"
    if passed is False:
        return "FAIL (criteria not currently met)"
    return "UNAVAILABLE (screener error / insufficient data)"


# =====================================================================
# BACKGROUND PIPELINE
# =====================================================================

def _run_technical_analysis(job_id, ticker, call_openai_api_fn, call_gemini_api_fn):
    start_time = time.time()
    try:
        company_name = _resolve_company_name(ticker)

        # ── Stage A + B: engine (1Y, 5Y) and screeners, concurrently ──
        update_agent_job(job_id, {"progress": "📈 Computing indicators across 1Y daily & 5Y weekly + running screeners..."})
        with ThreadPoolExecutor(max_workers=4) as ex:
            # Warm kaleido's headless-Chromium up front so its cold-start hides behind the
            # data fetch instead of stalling Stage C. (Figure building here never touches
            # kaleido, and rasterization runs only after this block, so no concurrency on it.)
            fut_warm = ex.submit(_kaleido_available)
            fut_1y = ex.submit(_render_timeframe, ticker, company_name, "daily", 1, "1Y Daily")
            fut_5y = ex.submit(_render_timeframe, ticker, company_name, "weekly", 5, "5Y Weekly")
            fut_scr = ex.submit(_run_screeners, ticker, company_name)
            tf1 = fut_1y.result()
            tf5 = fut_5y.result()
            scr = fut_scr.result()
            try:
                fut_warm.result()   # ensure the probe is cached before Stage C (already done by now)
            except Exception:
                pass

        if not tf1.get("ok") and not tf5.get("ok"):
            raise ValueError(f"No price data found for {ticker} on either timeframe "
                             f"(1Y={tf1.get('signal')}, 5Y={tf5.get('signal')}). Check the ticker symbol.")

        # ── Stage C: render PNGs (model) + collect Plotly JSON (frontend) ──
        update_agent_job(job_id, {"progress": "🎨 Rendering charts..."})
        ordered = []  # (label, fig, summary_text)
        for tf in (tf5, tf1):  # weekly first, then daily
            if tf and tf.get("ok"):
                for label, c in tf["charts"].items():
                    ordered.append((label, c["fig"], c["summary"]))
        for label, fig in scr.get("charts", {}).items():
            ordered.append((label, fig, ""))

        # Only the highest-signal charts are rasterized for vision; the rest reach the model
        # as text readings. charts_json is still built for ALL charts, so the UI is unchanged.
        vision_labels = set(scr.get("charts", {}).keys())   # every passed-screener chart
        for tf in (tf5, tf1):
            if tf and tf.get("ok"):
                for label in tf["charts"]:
                    if label.split("—")[-1].strip() in _VISION_SUFFIXES:
                        vision_labels.add(label)

        charts_json = {}
        pngs = {}
        for label, fig, _ in ordered:
            try:
                charts_json[label] = fig.to_json()
            except Exception as e:
                print(f"TECHNICAL_AGENT: to_json failed for '{label}': {e}", file=sys.stderr)
            if label in vision_labels:
                png = figure_to_png_b64(fig)
                if png:
                    pngs[label] = png
        had_images = len(pngs) > 0

        # ── Stage D: assemble the multimodal message + text context ──
        update_agent_job(job_id, {"progress": "🧠 Assembling multimodal context for GPT-5.4..."})
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        sig_1y = tf1.get("signal", "N/A")
        sig_5y = tf5.get("signal", "N/A")

        content = []        # OpenAI vision content blocks
        text_parts = []     # plain-text mirror (for chat + envelope)

        def _add_text(t):
            content.append({"type": "text", "text": t})
            text_parts.append(t)

        img_note = ("Chart images are attached for the highest-signal charts (Close/SMA, RSI, ADL, RSI-Divergence, "
                    "plus any passed screener) — read those visually and do show-and-tell. Every other chart is "
                    "provided as a precise engine text reading below; treat those readings as authoritative."
                    if had_images else
                    "NOTE: chart images are NOT available this run — reason purely from the detailed text readings below.")
        _add_text(
            f"## TECHNICAL DOSSIER — {ticker} ({company_name}) — as of {today}\n"
            f"Engine signals: 1Y Daily = {sig_1y} · 5Y Weekly = {sig_5y}\n"
            f"Order: 5Y WEEKLY readings, then 1Y DAILY readings, then the three screener verdicts. "
            f"Each chart's plain-language reading precedes its image.\n{img_note}"
        )

        for tf, tf_name in ((tf5, "5Y WEEKLY"), (tf1, "1Y DAILY")):
            if not (tf and tf.get("ok")):
                _add_text(f"### {tf_name}: data unavailable for this timeframe ({tf.get('signal', 'N/A')}).")
                continue
            _add_text(f"### {tf_name} — SUMMARY TABLE (engine signal: {tf['signal']})\n{_fmt_table(tf['summary'])}")
            for label, c in tf["charts"].items():
                _add_text(f"[CHART: {label}]\n{c['summary'] or '(no text reading)'}")
                if had_images and pngs.get(label):
                    content.append({"type": "image_url", "image_url": {"url": pngs[label]}})

        # Screeners
        wy_reason = scr.get("wyckoff", {}).get("reason", "")
        screener_text = (
            "### ADVANCED SCREENER VERDICTS\n"
            f"MOMENTUM BREAKOUT (short-term trigger) — {_verdict_word(scr['momentum'].get('passed'))}\n"
            f"  Criteria: {MOMENTUM_CRITERIA}\n"
            f"DIVERGENCE BOTTOM (early reversal) — {_verdict_word(scr['divergence'].get('passed'))}\n"
            f"  Criteria: {DIVERGENCE_CRITERIA}\n"
            f"WYCKOFF INSTITUTIONAL BASE (long-term context) — {_verdict_word(scr['wyckoff'].get('passed'))}\n"
            f"  Active phase / reason: {wy_reason or 'n/a'}\n"
            f"  Criteria: {WYCKOFF_CRITERIA}"
        )
        _add_text(screener_text)
        for label in ("Momentum Breakout — Chart", "Divergence Bottom — Chart", "Wyckoff Institutional Base — Chart"):
            if label in scr.get("charts", {}):
                _add_text(f"[CHART: {label}]")
                if had_images and pngs.get(label):
                    content.append({"type": "image_url", "image_url": {"url": pngs[label]}})

        available_labels = list(charts_json.keys())
        _add_text(
            "## YOUR TASK\n"
            "Run the full confluence methodology. Reconcile the 5Y weekly regime against the 1Y daily posture "
            "explicitly. Map Momentum→short, Divergence→short/medium, Wyckoff→long. Derive each horizon's "
            "conviction from agreement across independent witnesses. Cite specific Rs. levels and reference each "
            "chart by its exact [CHART: ...] label. Return ONLY the JSON object per your schema.\n\n"
            "Available chart labels this run (use these verbatim):\n- " + "\n- ".join(available_labels)
        )

        text_context = "\n\n".join(text_parts)

        # ── Stage E: GPT-5.4 @ high, with graceful fallback ladder ──
        update_agent_job(job_id, {"progress": f"🧠 GPT-5.4 ({REASONING_EFFORT}) reasoning over the charts..."})
        messages_full = [
            {"role": "system", "content": TECHNICAL_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]
        messages_text = [
            {"role": "system", "content": TECHNICAL_SYSTEM_PROMPT},
            {"role": "user", "content": text_context},
        ]

        # (use_images, use_effort)
        attempts = [(True, True), (False, True), (False, False)]
        if not had_images:
            attempts = [(False, True), (False, False)]

        raw = None
        used_images = False
        used_effort = False
        last_err = None
        for use_images, use_effort in attempts:
            kwargs = dict(model=MODEL, temperature=1, use_streaming=True, timeout=LLM_TIMEOUT)
            if use_effort:
                kwargs["reasoning_effort"] = REASONING_EFFORT
            msgs = messages_full if use_images else messages_text
            try:
                raw = call_openai_api_fn(msgs, **kwargs)
                used_images = use_images
                used_effort = use_effort
                break
            except Exception as e:
                last_err = e
                print(f"TECHNICAL_AGENT: LLM attempt (images={use_images}, effort={use_effort}) failed: {e}",
                      file=sys.stderr)
                continue
        if raw is None:
            raise RuntimeError(f"GPT-5.4 synthesis failed after fallbacks: {last_err}")

        # ── Parse JSON ──
        structured = None
        clean = _extract_json_object(raw)
        try:
            structured = json.loads(clean, strict=False)
        except (json.JSONDecodeError, TypeError) as jde:
            print(f"TECHNICAL_AGENT: JSON parse failed ({jde}); keeping raw text fallback.", file=sys.stderr)
            structured = None

        elapsed_total = int(time.time() - start_time)
        model_used = f"gpt-5.4 ({REASONING_EFFORT})" if used_effort else "gpt-5.4"
        if not used_images:
            model_used += " · text-only"

        # ── Stage F: store envelope (strictly JSON-serializable) ──
        result_data = _json_safe({
            "ticker": ticker,
            "company_name": company_name,
            "structured": structured,
            "analysis": raw,
            "charts": charts_json,              # canonical label -> Plotly JSON string
            "text_context": text_context[:120000],
            "engine_signals": {"daily_1y": sig_1y, "weekly_5y": sig_5y},
            "screeners_raw": {
                "momentum": scr.get("momentum"),
                "divergence": scr.get("divergence"),
                "wyckoff": scr.get("wyckoff"),
                "errors": scr.get("errors"),
            },
            "had_images": had_images and used_images,
            "model_used": model_used,
            "analyzed_at": time.time(),
            "analysis_time_seconds": elapsed_total,
        })

        try:
            store_latest_result(AGENT_TYPE, ticker, result_data)
        except Exception as e:
            print(f"TECHNICAL_AGENT: store_latest_result failed (continuing): {e}", file=sys.stderr)

        update_agent_job(job_id, {
            "status": "complete",
            "progress": "Analysis complete!",
            "result": result_data,
            "completed_at": time.time(),
            "total_time": elapsed_total,
        })
        print(f"TECHNICAL_AGENT: ✓ {ticker} complete in {elapsed_total}s "
              f"(images={had_images and used_images}, effort={used_effort})", file=sys.stderr)

    except Exception as e:
        print(f"TECHNICAL_AGENT ERROR: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        update_agent_job(job_id, {
            "status": "error",
            "error": f"Technical analysis failed: {str(e)}",
        })


# =====================================================================
# FLASK ROUTE REGISTRATION
# =====================================================================

def register_technical_routes(app, call_openai_api_fn, call_gemini_api_fn=None):
    """Register the Technical Agent routes.

    Args:
        app: Flask app instance
        call_openai_api_fn: handler.call_openai_api (must accept reasoning_effort)
        call_gemini_api_fn: optional; reserved for future multimodal fallback (unused today)
    """

    @app.route("/agent/technical/analyze", methods=["POST"])
    def agent_technical_analyze():
        try:
            data = request.get_json(force=True)
            ticker = (data.get("ticker", "") or "").strip().upper()
            force_refresh = data.get("force_refresh", False)
            if not ticker:
                return jsonify({"error": "Ticker is required"}), 400

            if not force_refresh:
                existing = get_latest_result(AGENT_TYPE, ticker)
                if existing:
                    age_hours = (time.time() - existing["stored_at"]) / 3600
                    if age_hours < TECHNICAL_CACHE_TTL_HOURS:
                        return jsonify({
                            "status": "complete",
                            "result": existing["result"],
                            "cached": True,
                            "age_minutes": int(age_hours * 60),
                        })

            job_id = create_agent_job(AGENT_TYPE, ticker)
            thread = threading.Thread(
                target=_run_technical_analysis,
                args=(job_id, ticker, call_openai_api_fn, call_gemini_api_fn),
            )
            thread.daemon = True
            thread.start()

            return jsonify({
                "job_id": job_id,
                "status": "processing",
                "message": f"Technical analysis started for {ticker}",
            })
        except Exception as e:
            print(f"TECHNICAL_AGENT ERROR (analyze): {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500

    @app.route("/agent/technical/<job_id>/status", methods=["GET"])
    def agent_technical_status(job_id):
        try:
            job = get_agent_job(job_id)
            if not job:
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
            print(f"TECHNICAL_AGENT_STATUS ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({"status": "error", "error": f"Status check failed: {str(e)}"}), 500

    @app.route("/agent/technical/chat", methods=["POST"])
    def agent_technical_chat():
        try:
            data = request.get_json(force=True)
            question = (data.get("question", "") or "").strip()
            analysis_context = (data.get("analysis_context", "") or "").strip()
            text_context = (data.get("text_context", "") or "").strip()
            if not question:
                return jsonify({"error": "Question is required"}), 400
            if not analysis_context and not text_context:
                return jsonify({"error": "Analysis context is required. Generate the report first."}), 400

            user_msg = (
                f"TECHNICAL REPORT (JSON):\n{analysis_context[:40000]}\n\n"
                f"SUPPORTING ENGINE & SCREENER READINGS:\n{text_context[:40000]}\n\n"
                f"QUESTION: {question}"
            )
            messages = [
                {"role": "system", "content": TECHNICAL_CHAT_PROMPT},
                {"role": "user", "content": user_msg},
            ]
            try:
                answer = call_openai_api_fn(messages, model=MODEL, temperature=1,
                                            reasoning_effort="high", timeout=CHAT_TIMEOUT)
            except Exception as e:
                print(f"TECHNICAL_AGENT_CHAT: retry without reasoning_effort ({e})", file=sys.stderr)
                answer = call_openai_api_fn(messages, model=MODEL, temperature=1, timeout=CHAT_TIMEOUT)

            return jsonify({"answer": answer, "status": "success"})
        except Exception as e:
            print(f"TECHNICAL_AGENT_CHAT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500

    @app.route("/agent/technical/latest", methods=["GET"])
    def agent_technical_latest():
        ticker = (request.args.get("ticker", "") or "").strip().upper()
        if not ticker:
            return jsonify({"error": "Ticker required"}), 400
        result = get_latest_result(AGENT_TYPE, ticker)
        if result:
            return jsonify({
                "status": "complete",
                "result": result["result"],
                "age_minutes": round((time.time() - result["stored_at"]) / 60),
            })
        return jsonify({"status": "none", "message": "No cached result found"})
