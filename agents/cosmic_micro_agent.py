"""
cosmic_micro_agent.py — Cosmic Micro Analyst Agent for the Kagger AI Agent Marketplace.

Fuses deterministic Swiss Ephemeris data, fundamental financials, and the full
astrological rulebook (Sections 1-30) to produce per-ticker or per-industry cosmic forecasts.
"""

import sys
import os
import time
import threading
import traceback
import json
import csv
import datetime
from flask import request, jsonify

from agents.utils.ephemeris import generate_cosmic_data_report
from agents.base import (
    create_agent_job, update_agent_job, get_agent_job,
    store_latest_result, get_latest_result,
)
from agents.prompts.cosmic_prompts import (
    COSMIC_MICRO_SYNTHESIS_PROMPT, COSMIC_MICRO_CHAT_PROMPT,
    COSMIC_ASTRO_FRAMEWORK, COSMIC_PDF_AUGMENTATION_TEXT,
)

# =====================================================================
# CSV LAZY LOADING & LOOKUP
# =====================================================================
_TRENDLYNE_CACHE = {}
_TRENDLYNE_INDUSTRIES = {}
_CSV_LOAD_LOCK = threading.Lock()

def _load_trendlyne_csv():
    global _TRENDLYNE_CACHE, _TRENDLYNE_INDUSTRIES
    if _TRENDLYNE_CACHE:
        return
    
    with _CSV_LOAD_LOCK:
        if _TRENDLYNE_CACHE:
            return
            
        paths_to_try = [
            "trendlyne_all_stocks_master.csv",
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trendlyne_all_stocks_master.csv")
        ]
        
        csv_path = None
        for p in paths_to_try:
            if os.path.exists(p):
                csv_path = p
                break
                
        if not csv_path:
            print("ERROR: trendlyne_all_stocks_master.csv not found", file=sys.stderr)
            return
            
        try:
            with open(csv_path, mode="r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    ticker = (row.get("Ticker") or "").strip().upper()
                    if ticker:
                        stock_info = {
                            "stock_name": (row.get("Stock Name") or "").strip(),
                            "ticker": ticker,
                            "industry": (row.get("Industry Name") or "").strip(),
                            "sector": (row.get("Sector Name") or "").strip()
                        }
                        _TRENDLYNE_CACHE[ticker] = stock_info
                        
                        ind = stock_info["industry"]
                        if ind:
                            if ind not in _TRENDLYNE_INDUSTRIES:
                                _TRENDLYNE_INDUSTRIES[ind] = []
                            _TRENDLYNE_INDUSTRIES[ind].append(stock_info)
            print(f"COSMIC_MICRO_AGENT: Loaded {len(_TRENDLYNE_CACHE)} stocks from CSV", file=sys.stderr)
        except Exception as e:
            print(f"ERROR reading trendlyne_all_stocks_master.csv: {e}", file=sys.stderr)


def _lookup_trendlyne_sector(ticker: str) -> dict:
    """Reads trendlyne_all_stocks_master.csv and returns {stock_name, ticker, industry, sector}."""
    _load_trendlyne_csv()
    tick_upper = ticker.upper().strip()
    return _TRENDLYNE_CACHE.get(tick_upper, {
        "stock_name": "",
        "ticker": ticker,
        "industry": "",
        "sector": ""
    })


def _lookup_trendlyne_industry_stocks(industry: str) -> list:
    """Returns list of stocks in the specified industry."""
    _load_trendlyne_csv()
    return _TRENDLYNE_INDUSTRIES.get(industry, [])


# =====================================================================
# SECTOR -> AGGREGATE INDEX MAPPING (for industry price-tape reconciliation)
# =====================================================================
# Maps each trendlyne Sector Name to the best-fit Nifty index in
# market_data._MACRO_UNIVERSE, so an industry/sector forecast can be reconciled
# against an aggregate sectoral price tape (the analog of a single stock's own tape).
_SECTOR_TO_INDEX = {
    "Banking and Finance":              "Nifty Financial Services",
    "General Industrials":              "Nifty Infrastructure",
    "Software & Services":              "Nifty IT",
    "Pharmaceuticals & Biotechnology":  "Nifty Pharma",
    "Textiles Apparels & Accessories":  "Nifty India Consumption",
    "Commercial Services & Supplies":   "Nifty Services",
    "Chemicals & Petrochemicals":       "Nifty Commodities",
    "Cement and Construction":          "Nifty Infrastructure",
    "Automobiles & Auto Components":    "Nifty Auto",
    "Metals & Mining":                  "Nifty Metal",
    "Food, Beverages & Tobacco":        "Nifty FMCG",
    "Realty":                           "Nifty Realty",
    "Consumer Durables":                "Nifty Consumer Durables",
    "Utilities":                        "Nifty Energy",
    "Transportation":                   "Nifty Infrastructure",
    "Healthcare":                       "Nifty Healthcare",
    "Retailing":                        "Nifty India Consumption",
    "FMCG":                             "Nifty FMCG",
    "Hotels Restaurants & Tourism":     "Nifty India Consumption",
    "Diversified Consumer Services":    "Nifty India Consumption",
    "Media":                            "Nifty Media",
    "Oil & Gas":                        "Nifty Oil & Gas",
    "Diversified":                      "Nifty 500",
    "Fertilizers":                      "Nifty Commodities",
    "Telecommunications Equipment":     "Nifty Services",
    "Forest Materials":                 "Nifty Commodities",
    "Telecom Services":                 "Nifty Services",
    "Hardware Technology & Equipment":  "Nifty IT",
    "Others":                           "Nifty 500",
}

# Finer industry-level overrides (checked BEFORE the sector map). Substring match,
# lowercased, on the industry name. Order matters — first match wins.
_INDUSTRY_INDEX_OVERRIDES = [
    ("aerospace & defence", "Nifty India Defence"),
    ("defence",             "Nifty India Defence"),
    ("defense",             "Nifty India Defence"),
    ("banks",               "Nifty Bank"),  # pure-bank industry; NBFC/insurance stay Financial Services
]


def _resolve_sector_index(industry: str, sector: str) -> str:
    """
    Pick the best-fit aggregate index display name (a key of market_data._MACRO_UNIVERSE)
    for an industry/sector. Tries the finer industry overrides first, then the sector map,
    then falls back to the broad market (Nifty 500) so a tape is always available.
    """
    ind_l = (industry or "").strip().lower()
    for needle, idx in _INDUSTRY_INDEX_OVERRIDES:
        if needle in ind_l:
            return idx
    if sector:
        hit = _SECTOR_TO_INDEX.get(sector.strip())
        if hit:
            return hit
    return "Nifty 500"


# =====================================================================
# BACKGROUND ANALYSIS JOB
# =====================================================================
def _run_cosmic_micro_analysis(
    job_id,
    ticker,
    industry,
    call_openai_api_fn,
    get_cache_fn,
    get_full_analysis_fn,
):
    try:
        if ticker:
            ticker_upper = ticker.upper().strip()
            update_agent_job(job_id, {"progress": f"🔍 Fetching financial data for {ticker_upper}..."})
            
            # Fetch from cache first
            cached_data = get_cache_fn(ticker_upper)
            if not cached_data:
                update_agent_job(job_id, {"progress": "⏳ Cache miss. Fetching fresh financial data..."})
                _, cached_data = get_full_analysis_fn(ticker_upper, skip_ai_summary=True)
                
            if not cached_data or isinstance(cached_data, int):
                if isinstance(cached_data, dict) and "error" in cached_data:
                    error_msg = cached_data["error"]
                else:
                    error_msg = f"Could not retrieve financial data for {ticker_upper}. Please analyze it in Company Research first."
                raise ValueError(error_msg)
                
            company_name = cached_data.get("company_name", ticker_upper)
            key_metrics = dict(cached_data.get("key_metrics", {}))
            fundamentals = cached_data.get("fundamentals", {})
            company_description = cached_data.get("company_description", "")
            
            # Fetch current market price (CMP) + multi-session price trend using yfinance
            import yfinance as yf
            from agents.utils.market_data import _trend_metrics, _format_trend
            live_price = None
            price_trend_text = None
            try:
                for suffix in [".NS", ".BO", ""]:
                    symbol = f"{ticker_upper}{suffix}" if suffix else ticker_upper
                    if suffix == "" and "." not in ticker_upper:
                        continue
                    t = yf.Ticker(symbol)
                    hist = t.history(period="3mo")
                    if not hist.empty:
                        live_price = hist['Close'].iloc[-1]
                        try:
                            metrics = _trend_metrics(hist['Close'])
                            if metrics:
                                price_trend_text = _format_trend(metrics)
                                print(f"COSMIC_MICRO_AGENT: {symbol} price trend: {price_trend_text}", file=sys.stderr)
                        except Exception as te:
                            print(f"WARNING: trend metrics failed for {symbol}: {te}", file=sys.stderr)
                        print(f"COSMIC_MICRO_AGENT: Fetched yfinance price for {symbol} via history: ₹{live_price:.2f}", file=sys.stderr)
                        break
                    else:
                        info = t.info
                        live_price = info.get('currentPrice') or info.get('regularMarketPrice')
                        if live_price:
                            print(f"COSMIC_MICRO_AGENT: Fetched yfinance price for {symbol} via info: ₹{live_price:.2f}", file=sys.stderr)
                            break
            except Exception as e:
                print(f"WARNING: Failed to fetch yfinance price for {ticker_upper}: {e}", file=sys.stderr)

            if live_price is not None:
                updated = False
                for k in list(key_metrics.keys()):
                    if "current" in k.lower() and "price" in k.lower():
                        key_metrics[k] = f"₹{live_price:,.2f}"
                        updated = True
                if not updated:
                    key_metrics["Current Price"] = f"₹{live_price:,.2f}"
            
            # CSV lookup
            trendlyne_info = _lookup_trendlyne_sector(ticker_upper)
            csv_industry = trendlyne_info.get("industry", "")
            csv_sector = trendlyne_info.get("sector", "")
            
            # Format key metrics
            metrics_summary = []
            for k, v in key_metrics.items():
                metrics_summary.append(f"- {k}: {v}")
            metrics_text = "\n".join(metrics_summary) if metrics_summary else "No key metrics available."
            
            # Format quarterly results
            quarterly_text = "No quarterly results available."
            quarterly_data = fundamentals.get("Quarterly Results", [])
            if quarterly_data:
                lines = []
                for row in quarterly_data:
                    row_str = ", ".join(f"{k}: {v}" for k, v in row.items())
                    lines.append(f"- {row_str}")
                quarterly_text = "\n".join(lines)
                
            update_agent_job(job_id, {"progress": "🌌 Generating astronomical ephemeris data..."})
            astro_context = generate_cosmic_data_report()
            
            # Build the deterministic price-tape (multi-session trend) block
            if price_trend_text:
                price_tape_text = f"- **{company_name} ({ticker_upper})**: ₹{live_price:,.2f} | {price_trend_text}"
            elif live_price is not None:
                price_tape_text = f"- **{company_name} ({ticker_upper})**: ₹{live_price:,.2f} | multi-session trend unavailable"
            else:
                price_tape_text = "Live price tape unavailable for this ticker."

            # Formulate user message
            current_date = datetime.datetime.now().strftime("%B %d, %Y")
            user_message = f"""## REPORT DATE: {current_date}
## ANALYSIS TYPE: Stock Ticker Analysis
## TARGET TICKER: {ticker_upper}
## COMPANY NAME: {company_name}
## CSV INDUSTRY: {csv_industry or "N/A"}
## CSV SECTOR: {csv_sector or "N/A"}

---

## FEED 1: FINANCIAL FUNDAMENTAL SNAPSHOT
{metrics_text}

---

## LIVE PRICE TAPE (Deterministic Ground Truth — multi-session trend)
{price_tape_text}

---

## FEED 2: QUARTERLY RESULTS
{quarterly_text}

---

## FEED 3: COMPANY DESCRIPTION
{company_description or "No description available."}

---

## FEED 4: ASTROLOGICAL EPHEMERIS DATA (Swiss Ephemeris)
{astro_context}
"""
            cache_key = ticker_upper
            
        else:
            # Industry analysis
            update_agent_job(job_id, {"progress": f"🔍 Gathering stocks for industry: {industry}..."})
            industry_stocks = _lookup_trendlyne_industry_stocks(industry)
            sector_name = ""
            if industry_stocks:
                sector_name = industry_stocks[0].get("sector", "")
                
            peers_list_str = "\n".join([f"- {s['ticker']}: {s['stock_name']}" for s in industry_stocks])

            # Reconcile the industry/sector against an aggregate sectoral index price tape
            from agents.utils.market_data import get_index_trend
            sector_index_name = _resolve_sector_index(industry, sector_name)
            sector_tape = get_index_trend(sector_index_name) if sector_index_name else None
            if sector_tape:
                sector_tape_text = f"- Aggregate sectoral index for {industry} ({sector_name or 'sector'}): {sector_tape}"
            else:
                sector_tape_text = f"Aggregate sectoral index ({sector_index_name or 'n/a'}) trend temporarily unavailable."
            
            update_agent_job(job_id, {"progress": "🌌 Generating astronomical ephemeris data..."})
            astro_context = generate_cosmic_data_report()
            
            # Formulate user message
            current_date = datetime.datetime.now().strftime("%B %d, %Y")
            user_message = f"""## REPORT DATE: {current_date}
## ANALYSIS TYPE: Industry / Sector Analysis
## TARGET INDUSTRY: {industry}
## SECTOR: {sector_name or "N/A"}

---

## FEED 1: INDUSTRY COMPONENT STOCKS (from trendlyne CSV)
{peers_list_str or "No component stocks found in CSV."}

---

## LIVE PRICE TAPE (Aggregate Sectoral Index — Deterministic Ground Truth)
{sector_tape_text}

---

## FEED 2: ASTROLOGICAL EPHEMERIS DATA (Swiss Ephemeris)
{astro_context}
"""
            cache_key = f"IND_{industry.upper().replace(' ', '_')}"
            
        # GPT-5.5 Synthesis
        update_agent_job(job_id, {"progress": "🌌 GPT-5.5 synthesizing Cosmic Micro Intelligence Report..."})
        
        pdf_section = ""
        if COSMIC_PDF_AUGMENTATION_TEXT.strip():
            pdf_section = f"""
## ADDITIONAL ASTROLOGICAL REFERENCE (from PDF source):
{COSMIC_PDF_AUGMENTATION_TEXT}
"""
            
        system_prompt = COSMIC_MICRO_SYNTHESIS_PROMPT.format(
            astro_framework=COSMIC_ASTRO_FRAMEWORK,
            pdf_augmentation=pdf_section,
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        
        result_str = call_openai_api_fn(
            messages,
            model="gpt-5.5",
            temperature=1.0,
            timeout=300,
        )
        
        # Clean and parse JSON
        result_data = None
        try:
            clean_str = result_str.strip()
            if clean_str.startswith("```json"):
                clean_str = clean_str[7:]
            if clean_str.endswith("```"):
                clean_str = clean_str[:-3]
            clean_str = clean_str.strip()
            result_data = json.loads(clean_str)
            
            # Post-process to inject exact yfinance price if available
            if ticker and live_price is not None:
                if "fundamental_snapshot" in result_data:
                    result_data["fundamental_snapshot"]["current_price"] = f"₹{live_price:,.2f}"
        except Exception as parse_err:
            print(f"COSMIC_MICRO_AGENT: JSON parse failed: {parse_err}", file=sys.stderr)
            result_data = {
                "raw_text": result_str,
                "error": f"JSON parsing failed: {str(parse_err)}"
            }
            
        store_latest_result("cosmic_micro", cache_key, result_data)
        update_agent_job(job_id, {
            "status": "complete",
            "progress": "Analysis complete!",
            "result": result_data
        })
        
    except Exception as e:
        print(f"COSMIC_MICRO_AGENT ERROR in background job: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        update_agent_job(job_id, {
            "status": "error",
            "error": str(e)
        })


# =====================================================================
# ROUTE REGISTRATION
# =====================================================================
def register_cosmic_micro_routes(
    app,
    call_openai_api_fn,
    get_cache_fn,
    get_full_analysis_fn,
):
    """
    Register all Cosmic Micro Analyst Agent API routes.
    """

    @app.route("/api/cosmic_micro/industries", methods=["GET"])
    def get_cosmic_micro_industries():
        try:
            _load_trendlyne_csv()
            industries_list = sorted(list(_TRENDLYNE_INDUSTRIES.keys()))
            return jsonify(industries_list)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/agent/cosmic_micro/analyze", methods=["POST"])
    def agent_cosmic_micro_analyze():
        try:
            data = request.get_json(force=True)
            ticker = data.get("ticker", "").strip()
            industry = data.get("industry", "").strip()
            force_refresh = data.get("force_refresh", False)
            
            if not ticker and not industry:
                return jsonify({"error": "Either ticker or industry is required"}), 400
                
            if ticker:
                cache_key = ticker.upper().strip()
            else:
                cache_key = f"IND_{industry.upper().replace(' ', '_')}"
                
            # Check cached result
            if not force_refresh:
                existing = get_latest_result("cosmic_micro", cache_key)
                if existing:
                    age_hours = (time.time() - existing["stored_at"]) / 3600
                    if age_hours < 16:  # 16 hours TTL
                        age_minutes = int(age_hours * 60)
                        return jsonify({
                            "status": "complete",
                            "result": existing["result"],
                            "cached": True,
                            "age_minutes": age_minutes,
                        })
                        
            job_id = create_agent_job("cosmic_micro", cache_key)
            
            thread = threading.Thread(
                target=_run_cosmic_micro_analysis,
                args=(
                    job_id,
                    ticker,
                    industry,
                    call_openai_api_fn,
                    get_cache_fn,
                    get_full_analysis_fn,
                ),
            )
            thread.daemon = True
            thread.start()
            
            return jsonify({
                "job_id": job_id,
                "status": "processing",
                "message": f"Cosmic Micro analysis started for {ticker or industry}",
            })
            
        except Exception as e:
            print(f"COSMIC_MICRO_AGENT ROUTE ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500

    @app.route("/agent/cosmic_micro/<job_id>/status", methods=["GET"])
    def agent_cosmic_micro_status(job_id):
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

    @app.route("/agent/cosmic_micro/chat", methods=["POST"])
    def agent_cosmic_micro_chat():
        try:
            data = request.get_json(force=True)
            question = data.get("question", "").strip()
            analysis_context = data.get("analysis_context", "")
            
            if not question:
                return jsonify({"error": "Question is required"}), 400
                
            ticker = data.get("ticker", "").strip()
            industry = data.get("industry", "").strip()
            
            fundamental_ctx = "Not applicable (Industry analysis)"
            astro_ctx = "Not available"
            
            # Fetch context data for chat
            if ticker:
                ticker_upper = ticker.upper().strip()
                
                # Fetch yfinance live price + multi-session trend for chat
                live_price = None
                price_trend_text = None
                try:
                    import yfinance as yf
                    from agents.utils.market_data import _trend_metrics, _format_trend
                    for suffix in [".NS", ".BO", ""]:
                        symbol = f"{ticker_upper}{suffix}" if suffix else ticker_upper
                        if suffix == "" and "." not in ticker_upper:
                            continue
                        t = yf.Ticker(symbol)
                        hist = t.history(period="3mo")
                        if not hist.empty:
                            live_price = hist['Close'].iloc[-1]
                            try:
                                metrics = _trend_metrics(hist['Close'])
                                if metrics:
                                    price_trend_text = _format_trend(metrics)
                            except Exception:
                                pass
                            break
                        else:
                            info = t.info
                            live_price = info.get('currentPrice') or info.get('regularMarketPrice')
                            if live_price:
                                break
                except Exception as e:
                    print(f"yfinance fetch error in chat for {ticker_upper}: {e}", file=sys.stderr)

                cached_data = get_cache_fn(ticker_upper)
                if cached_data:
                    key_metrics = dict(cached_data.get("key_metrics", {}))
                    fundamentals = cached_data.get("fundamentals", {})
                    comp_desc = cached_data.get("company_description", "")
                    
                    if live_price is not None:
                        updated = False
                        for k in list(key_metrics.keys()):
                            if "current" in k.lower() and "price" in k.lower():
                                key_metrics[k] = f"₹{live_price:,.2f}"
                                updated = True
                        if not updated:
                            key_metrics["Current Price"] = f"₹{live_price:,.2f}"
                    
                    metrics_summary = [f"- {k}: {v}" for k, v in key_metrics.items()]
                    metrics_text = "\n".join(metrics_summary)
                    price_tape_line = price_trend_text if price_trend_text else "multi-session trend unavailable"
                    
                    quarterly_text = "No quarterly results available."
                    quarterly_data = fundamentals.get("Quarterly Results", [])
                    if quarterly_data:
                        lines = []
                        for row in quarterly_data:
                            row_str = ", ".join(f"{k}: {v}" for k, v in row.items())
                            lines.append(f"- {row_str}")
                        quarterly_text = "\n".join(lines)
                        
                    fundamental_ctx = f"""Company Summary: {comp_desc}
                    
Key Metrics:
{metrics_text}

Recent Price Tape (multi-session trend): {price_tape_line}

Quarterly Results:
{quarterly_text}"""

            # Generate astro ephemeris data
            astro_ctx = generate_cosmic_data_report()
            
            pdf_section = ""
            if COSMIC_PDF_AUGMENTATION_TEXT.strip():
                pdf_section = f"""
## ADDITIONAL ASTROLOGICAL REFERENCE (from PDF source):
{COSMIC_PDF_AUGMENTATION_TEXT}
"""

            chat_prompt = COSMIC_MICRO_CHAT_PROMPT.format(
                analysis=str(analysis_context)[:40000],
                fundamental_context=fundamental_ctx[:15000],
                astro_context=astro_ctx[:15000],
                pdf_augmentation=pdf_section,
            )
            
            messages = [
                {"role": "system", "content": chat_prompt},
                {"role": "user", "content": question},
            ]
            
            answer = call_openai_api_fn(
                messages,
                model="gpt-5.5",
                temperature=1.0,
                timeout=180,
            )
            
            return jsonify({"answer": answer, "status": "success"})
            
        except Exception as e:
            print(f"COSMIC_MICRO_AGENT_CHAT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500

    @app.route("/agent/cosmic_micro/latest/<ticker>", methods=["GET"])
    def agent_cosmic_micro_latest(ticker):
        if not ticker:
            return jsonify({"status": "none", "message": "Ticker is required"})
            
        ticker_upper = ticker.upper().strip()
        result = get_latest_result("cosmic_micro", ticker_upper)
        if result:
            return jsonify({
                "status": "complete",
                "result": result["result"],
                "age_minutes": round((time.time() - result["stored_at"]) / 60),
            })
            
        return jsonify({"status": "none", "message": f"No cached result found for {ticker_upper}"})
