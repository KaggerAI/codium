"""
forensic_agent.py — Forensic Analysis Agent for Kagger AI Agent Marketplace.

Computes quantitative red-flag scores (Beneish M-Score, Altman Z-Score),
analyzes shareholding patterns, searches for analyst downgrades and negative
news via Perplexity, fetches credit rating & annual report PDFs, and produces
a structured Forensic Health Report via Gemini.
"""

import sys
import time
import json
import math
import asyncio
import threading
import traceback

from flask import request, jsonify

from agents.base import (
    create_agent_job, update_agent_job, get_agent_job,
    store_latest_result, get_latest_result
)
from agents.prompts.forensic_prompts import (
    FORENSIC_ANALYSIS_PROMPT, FORENSIC_CHAT_PROMPT, FORENSIC_NO_DATA_MSG
)
from fund_calculations import calculate_altman_zscore, calculate_beneish_mscore


# =====================================================================
# QUANTITATIVE CALCULATIONS (Pure Python, no API calls)
# =====================================================================

def _safe_div(a, b, default=None):
    """Safe division that handles None, NaN, zero, and non-numeric types."""
    try:
        a_val = float(a) if a is not None else None
        b_val = float(b) if b is not None else None
        if a_val is None or b_val is None or b_val == 0 or math.isnan(a_val) or math.isnan(b_val):
            return default
        result = a_val / b_val
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _safe_float(val, default=None):
    """Safely convert a value to float."""
    if val is None:
        return default
    try:
        result = float(str(val).replace(',', '').replace('%', '').strip())
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _get_table_values(fundamentals, table_name, row_name, num_years=3):
    """
    Extract values for a specific row from a fundamentals table.
    Handles both dict-of-lists, list-of-dicts (records), and DataFrame formats.
    Returns a list of floats (most recent first).
    """
    table = fundamentals.get(table_name)
    if table is None:
        return []
    
    # Handle JSON string
    if isinstance(table, str):
        try:
            table = json.loads(table)
        except:
            return []
    
    # Handle DataFrame
    if hasattr(table, 'to_dict') and not isinstance(table, (dict, list)):
        try:
            df = table
            # Find the row matching row_name (case-insensitive partial match)
            matching_rows = [idx for idx in df.index if row_name.lower() in str(idx).lower()]
            if not matching_rows:
                return []
            row = df.loc[matching_rows[0]]
            # Get the last num_years values (most recent last in DF)
            values = row.values[-num_years:] if len(row) >= num_years else row.values
            return [_safe_float(v) for v in reversed(values)]  # Most recent first
        except Exception:
            return []

    # Handle list of records (records format: [{'': 'RowName', '2021': 10, '2022': 20}, ...])
    if isinstance(table, list):
        for row in table:
            if not isinstance(row, dict):
                continue
            # The first value in the dict is usually the row name (index column)
            row_label = next(iter(row.values()), "")
            if row_name.lower() in str(row_label).lower():
                # Extract values from the row, skipping the label (first column)
                val_keys = list(row.keys())[1:]
                values = [row[k] for k in val_keys if row[k] is not None]
                recent = values[-num_years:] if len(values) >= num_years else values
                return [_safe_float(v) for v in reversed(recent)]

    # Handle dict format (JSON parsed: {'RowName': [10, 20], ...})
    if isinstance(table, dict):
        for key, values in table.items():
            if row_name.lower() in key.lower():
                if isinstance(values, list):
                    # Filter out None values
                    cleaned_vals = [v for v in values if v is not None]
                    recent = cleaned_vals[-num_years:] if len(cleaned_vals) >= num_years else cleaned_vals
                    return [_safe_float(v) for v in reversed(recent)]
                elif isinstance(values, dict):
                    # Sorted by column header (date), most recent first
                    # Filter out None values
                    cleaned_items = [(k, v) for k, v in values.items() if v is not None]
                    sorted_vals = sorted(cleaned_items, reverse=True)[:num_years]
                    return [_safe_float(v) for _, v in sorted_vals]
    
    return []


def compute_beneish_mscore(fundamentals):
    """
    Compute adapted 5-variable Beneish M-Score.
    Delegates to fund_calculations.calculate_beneish_mscore for robust logic.
    """
    try:
        retrieved_data = {"Fundamentals": fundamentals}
        calc_spec = {"inputs": {"period": -1}}
        result = calculate_beneish_mscore(retrieved_data, calc_spec)
        
        return {
            'score': result.get('value'),
            'variables': result.get('variables', {}),
            'interpretation': result.get('interpretation', 'Insufficient data'),
            'flag': result.get('flag', '⚪')
        }
    except Exception as e:
        print(f"FORENSIC: M-Score calculation error: {e}", file=sys.stderr)
        return {
            'score': None,
            'variables': {},
            'interpretation': 'Calculation error',
            'flag': '⚪'
        }


def compute_altman_zscore(fundamentals, key_metrics):
    """
    Compute Altman Z-Score.
    Delegates to fund_calculations.calculate_altman_zscore for robust logic.
    """
    try:
        retrieved_data = {"Fundamentals": fundamentals}
        market_cap_str = key_metrics.get('market_cap', '0')
        market_cap = _safe_float(market_cap_str)
        calc_spec = {"inputs": {"period": -1, "market_cap": market_cap}}
        result = calculate_altman_zscore(retrieved_data, calc_spec)
        
        return {
            'score': result.get('value'),
            'variables': result.get('variables', {}),
            'interpretation': result.get('interpretation', 'Insufficient data'),
            'flag': result.get('flag', '⚪')
        }
    except Exception as e:
        print(f"FORENSIC: Z-Score calculation error: {e}", file=sys.stderr)
        return {
            'score': None,
            'variables': {},
            'interpretation': 'Calculation error',
            'flag': '⚪'
        }


def compute_signal_checks(fundamentals, key_metrics):
    """
    Compute 8 additional forensic signals from financial data.
    Returns a list of signal dicts with name, value, threshold, and flag.
    """
    signals = []
    
    try:
        # 1. Promoter Stake Drop (QoQ)
        shareholding = fundamentals.get('Quarterly Shareholding Pattern') or fundamentals.get('Quarterly Shareholding')
        promoter_drop = _compute_promoter_drop(shareholding)
        signals.append(promoter_drop)
        
        # 2. FII Exodus (QoQ)
        fii_drop = _compute_fii_drop(shareholding)
        signals.append(fii_drop)
        
        # 3. Cash-Profit Divergence (CFO / Net Profit)
        cash_div = _compute_cash_profit_divergence(fundamentals)
        signals.append(cash_div)
        
        # 4. Debt Overload (Borrowings / Equity)
        debt_ratio = _compute_debt_ratio(fundamentals)
        signals.append(debt_ratio)
        
        # 5. Interest Coverage (Operating Profit / Interest)
        interest_cov = _compute_interest_coverage(fundamentals)
        signals.append(interest_cov)
        
        # 6. Margin Erosion (4Q OPM trend)
        margin_trend = _compute_margin_erosion(fundamentals)
        signals.append(margin_trend)
        
        # 7. Negative FCF (CFO - Capex for 3 years)
        neg_fcf = _compute_negative_fcf(fundamentals)
        signals.append(neg_fcf)
        
        # 8. ROE Deterioration
        roe_trend = _compute_roe_deterioration(fundamentals)
        signals.append(roe_trend)
        
    except Exception as e:
        print(f"FORENSIC: Signal checks error: {e}", file=sys.stderr)
    
    return signals


def _compute_promoter_drop(shareholding):
    """Check for significant promoter stake decline QoQ."""
    signal = {'name': 'Promoter Stake Change (QoQ)', 'value': 'N/A', 'threshold': '>2% drop = 🔴', 'flag': '⚪'}
    try:
        promoter_vals = _get_shareholding_row(shareholding, 'Promoters')
        if len(promoter_vals) >= 2:
            change = promoter_vals[0] - promoter_vals[1]  # Most recent - previous
            signal['value'] = f'{change:+.2f}%'
            if change <= -2:
                signal['flag'] = '🔴'
            elif change <= -1:
                signal['flag'] = '🟡'
            else:
                signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_fii_drop(shareholding):
    """Check for FII exodus pattern."""
    signal = {'name': 'FII Change (QoQ)', 'value': 'N/A', 'threshold': '>3% drop = 🟡', 'flag': '⚪'}
    try:
        fii_vals = _get_shareholding_row(shareholding, 'FII')
        if not fii_vals or len(fii_vals) < 2:
            fii_vals = _get_shareholding_row(shareholding, 'Foreign')
        if len(fii_vals) >= 2:
            change = fii_vals[0] - fii_vals[1]
            signal['value'] = f'{change:+.2f}%'
            if change <= -3:
                signal['flag'] = '🟡'
            elif change <= -1:
                signal['flag'] = '🟢'
            else:
                signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_cash_profit_divergence(fundamentals):
    """Check CFO vs Net Profit ratio."""
    signal = {'name': 'Cash-Profit Divergence (CFO/NP)', 'value': 'N/A', 'threshold': '<0.5 for 2+ yrs = 🔴', 'flag': '⚪'}
    try:
        cfo = _get_table_values(fundamentals, 'Cash Flow', 'Cash from Operating', 3)
        net_profit = _get_table_values(fundamentals, 'Annual Results', 'Net Profit', 3)
        if not net_profit or len(net_profit) < 2:
            net_profit = _get_table_values(fundamentals, 'Profit & Loss', 'Net Profit', 3)
        
        if len(cfo) >= 2 and len(net_profit) >= 2:
            ratios = []
            for i in range(min(len(cfo), len(net_profit))):
                r = _safe_div(cfo[i], net_profit[i])
                if r is not None:
                    ratios.append(r)
            
            if ratios:
                avg_ratio = sum(ratios) / len(ratios)
                signal['value'] = f'{avg_ratio:.2f}x (avg {len(ratios)} yrs)'
                low_count = sum(1 for r in ratios if r < 0.5)
                if low_count >= 2:
                    signal['flag'] = '🔴'
                elif avg_ratio < 0.5:
                    signal['flag'] = '🟡'
                else:
                    signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_debt_ratio(fundamentals):
    """Check Borrowings / Equity ratio."""
    signal = {'name': 'Debt Overload (Borr/Equity)', 'value': 'N/A', 'threshold': '>2.0 = 🔴', 'flag': '⚪'}
    try:
        borrowings = _get_table_values(fundamentals, 'Balance Sheet', 'Borrowings', 1)
        equity_cap = _get_table_values(fundamentals, 'Balance Sheet', 'Share Capital', 1)
        reserves = _get_table_values(fundamentals, 'Balance Sheet', 'Reserves', 1)
        
        if borrowings:
            borr = borrowings[0] or 0
            eq = (equity_cap[0] or 0) if equity_cap else 0
            res = (reserves[0] or 0) if reserves else 0
            total_equity = eq + res
            
            ratio = _safe_div(borr, total_equity)
            if ratio is not None:
                signal['value'] = f'{ratio:.2f}x'
                if ratio > 2.0:
                    signal['flag'] = '🔴'
                elif ratio > 1.0:
                    signal['flag'] = '🟡'
                else:
                    signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_interest_coverage(fundamentals):
    """Check Operating Profit / Interest ratio."""
    signal = {'name': 'Interest Coverage', 'value': 'N/A', 'threshold': '<1.5 = 🔴', 'flag': '⚪'}
    try:
        op_profit = _get_table_values(fundamentals, 'Annual Results', 'Operating Profit', 1)
        if not op_profit:
            op_profit = _get_table_values(fundamentals, 'Profit & Loss', 'Operating Profit', 1)
        interest = _get_table_values(fundamentals, 'Annual Results', 'Interest', 1)
        if not interest:
            interest = _get_table_values(fundamentals, 'Profit & Loss', 'Interest', 1)
        
        if op_profit and interest:
            ratio = _safe_div(op_profit[0], interest[0])
            if ratio is not None:
                signal['value'] = f'{ratio:.2f}x'
                if ratio < 1.5:
                    signal['flag'] = '🔴'
                elif ratio < 3.0:
                    signal['flag'] = '🟡'
                else:
                    signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_margin_erosion(fundamentals):
    """Check OPM% trend over 4 quarters."""
    signal = {'name': 'Margin Erosion (OPM% trend)', 'value': 'N/A', 'threshold': 'Declining 4+ Qtrs = 🟡', 'flag': '⚪'}
    try:
        opm = _get_table_values(fundamentals, 'Financial Ratios', 'OPM', 4)
        if not opm or len(opm) < 3:
            # Try quarterly data to compute OPM
            q_sales = _get_table_values(fundamentals, 'Quarterly Results', 'Sales', 5)
            q_expenses = _get_table_values(fundamentals, 'Quarterly Results', 'Expenses', 5)
            if len(q_sales) >= 4 and len(q_expenses) >= 4:
                opm = []
                for i in range(min(len(q_sales), len(q_expenses))):
                    if q_sales[i] and q_sales[i] > 0:
                        margin = ((q_sales[i] - q_expenses[i]) / q_sales[i]) * 100
                        opm.append(margin)
        
        if len(opm) >= 3:
            # Check if consistently declining
            declining_count = sum(1 for i in range(len(opm)-1) if opm[i] < opm[i+1])
            signal['value'] = f'{opm[0]:.1f}% (latest) | {declining_count}/{len(opm)-1} declining'
            if declining_count >= 3:
                signal['flag'] = '🟡'
            elif declining_count >= 2:
                signal['flag'] = '🟢'
            else:
                signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_negative_fcf(fundamentals):
    """Check if Free Cash Flow (CFO - Capex) has been negative for 3+ years."""
    signal = {'name': 'Negative FCF Streak', 'value': 'N/A', 'threshold': '3+ years = 🔴', 'flag': '⚪'}
    try:
        cfo = _get_table_values(fundamentals, 'Cash Flow', 'Cash from Operating', 3)
        capex = _get_table_values(fundamentals, 'Cash Flow', 'Cash from Investing', 3)
        
        if len(cfo) >= 2 and len(capex) >= 2:
            neg_count = 0
            fcf_values = []
            for i in range(min(len(cfo), len(capex))):
                if cfo[i] is not None and capex[i] is not None:
                    # Investing activity is typically negative (outflow)
                    fcf = cfo[i] + capex[i]  # capex is already negative
                    fcf_values.append(fcf)
                    if fcf < 0:
                        neg_count += 1
            
            if fcf_values:
                signal['value'] = f'{neg_count}/{len(fcf_values)} years negative'
                if neg_count >= 3:
                    signal['flag'] = '🔴'
                elif neg_count >= 2:
                    signal['flag'] = '🟡'
                else:
                    signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_roe_deterioration(fundamentals):
    """Check ROE trend over recent years."""
    signal = {'name': 'ROE Trend', 'value': 'N/A', 'threshold': 'Declining 3+ yrs = 🟡', 'flag': '⚪'}
    try:
        roe = _get_table_values(fundamentals, 'Financial Ratios', 'ROE', 4)
        if not roe or len(roe) < 3:
            roe = _get_table_values(fundamentals, 'Financial Ratios', 'Return on Equity', 4)
        
        if len(roe) >= 3:
            declining = sum(1 for i in range(len(roe)-1) if roe[i] is not None and roe[i+1] is not None and roe[i] < roe[i+1])
            latest = roe[0] if roe[0] is not None else 'N/A'
            signal['value'] = f'{latest}% (latest) | {declining}/{len(roe)-1} declining'
            if declining >= 3:
                signal['flag'] = '🟡'
            elif declining >= 2 and latest is not None and isinstance(latest, (int, float)) and latest < 10:
                signal['flag'] = '🟡'
            else:
                signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _get_shareholding_row(shareholding, row_name):
    """Extract shareholding values for a given category (Promoters, FII, etc.)."""
    if shareholding is None:
        return []
    
    # Handle DataFrame
    if hasattr(shareholding, 'to_dict'):
        try:
            matching = [idx for idx in shareholding.index if row_name.lower() in str(idx).lower()]
            if matching:
                vals = shareholding.loc[matching[0]].values
                return [_safe_float(v) for v in reversed(vals) if _safe_float(v) is not None]
        except Exception:
            pass
    
    # Handle dict format
    if isinstance(shareholding, dict):
        for key, values in shareholding.items():
            if row_name.lower() in key.lower():
                if isinstance(values, list):
                    return [_safe_float(v) for v in reversed(values) if _safe_float(v) is not None]
                elif isinstance(values, dict):
                    sorted_vals = sorted(values.items(), reverse=True)
                    return [_safe_float(v) for _, v in sorted_vals if _safe_float(v) is not None]
    
    return []


# =====================================================================
# ROUTE REGISTRATION
# =====================================================================

def register_forensic_routes(app, call_gemini_api_fn, call_perplexity_api_fn,
                              get_cache_fn, fetch_forensic_docs_fn, get_full_analysis_fn=None):
    """
    Register all Forensic Agent API routes with the Flask app.
    
    Args:
        app: Flask app instance
        call_gemini_api_fn: Reference to call_gemini_api from handler.py
        call_perplexity_api_fn: Reference to call_perplexity_api from handler.py
        get_cache_fn: Reference to get_any_cache from handler.py (checks local + Redis)
        fetch_forensic_docs_fn: Reference to fetch_forensic_documents_async from screener_fetcher
        get_full_analysis_fn: Reference to get_analysis_for_ticker from handler.py (for fallback)
    """

    @app.route('/agent/forensic/analyze', methods=['POST'])
    def agent_forensic_analyze():
        """
        Start a Forensic Agent analysis job.
        Takes { ticker: str } and returns { job_id: str }.
        Uses background thread + polling pattern.
        """
        try:
            data = request.get_json(force=True)
            ticker = data.get('ticker', '').strip().upper()

            if not ticker:
                return jsonify({'error': 'Ticker is required'}), 400

            print(f"FORENSIC_AGENT: Starting analysis for {ticker}", file=sys.stderr)

            # Check if we already have a recent result for this ticker
            existing = get_latest_result('forensic', ticker)
            if existing and data.get('force_refresh') is not True:
                age_minutes = (time.time() - existing['stored_at']) / 60
                if age_minutes < 120:  # Less than 2 hours old
                    print(f"FORENSIC_AGENT: Returning cached result for {ticker} ({age_minutes:.0f}m old)", file=sys.stderr)
                    return jsonify({
                        'status': 'complete',
                        'result': existing['result'],
                        'cached': True,
                        'age_minutes': round(age_minutes)
                    })

            # Check that we have base data to work with (checks Local + Redis)
            cached_data = get_cache_fn(ticker)
            
            # --- QUARTERLY FRESHNESS CHECK ---
            # If cache exists, verify it has the latest quarterly results.
            # If stale, invalidate cache to trigger the full analysis fallback below.
            if cached_data and get_full_analysis_fn:
                try:
                    from screener_fetcher import fetch_latest_quarter_header_async
                    cached_fund = cached_data.get('fundamentals', {})
                    cached_is_consolidated = cached_data.get('is_consolidated', False)
                    
                    # Extract latest quarter from cached data
                    q_results_json = cached_fund.get('Quarterly Results')
                    if q_results_json:
                        if isinstance(q_results_json, str):
                            q_data = json.loads(q_results_json)
                            q_headers = [str(h).strip() for h in q_data.get('index', []) if h and str(h).strip()]
                        elif isinstance(q_results_json, list):
                            # Records format — extract column headers
                            if q_results_json:
                                q_headers = [str(k).strip() for k in q_results_json[0].keys() if k and str(k).strip()]
                                q_headers = q_headers[1:]  # Skip label column
                        else:
                            q_headers = []
                        
                        if q_headers:
                            cached_latest_q = q_headers[-1]
                            live_latest_q = asyncio.run(fetch_latest_quarter_header_async(ticker, consolidated=cached_is_consolidated))
                            
                            print(f"FORENSIC_AGENT: Quarter check for {ticker}: Cached='{cached_latest_q}' vs Live='{live_latest_q}' (Consolidated={cached_is_consolidated})", file=sys.stderr)
                            
                            if live_latest_q and live_latest_q != cached_latest_q:
                                print(f"FORENSIC_AGENT: STALE DATA — new quarter {live_latest_q} detected. Invalidating cache for {ticker}.", file=sys.stderr)
                                cached_data = None  # Trigger full analysis fallback
                            else:
                                print(f"FORENSIC_AGENT: Cache is fresh for {ticker} (latest: {cached_latest_q}).", file=sys.stderr)
                except Exception as qcheck_err:
                    print(f"FORENSIC_AGENT: Quarter check failed for {ticker} (using cached data): {qcheck_err}", file=sys.stderr)
            
            # --- NEW: FALLBACK TO FULL ANALYSIS IF NO CACHE FOUND ---
            if not cached_data and get_full_analysis_fn:
                print(f"FORENSIC_AGENT: No cached data found for {ticker}. Running full analysis fallback...", file=sys.stderr)
                try:
                    # Run full analysis synchronously (this might take 40-60s)
                    # We do this here because the background pipeline needs the data
                    # --- MODIFIED: Skip AI Summary for Forensic Agent to save cost/time ---
                    _, cached_data = get_full_analysis_fn(ticker, skip_ai_summary=True)
                    
                    if not cached_data:
                        print(f"FORENSIC_AGENT: Full analysis fallback failed for {ticker}", file=sys.stderr)
                        return jsonify({
                            'error': 'no_base_data',
                            'message': FORENSIC_NO_DATA_MSG
                        }), 400
                    
                    print(f"FORENSIC_AGENT: Full analysis fallback successful for {ticker}", file=sys.stderr)
                except Exception as fe:
                    print(f"FORENSIC_AGENT ERROR during fallback for {ticker}: {fe}", file=sys.stderr)
                    return jsonify({
                        'error': 'fallback_failed',
                        'message': f"Could not pull financial data for {ticker}. Error: {str(fe)}"
                    }), 500

            if not cached_data:
                print(f"FORENSIC_AGENT: No cached data found for {ticker} and no fallback available", file=sys.stderr)
                return jsonify({
                    'error': 'no_base_data',
                    'message': FORENSIC_NO_DATA_MSG
                }), 400

            # Create background job
            job_id = create_agent_job('forensic', ticker)

            # Start background analysis thread
            thread = threading.Thread(
                target=_run_forensic_analysis,
                args=(job_id, ticker, cached_data, call_gemini_api_fn,
                      call_perplexity_api_fn, fetch_forensic_docs_fn)
            )
            thread.daemon = True
            thread.start()

            return jsonify({
                'job_id': job_id,
                'status': 'processing',
                'message': f'Forensic analysis started for {ticker}.'
            })

        except Exception as e:
            print(f"FORENSIC_AGENT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({'error': str(e)}), 500

    @app.route('/agent/forensic/<job_id>/status', methods=['GET'])
    def agent_forensic_status(job_id):
        """Poll endpoint for Forensic Agent job status."""
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

    @app.route('/agent/forensic/chat', methods=['POST'])
    def agent_forensic_chat():
        """
        Chat with the Forensic Agent about a completed analysis.
        Takes { ticker, question, analysis_context, data_context }.
        """
        try:
            data = request.get_json(force=True)
            question = data.get('question', '').strip()
            ticker = data.get('ticker', '').strip().upper()
            analysis_context = data.get('analysis_context', '').strip()
            data_context = data.get('data_context', '').strip()
            company_name = data.get('company_name', ticker)

            if not question:
                return jsonify({'error': 'Question is required'}), 400

            if not analysis_context:
                return jsonify({'error': 'Analysis context is required. Run the analysis first.'}), 400

            print(f"FORENSIC_AGENT_CHAT: Question for {ticker}: {question[:80]}...", file=sys.stderr)

            chat_prompt = FORENSIC_CHAT_PROMPT.format(
                company_name=company_name,
                ticker=ticker,
                analysis=analysis_context[:50000],
                data_context=data_context[:30000] if data_context else "Raw data not available."
            )

            messages = [
                {"role": "system", "content": chat_prompt},
                {"role": "user", "content": question}
            ]

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
            print(f"FORENSIC_AGENT_CHAT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({'error': str(e)}), 500

    @app.route('/agent/forensic/latest', methods=['GET'])
    def agent_forensic_latest():
        """
        Get the latest cached result for a ticker (for persistence across tab switches).
        """
        ticker = request.args.get('ticker', '').strip().upper()
        if not ticker:
            return jsonify({'error': 'Ticker is required'}), 400

        result = get_latest_result('forensic', ticker)
        if result:
            return jsonify({
                'status': 'complete',
                'result': result['result'],
                'age_minutes': round((time.time() - result['stored_at']) / 60)
            })

        return jsonify({'status': 'none', 'message': 'No cached result found'})


# =====================================================================
# BACKGROUND ANALYSIS PIPELINE
# =====================================================================

def _run_forensic_analysis(job_id, ticker, cached_data, call_gemini_api_fn,
                            call_perplexity_api_fn, fetch_forensic_docs_fn):
    """
    Background function that runs the full Forensic Agent analysis pipeline:
    1. Read cached financial data
    2. Fetch fresh data in parallel (news, analyst recs, credit ratings, annual report)
    3. Compute quantitative scores (M-Score, Z-Score, signals)
    4. Send everything to Gemini for structured analysis
    5. Store result
    """
    start_time = time.time()

    try:
        # =====================================================
        # STEP 1: Extract data from cache
        # =====================================================
        update_agent_job(job_id, {'progress': f'Reading cached financial data for {ticker}...'})
        print(f"FORENSIC_AGENT: Step 1 — Reading cached data for {ticker}", file=sys.stderr)

        fundamentals = cached_data.get('fundamentals', {})
        key_metrics = cached_data.get('key_metrics', {})
        company_name = cached_data.get('company_name', ticker)

        # Handle case where fundamentals might be JSON string (from Redis)
        if isinstance(fundamentals, str):
            try:
                fundamentals = json.loads(fundamentals)
            except json.JSONDecodeError:
                fundamentals = {}

        elapsed_step1 = int(time.time() - start_time)
        print(f"FORENSIC_AGENT: Step 1 complete — Cached data loaded in {elapsed_step1}s", file=sys.stderr)

        # =====================================================
        # STEP 2: Fetch fresh data in parallel (async)
        # =====================================================
        update_agent_job(job_id, {
            'progress': f'Fetching analyst recommendations, news, and documents for {ticker}...'
        })
        print(f"FORENSIC_AGENT: Step 2 — Fetching fresh data in parallel", file=sys.stderr)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            analyst_recs, negative_news, forensic_docs = loop.run_until_complete(
                _fetch_fresh_data_async(ticker, company_name, call_perplexity_api_fn, fetch_forensic_docs_fn)
            )
        finally:
            loop.close()

        elapsed_step2 = int(time.time() - start_time)
        print(f"FORENSIC_AGENT: Step 2 complete — Fresh data fetched in {elapsed_step2}s", file=sys.stderr)

        # =====================================================
        # STEP 3: Compute quantitative scores
        # =====================================================
        update_agent_job(job_id, {
            'progress': f'Computing M-Score, Z-Score, and signal checks for {ticker}...'
        })
        print(f"FORENSIC_AGENT: Step 3 — Computing quantitative scores", file=sys.stderr)

        mscore = compute_beneish_mscore(fundamentals)
        zscore = compute_altman_zscore(fundamentals, key_metrics)
        signals = compute_signal_checks(fundamentals, key_metrics)

        elapsed_step3 = int(time.time() - start_time)
        print(f"FORENSIC_AGENT: Step 3 complete — Scores computed in {elapsed_step3}s", file=sys.stderr)
        print(f"  M-Score: {mscore['score']} ({mscore['flag']}), Z-Score: {zscore['score']} ({zscore['flag']})", file=sys.stderr)

        # =====================================================
        # STEP 4: Send everything to Gemini
        # =====================================================
        update_agent_job(job_id, {
            'progress': f'AI is analyzing all forensic data for {ticker} — generating report...'
        })
        print(f"FORENSIC_AGENT: Step 4 — Sending to Gemini for analysis", file=sys.stderr)

        # Build the data context for Gemini
        data_context = _build_gemini_context(
            ticker, company_name, fundamentals, key_metrics,
            mscore, zscore, signals, analyst_recs, negative_news, forensic_docs
        )

        analysis_prompt = f"""{FORENSIC_ANALYSIS_PROMPT}

## Company: {company_name} ({ticker})

## Data Package:
{data_context}
"""

        messages = [{"role": "user", "content": analysis_prompt}]

        analysis_result = call_gemini_api_fn(
            messages,
            model="gemini-3-flash-preview",
            temperature=1,
            thinking_level='HIGH'   # Deep reasoning for thorough forensic analysis
        )

        elapsed_step4 = int(time.time() - start_time)
        print(f"FORENSIC_AGENT: Step 4 complete — Gemini analysis in {elapsed_step4}s ({len(analysis_result)} chars)", file=sys.stderr)

        # =====================================================
        # STEP 5: Store result
        # =====================================================
        result_data = {
            'analysis': analysis_result,
            'ticker': ticker,
            'company_name': company_name,
            'scores': {
                'mscore': mscore,
                'zscore': zscore,
                'signals': signals
            },
            'data_sources': {
                'analyst_recs': analyst_recs[:2000] if analyst_recs else None,
                'negative_news': negative_news[:2000] if negative_news else None,
                'credit_ratings_count': len(forensic_docs.get('credit_ratings', [])),
                'has_annual_report': forensic_docs.get('annual_report') is not None
            },
            'analyzed_at': time.time(),
            'analysis_time_seconds': elapsed_step4
        }

        # Save to persistent store
        store_latest_result('forensic', ticker, result_data)

        # Update job as complete
        update_agent_job(job_id, {
            'status': 'complete',
            'progress': 'Forensic analysis complete!',
            'result': result_data,
            'completed_at': time.time(),
            'total_time': elapsed_step4
        })

        print(f"FORENSIC_AGENT: ✓ Analysis complete for {ticker} in {elapsed_step4}s ({len(analysis_result)} chars)", file=sys.stderr)

    except Exception as e:
        elapsed = int(time.time() - start_time)
        print(f"FORENSIC_AGENT ERROR: Job {job_id} failed after {elapsed}s: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        update_agent_job(job_id, {
            'status': 'error',
            'error': f'Forensic analysis failed: {str(e)}',
            'failed_at': time.time(),
            'elapsed': elapsed
        })


async def _fetch_fresh_data_async(ticker, company_name, call_perplexity_api_fn, fetch_forensic_docs_fn):
    """
    Fetch all fresh data in parallel:
    1. Analyst recommendations via Perplexity
    2. Negative news via Perplexity
    3. Credit ratings + annual report PDFs from Screener
    """
    
    async def get_analyst_recs():
        """Fetch analyst recommendations via Perplexity."""
        try:
            query = f"""What are the latest analyst ratings, recommendations, and target price changes
for {company_name} ({ticker}) on NSE/BSE India in the last 3 months?
Include: brokerage name, rating (Buy/Sell/Hold), target price, date of change.
Focus on any DOWNGRADES or SELL recommendations.
Return as structured data. If no recent changes found, say so."""

            result = await asyncio.to_thread(
                call_perplexity_api_fn,
                [{"role": "user", "content": query}],
                "sonar-pro", 0.1, 60
            )
            return result
        except Exception as e:
            print(f"FORENSIC_AGENT: Analyst recs fetch failed: {e}", file=sys.stderr)
            return f"Error fetching analyst recommendations: {e}"
    
    async def get_negative_news():
        """Fetch negative news via Perplexity."""
        try:
            query = f"""Search for any negative news, controversies, regulatory actions, SEBI notices,
fraud allegations, auditor resignations, management exits, or legal issues
involving {company_name} ({ticker}) in the last 6 months.
Also include any credit rating downgrades by CRISIL, ICRA, CARE, or India Ratings.
If there is no significant negative news, say "No significant negative news found."
Be factual and cite sources."""

            result = await asyncio.to_thread(
                call_perplexity_api_fn,
                [{"role": "user", "content": query}],
                "sonar-pro", 0.1, 60
            )
            return result
        except Exception as e:
            print(f"FORENSIC_AGENT: Negative news fetch failed: {e}", file=sys.stderr)
            return f"Error fetching negative news: {e}"
    
    async def get_forensic_docs():
        """Fetch credit rating + annual report PDFs from Screener."""
        try:
            return await fetch_forensic_docs_fn(ticker)
        except Exception as e:
            print(f"FORENSIC_AGENT: Forensic docs fetch failed: {e}", file=sys.stderr)
            return {'credit_ratings': [], 'annual_report': None}
    
    # Run all three in parallel
    results = await asyncio.gather(
        get_analyst_recs(),
        get_negative_news(),
        get_forensic_docs(),
        return_exceptions=True
    )
    
    analyst_recs = results[0] if not isinstance(results[0], Exception) else f"Error: {results[0]}"
    negative_news = results[1] if not isinstance(results[1], Exception) else f"Error: {results[1]}"
    forensic_docs = results[2] if not isinstance(results[2], Exception) else {'credit_ratings': [], 'annual_report': None}
    
    return analyst_recs, negative_news, forensic_docs


def _build_gemini_context(ticker, company_name, fundamentals, key_metrics,
                           mscore, zscore, signals, analyst_recs, negative_news, forensic_docs):
    """Build the comprehensive data context string for the Gemini prompt."""
    
    sections = []
    
    # --- Quantitative Scores ---
    sections.append("### COMPUTED SCORES")
    sections.append(f"\n**Beneish M-Score:** {mscore['score']} {mscore['flag']}")
    sections.append(f"Interpretation: {mscore['interpretation']}")
    sections.append(f"Variables: {json.dumps(mscore['variables'], indent=2)}")
    
    sections.append(f"\n**Altman Z-Score:** {zscore['score']} {zscore['flag']}")
    sections.append(f"Interpretation: {zscore['interpretation']}")
    sections.append(f"Variables: {json.dumps(zscore['variables'], indent=2)}")
    
    # --- Signal Checks ---
    sections.append("\n### SIGNAL CHECKS")
    for sig in signals:
        sections.append(f"- {sig['flag']} **{sig['name']}**: {sig['value']} (threshold: {sig['threshold']})")
    
    # --- Key Metrics ---
    sections.append("\n### KEY METRICS")
    if key_metrics:
        for k, v in key_metrics.items():
            sections.append(f"- {k}: {v}")
    
    # --- Financial Statements Summary ---
    sections.append("\n### FINANCIAL STATEMENTS (from cache)")
    if fundamentals:
        for table_name in ['Annual Results', 'Profit & Loss', 'Balance Sheet', 'Cash Flow', 
                          'Financial Ratios', 'Quarterly Results', 'Quarterly Shareholding Pattern',
                          'Quarterly Shareholding']:
            table = fundamentals.get(table_name)
            if table is not None:
                sections.append(f"\n**{table_name}:**")
                try:
                    if hasattr(table, 'to_string'):
                        # DataFrame — convert to string (limit size)
                        table_str = table.to_string()
                        sections.append(table_str[:5000])
                    elif isinstance(table, dict):
                        sections.append(json.dumps(table, indent=2, default=str)[:5000])
                    elif isinstance(table, str):
                        sections.append(table[:5000])
                except Exception as e:
                    sections.append(f"Error rendering table: {e}")
    
    # --- Analyst Recommendations ---
    sections.append("\n### ANALYST RECOMMENDATIONS (from Perplexity)")
    sections.append(str(analyst_recs)[:5000] if analyst_recs else "No analyst data available")
    
    # --- Negative News ---
    sections.append("\n### NEGATIVE NEWS SEARCH (from Perplexity)")
    sections.append(str(negative_news)[:5000] if negative_news else "No negative news data available")
    
    # --- Credit Ratings ---
    sections.append("\n### CREDIT RATING REPORTS")
    credit_ratings = forensic_docs.get('credit_ratings', [])
    if credit_ratings:
        for i, cr in enumerate(credit_ratings):
            sections.append(f"\n**{cr.get('label', f'Credit Rating {i+1}')}:**")
            text = cr.get('text', '')
            sections.append(text[:8000] if text else "No text extracted")
    else:
        sections.append("No credit rating documents found on Screener.in")
    
    # --- Annual Report ---
    sections.append("\n### ANNUAL REPORT EXCERPTS")
    annual = forensic_docs.get('annual_report')
    if annual:
        sections.append(f"**{annual.get('label', 'Annual Report')}:**")
        text = annual.get('text', '')
        sections.append(text[:10000] if text else "No text extracted")
    else:
        sections.append("No annual report found on Screener.in")
    
    return "\n".join(sections)
