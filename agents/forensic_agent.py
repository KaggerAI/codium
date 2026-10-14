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
from calculations.fund_calculations import calculate_altman_zscore, calculate_beneish_mscore


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


def _record_label(row):
    """Row label of a Screener record. reset_index() puts a RangeIndex 'index'
    column first, so the label lives under the '' key, not the first value."""
    if '' in row:
        return row['']
    return next(iter(row.values()), "")


def _is_blank(val):
    """None, '' or NaN - an empty Screener cell."""
    if val is None or val == '':
        return True
    return isinstance(val, float) and math.isnan(val)


def _record_period_keys(row):
    """Period columns of a Screener record, oldest first (label/index excluded)."""
    if '' in row:
        return [k for k in row.keys() if k not in ('', 'index')]
    return list(row.keys())[1:]


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

    # Handle list of records (records format: [{'index': 0, '': 'RowName', 'Mar 2024': 10, ...}, ...])
    if isinstance(table, list):
        for row in table:
            if not isinstance(row, dict):
                continue
            row_label = _record_label(row)
            if row_name.lower() in str(row_label).lower():
                # Blank cells (e.g. a NaN TTM column) are missing, not latest
                values = [row[k] for k in _record_period_keys(row) if not _is_blank(row[k])]
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
    Compute 15 forensic signals from financial data.
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
        
        # 9. Yield on Cash
        cash_yield = _compute_yield_on_cash(fundamentals)
        signals.append(cash_yield)
        
        # 10. Other Income Ratio
        other_inc_ratio = _compute_other_income_ratio(fundamentals)
        signals.append(other_inc_ratio)
        
        # 11. Effective Tax Rate Check
        tax_check = _compute_tax_rate_check(fundamentals)
        signals.append(tax_check)
        
        # 12. Receivables Days Change
        debtor_change = _compute_debtor_days_change(fundamentals)
        signals.append(debtor_change)
        
        # 13. Inventory Days Change
        inventory_change = _compute_inventory_days_change(fundamentals)
        signals.append(inventory_change)
        
        # 14. Asset Turnover Decline
        turnover_decline = _compute_asset_turnover_decline(fundamentals)
        signals.append(turnover_decline)
        
        # 15. Quarterly Sales Decline (YoY)
        q_sales_decline = _compute_quarterly_sales_decline(fundamentals)
        signals.append(q_sales_decline)
        
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
        if not equity_cap:
            equity_cap = _get_table_values(fundamentals, 'Balance Sheet', 'Equity Capital', 1)
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


def _compute_yield_on_cash(fundamentals):
    """Check cash yield: Other Income / Cash & Bank."""
    signal = {'name': 'Yield on Cash', 'value': 'N/A', 'threshold': '<4% when Cash >10% of Assets = 🟡', 'flag': '⚪'}
    try:
        cash = _get_table_values(fundamentals, 'Balance Sheet', 'Cash & Equivalents', 1)
        if not cash:
            cash = _get_table_values(fundamentals, 'Balance Sheet', 'Cash & Bank', 1)
        if not cash:
            cash = _get_table_values(fundamentals, 'Balance Sheet', 'Cash', 1)
        if not cash:
            cash = _get_table_values(fundamentals, 'Balance Sheet', 'Other Assets', 1) # fallback
            
        assets = _get_table_values(fundamentals, 'Balance Sheet', 'Total Assets', 1)
        
        other_income = _get_table_values(fundamentals, 'Annual Results', 'Other Income', 1)
        if not other_income:
            other_income = _get_table_values(fundamentals, 'Profit & Loss', 'Other Income', 1)
            
        if cash and assets and other_income and cash[0] > 0 and assets[0] > 0:
            cash_pct = (cash[0] / assets[0]) * 100
            yield_pct = (other_income[0] / cash[0]) * 100
            signal['value'] = f'{yield_pct:.2f}% (Cash: {cash_pct:.1f}% of assets)'
            if cash_pct > 10:
                if yield_pct < 2.0:
                    signal['flag'] = '🔴'
                elif yield_pct < 4.0:
                    signal['flag'] = '🟡'
                else:
                    signal['flag'] = '🟢'
            else:
                signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_other_income_ratio(fundamentals):
    """Check Other Income / PBT ratio."""
    signal = {'name': 'Other Income / PBT', 'value': 'N/A', 'threshold': '>30% = 🟡, >50% = 🔴', 'flag': '⚪'}
    try:
        other_income = _get_table_values(fundamentals, 'Annual Results', 'Other Income', 1)
        if not other_income:
            other_income = _get_table_values(fundamentals, 'Profit & Loss', 'Other Income', 1)
            
        pbt = _get_table_values(fundamentals, 'Annual Results', 'Profit before tax', 1)
        if not pbt:
            pbt = _get_table_values(fundamentals, 'Profit & Loss', 'Profit before tax', 1)
            
        if other_income and pbt and pbt[0] > 0:
            ratio = (other_income[0] / pbt[0]) * 100
            signal['value'] = f'{ratio:.1f}%'
            if ratio > 50:
                signal['flag'] = '🔴'
            elif ratio > 30:
                signal['flag'] = '🟡'
            else:
                signal['flag'] = '🟢'
        elif other_income and pbt and pbt[0] <= 0:
            signal['value'] = f'Other Income: {other_income[0]} | PBT: {pbt[0]}'
            signal['flag'] = '🟡'
    except Exception:
        pass
    return signal


def _compute_tax_rate_check(fundamentals):
    """Check effective tax rate."""
    signal = {'name': 'Effective Tax Rate', 'value': 'N/A', 'threshold': '<15% = 🟡, <5% = 🔴', 'flag': '⚪'}
    try:
        tax_pct = _get_table_values(fundamentals, 'Annual Results', 'Tax %', 1)
        if not tax_pct:
            tax_pct = _get_table_values(fundamentals, 'Profit & Loss', 'Tax %', 1)
            
        if tax_pct and tax_pct[0] is not None:
            rate = tax_pct[0]
            signal['value'] = f'{rate:.1f}%'
            if rate < 5.0:
                signal['flag'] = '🔴'
            elif rate < 15.0:
                signal['flag'] = '🟡'
            else:
                signal['flag'] = '🟢'
        else:
            tax = _get_table_values(fundamentals, 'Annual Results', 'Tax', 1)
            if not tax:
                tax = _get_table_values(fundamentals, 'Profit & Loss', 'Tax', 1)
            pbt = _get_table_values(fundamentals, 'Annual Results', 'Profit before tax', 1)
            if not pbt:
                pbt = _get_table_values(fundamentals, 'Profit & Loss', 'Profit before tax', 1)
                
            if tax and pbt and pbt[0] > 0:
                rate = (tax[0] / pbt[0]) * 100
                signal['value'] = f'{rate:.1f}%'
                if rate < 5.0:
                    signal['flag'] = '🔴'
                elif rate < 15.0:
                    signal['flag'] = '🟡'
                else:
                    signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_debtor_days_change(fundamentals):
    """Check debtor days YoY change."""
    signal = {'name': 'Debtor Days Change', 'value': 'N/A', 'threshold': 'Increase >20 days = 🟡, >40 = 🔴', 'flag': '⚪'}
    try:
        dd = _get_table_values(fundamentals, 'Financial Ratios', 'Debtor Days', 2)
        if len(dd) >= 2:
            change = dd[0] - dd[1]
            signal['value'] = f'{dd[0]:.0f} days (Change: {change:+.0f} days)'
            if change > 40:
                signal['flag'] = '🔴'
            elif change > 20:
                signal['flag'] = '🟡'
            else:
                signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_inventory_days_change(fundamentals):
    """Check inventory days YoY change."""
    signal = {'name': 'Inventory Days Change', 'value': 'N/A', 'threshold': 'Increase >25 days = 🟡, >50 = 🔴', 'flag': '⚪'}
    try:
        inv = _get_table_values(fundamentals, 'Financial Ratios', 'Inventory Days', 2)
        if len(inv) >= 2:
            change = inv[0] - inv[1]
            signal['value'] = f'{inv[0]:.0f} days (Change: {change:+.0f} days)'
            if change > 50:
                signal['flag'] = '🔴'
            elif change > 25:
                signal['flag'] = '🟡'
            else:
                signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_asset_turnover_decline(fundamentals):
    """Check Asset Turnover trend YoY."""
    signal = {'name': 'Asset Turnover Decline', 'value': 'N/A', 'threshold': 'Drop >15% = 🟡, >30% = 🔴', 'flag': '⚪'}
    try:
        sales = _get_table_values(fundamentals, 'Annual Results', 'Sales', 2)
        assets = _get_table_values(fundamentals, 'Balance Sheet', 'Total Assets', 2)
        if len(sales) >= 2 and len(assets) >= 2:
            turnover0 = _safe_div(sales[0], assets[0])
            turnover1 = _safe_div(sales[1], assets[1])
            if turnover0 is not None and turnover1 is not None and turnover1 > 0:
                pct_change = ((turnover0 - turnover1) / turnover1) * 100
                signal['value'] = f'{turnover0:.2f}x (Change: {pct_change:+.1f}%)'
                if pct_change < -30:
                    signal['flag'] = '🔴'
                elif pct_change < -15:
                    signal['flag'] = '🟡'
                else:
                    signal['flag'] = '🟢'
    except Exception:
        pass
    return signal


def _compute_quarterly_sales_decline(fundamentals):
    """Check Quarterly Sales decline YoY (latest quarter vs 4 quarters ago)."""
    signal = {'name': 'Quarterly Sales Decline (YoY)', 'value': 'N/A', 'threshold': 'Drop >15% = 🟡, >25% = 🔴', 'flag': '⚪'}
    try:
        q_sales = _get_table_values(fundamentals, 'Quarterly Results', 'Sales', 5)
        if len(q_sales) >= 5:
            latest = q_sales[0]
            prior_yoy = q_sales[4]
            if latest is not None and prior_yoy is not None and prior_yoy > 0:
                pct_change = ((latest - prior_yoy) / prior_yoy) * 100
                signal['value'] = f'{pct_change:+.1f}% YoY'
                if pct_change < -25:
                    signal['flag'] = '🔴'
                elif pct_change < -15:
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
    
    # Handle list of records (Screener format) - all quarters, most recent first
    if isinstance(shareholding, list):
        for row in shareholding:
            if isinstance(row, dict) and row_name.lower() in str(_record_label(row)).lower():
                vals = [row[k] for k in _record_period_keys(row)]
                return [_safe_float(v) for v in reversed(vals) if _safe_float(v) is not None]
        return []

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
# BASE DATA (financial statements) — fetched directly, cache as backup
# =====================================================================

# Set by register_forensic_routes, so the watchlist warmer's direct call into
# _run_forensic_analysis resolves base data exactly the way the route does.
_BASE_DATA_DEPS = {'get_cache_fn': None, 'extract_key_metrics_fn': None}

# Without these two there is nothing to audit (M-Score, Z-Score, most signals).
_REQUIRED_TABLES = ('Balance Sheet', 'Annual Results')


def _tables_to_records(tables):
    """Screener DataFrames -> list-of-records, identical to the AI-context
    format get_analysis_for_ticker_async caches (handler.py fund_data_for_ai_context)."""
    records = {}
    for table_name, df in (tables or {}).items():
        rows = []
        for row_dict in df.reset_index().to_dict(orient='records'):
            rows.append({(str(k).strip() if isinstance(k, str) else k): (str(v).strip() if isinstance(v, str) else v)
                         for k, v in row_dict.items()})
        records[table_name] = rows
    return records


def _split_json_to_records(table_json):
    """Convert a frontend-cache table (df.reset_index().T.to_json(orient='split'))
    back to list-of-records. Parsed by hand so period labels like 'Mar 2024' are
    never coerced to dates."""
    obj = json.loads(table_json)
    columns = obj.get('index') or []   # original column names ('index', '', 'Mar 2024', ...)
    data = obj.get('data') or []       # data[i][j] = value of column i for row j
    if not columns or len(data) != len(columns):
        return []
    n_rows = len(data[0]) if data else 0
    rows = []
    for j in range(n_rows):
        rows.append({str(columns[i]).strip(): (data[i][j].strip() if isinstance(data[i][j], str) else data[i][j])
                     for i in range(len(columns))})
    return rows


def _has_required_tables(fundamentals):
    if not isinstance(fundamentals, dict):
        return False
    for name in _REQUIRED_TABLES:
        table = fundamentals.get(name)
        if not isinstance(table, list) or not table or not isinstance(table[0], dict):
            return False
    return True


def _normalize_base_data(cached_data):
    """Return a copy of a cached stock payload whose fundamentals are in
    list-of-records format, or None if it has nothing the agent can audit.

    The local cache holds records already; the Redis stock_analysis_* payload
    (and anything seeded from it) holds split-JSON strings, which every
    forensic parser silently reads as empty.
    """
    if not isinstance(cached_data, dict):
        return None
    fundamentals = cached_data.get('fundamentals')
    if isinstance(fundamentals, str):
        try:
            fundamentals = json.loads(fundamentals)
        except json.JSONDecodeError:
            return None
    if not isinstance(fundamentals, dict):
        return None

    normalized = {}
    for table_name, table in fundamentals.items():
        if isinstance(table, str):
            try:
                table = _split_json_to_records(table)
            except (json.JSONDecodeError, TypeError, AttributeError, IndexError):
                continue
        normalized[table_name] = table

    if not _has_required_tables(normalized):
        return None
    data = dict(cached_data)
    data['fundamentals'] = normalized
    return data


async def fetch_forensic_base_data_async(ticker):
    """
    Fetch only what the Forensic Agent audits: Screener's financial statements,
    top ratios (market cap etc.), consolidation status and company name.

    Deliberately independent of the Company Research pipeline — no price feed,
    charts, peers, valuations or AI summaries — so none of those can fail it.
    Raises ValueError when Screener returns no usable statements.
    """
    from fetchers.screener_fetcher import fetch_consolidated_async

    tables, _description, top_ratios, is_consolidated, html = await fetch_consolidated_async(ticker, return_html=True)
    fundamentals = _tables_to_records(tables)
    if not _has_required_tables(fundamentals):
        raise ValueError(f"Screener returned no financial statements for {ticker}")

    company_name = None
    if html:
        try:
            from bs4 import BeautifulSoup
            h1 = BeautifulSoup(html, 'html.parser').find('h1')
            company_name = h1.get_text(strip=True) if h1 else None
        except Exception as e:
            print(f"FORENSIC_AGENT: Company name parse failed for {ticker}: {e}", file=sys.stderr)
    if not company_name:
        company_name = await _yfinance_company_name(ticker)

    extract_key_metrics_fn = _BASE_DATA_DEPS.get('extract_key_metrics_fn')
    key_metrics = {}
    if extract_key_metrics_fn:
        try:
            key_metrics = extract_key_metrics_fn(fundamentals, top_ratios=top_ratios) or {}
        except Exception as e:
            print(f"FORENSIC_AGENT: Key metrics extraction failed for {ticker}: {e}", file=sys.stderr)
    if not key_metrics.get('market_cap') and top_ratios.get('Market Cap'):
        key_metrics['market_cap'] = top_ratios['Market Cap']

    return {
        'ticker': ticker,
        'company_name': company_name or ticker,
        'fundamentals': fundamentals,
        'key_metrics': key_metrics,
        'is_consolidated': is_consolidated,
    }


async def _yfinance_company_name(ticker):
    """Best-effort company name; never fails the run."""
    def _lookup():
        import yfinance as yf
        for suffix in ('.NS', '.BO'):
            info = yf.Ticker(f"{ticker}{suffix}").info or {}
            name = (info.get('longName') or info.get('shortName') or '').strip()
            if name:
                return name
        return None
    try:
        return await asyncio.wait_for(asyncio.to_thread(_lookup), timeout=15)
    except Exception as e:
        print(f"FORENSIC_AGENT: yfinance name lookup failed for {ticker}: {e}", file=sys.stderr)
        return None


def _resolve_base_data(ticker):
    """
    Return (base_data, source) where source is 'live' or 'cache'.

    Live Screener fetch first, so the audit always covers the latest filing.
    The shared stock cache is only a backup for when Screener is unreachable,
    and is read-only here: a forensic-only payload must never overwrite the
    richer one Company Research and the chatbot rely on.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(fetch_forensic_base_data_async(ticker)), 'live'
    except Exception as e:
        live_err = e
        print(f"FORENSIC_AGENT: Live base data fetch failed for {ticker}: {e}", file=sys.stderr)
    finally:
        loop.close()

    get_cache_fn = _BASE_DATA_DEPS.get('get_cache_fn')
    if get_cache_fn:
        try:
            cached = _normalize_base_data(get_cache_fn(ticker))
            if cached:
                print(f"FORENSIC_AGENT: Using cached financial statements for {ticker}", file=sys.stderr)
                return cached, 'cache'
        except Exception as e:
            print(f"FORENSIC_AGENT: Cache lookup failed for {ticker}: {e}", file=sys.stderr)

    raise ValueError(f"{FORENSIC_NO_DATA_MSG} (Reason: {live_err})")


# =====================================================================
# ROUTE REGISTRATION
# =====================================================================

def register_forensic_routes(app, call_gemini_api_fn, call_perplexity_api_fn,
                              get_cache_fn, fetch_forensic_docs_fn, extract_key_metrics_fn=None):
    """
    Register all Forensic Agent API routes with the Flask app.

    Args:
        app: Flask app instance
        call_gemini_api_fn: Reference to call_gemini_api from handler.py
        call_perplexity_api_fn: Reference to call_perplexity_api from handler.py
        get_cache_fn: Reference to get_any_cache from handler.py (backup when Screener is unreachable)
        fetch_forensic_docs_fn: Reference to fetch_forensic_documents_async from screener_fetcher
        extract_key_metrics_fn: Reference to extract_key_metrics_from_fundamentals from handler.py
    """
    _BASE_DATA_DEPS['get_cache_fn'] = get_cache_fn
    _BASE_DATA_DEPS['extract_key_metrics_fn'] = extract_key_metrics_fn

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

            # Create background job. Financial statements are fetched inside the
            # job (live from Screener, cache as backup), so no prior Company
            # Research run or cache is needed and this request returns at once.
            job_id = create_agent_job('forensic', ticker)

            # Start background analysis thread
            thread = threading.Thread(
                target=_run_forensic_analysis,
                args=(job_id, ticker, None, call_gemini_api_fn,
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
            })

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
                model="gemini-3.5-flash-lite",
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
    1. Load financial statements (cached_data if given, else live fetch / cache backup)
    2. Fetch fresh data in parallel (news, analyst recs, credit ratings, annual report)
    3. Compute quantitative scores (M-Score, Z-Score, signals)
    4. Send everything to Gemini for structured analysis
    5. Store result
    """
    start_time = time.time()

    try:
        # =====================================================
        # STEP 1: Load financial statements
        # =====================================================
        update_agent_job(job_id, {'progress': 'Fetching financial statements...'})
        print(f"FORENSIC_AGENT: Step 1 - Loading financial statements for {ticker}", file=sys.stderr)

        if cached_data is None:
            cached_data, base_source = _resolve_base_data(ticker)
        else:
            base_source = 'cache'
            cached_data = _normalize_base_data(cached_data)
            if cached_data is None:
                raise ValueError(FORENSIC_NO_DATA_MSG)

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
        print(f"FORENSIC_AGENT: Step 1 complete - Financial statements ({base_source}) loaded in {elapsed_step1}s", file=sys.stderr)

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
            analyst_recs, negative_news, forensic_docs, sebi_filings = loop.run_until_complete(
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
            mscore, zscore, signals, analyst_recs, negative_news, forensic_docs, sebi_filings
        )

        analysis_prompt = f"""{FORENSIC_ANALYSIS_PROMPT}

## Company: {company_name} ({ticker})

## Data Package:
{data_context}
"""

        messages = [{"role": "user", "content": analysis_prompt}]

        analysis_result = call_gemini_api_fn(
            messages,
            model="gemini-3.5-flash-lite",
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
                'has_annual_report': forensic_docs.get('annual_report') is not None,
                'sebi_filings_len': len(sebi_filings) if sebi_filings else 0,
                'financials_source': base_source
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
    
    async def get_sebi_reg30_filings():
        """Fetch SEBI Regulation 30 LODR material disclosures via Perplexity."""
        try:
            query = f"""Search for any material disclosures or corporate announcements filed by
{company_name} ({ticker}) with BSE/NSE India under SEBI LODR Regulation 30 in the last 6 months.

Include ANY of the following if found:
- Mergers, acquisitions, amalgamations, demergers, joint ventures
- Defaults on debt/interest/loan repayment obligations
- Key management changes (CEO, CFO, MD, Whole-Time Director, Company Secretary, Auditor appointments/resignations)
- Fund-raising (QIP, rights issue, preferential allotment, buyback)
- Major litigation, arbitration, or regulatory penalties
- Related party transactions requiring shareholder approval
- Restructuring, winding up, or change in business scope
- SEBI show-cause notices, investigation orders, or debarment

For each event found, include: date, nature of event, brief summary.
If no material filings found, say "No significant Regulation 30 filings found in the last 6 months."
Be factual and cite sources."""

            result = await asyncio.to_thread(
                call_perplexity_api_fn,
                [{"role": "user", "content": query}],
                "sonar-pro", 0.1, 60
            )
            return result
        except Exception as e:
            print(f"FORENSIC_AGENT: SEBI Reg 30 fetch failed: {e}", file=sys.stderr)
            return f"Error fetching SEBI Regulation 30 filings: {e}"
    
    # Run all four in parallel
    results = await asyncio.gather(
        get_analyst_recs(),
        get_negative_news(),
        get_forensic_docs(),
        get_sebi_reg30_filings(),
        return_exceptions=True
    )
    
    analyst_recs = results[0] if not isinstance(results[0], Exception) else f"Error: {results[0]}"
    negative_news = results[1] if not isinstance(results[1], Exception) else f"Error: {results[1]}"
    forensic_docs = results[2] if not isinstance(results[2], Exception) else {'credit_ratings': [], 'annual_report': None}
    sebi_filings = results[3] if not isinstance(results[3], Exception) else f"Error: {results[3]}"
    
    return analyst_recs, negative_news, forensic_docs, sebi_filings


def _records_to_text(rows):
    """Render a list-of-records table as compact pipe-separated rows."""
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return "No data"
    periods = _record_period_keys(rows[0])
    lines = [" | ".join(["Metric"] + [str(p) for p in periods])]
    for row in rows:
        values = ["" if _is_blank(row.get(p)) else str(row.get(p)) for p in periods]
        lines.append(" | ".join([str(_record_label(row))] + values))
    return "\n".join(lines)


def _build_gemini_context(ticker, company_name, fundamentals, key_metrics,
                           mscore, zscore, signals, analyst_recs, negative_news, forensic_docs, sebi_filings):
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
    sections.append("\n### FINANCIAL STATEMENTS")
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
                    elif isinstance(table, list):
                        sections.append(_records_to_text(table)[:5000])
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
    
    # --- SEBI Regulation 30 Material Disclosures ---
    sections.append("\n### SEBI REGULATION 30 MATERIAL DISCLOSURES (from Perplexity)")
    sections.append(str(sebi_filings)[:6000] if sebi_filings else "No material Regulation 30 disclosures available")
    
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
