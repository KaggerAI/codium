"""
forecasting_agent.py — Professional-Grade Multi-Method Valuation Engine for Kagger AI.

Computes 12 valuation models (DCF FCFF, DCF FCFE, DDM Gordon, DDM Multi-Stage,
P/E, P/B, EV/EBITDA, MCap/Sales, Residual Income, NAV, Precedent Transactions,
Statistical Band), intelligently selects applicable models, performs AI-driven
triangulation, and provides real-time recalculation for interactive slider UI.
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
from agents.prompts.forecasting_prompts import (
    FORECAST_ASSUMPTIONS_PROMPT, FORECAST_RESEARCH_PROMPT,
    FORECAST_TRIANGULATION_PROMPT, FORECAST_CHAT_PROMPT,
    FORECAST_NO_DATA_MSG, FORECAST_GUIDANCE_EXTRACTION_PROMPT
)


# =====================================================================
# UTILITY FUNCTIONS
# =====================================================================

def _safe_float(val, default=None):
    """Safely convert a value to float."""
    if val is None:
        return default
    try:
        result = float(str(val).replace(',', '').replace('%', '').replace('₹', '').replace('Cr.', '').strip())
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _safe_div(a, b, default=None):
    """Safe division handling None, NaN, zero."""
    try:
        a_val = float(a) if a is not None else None
        b_val = float(b) if b is not None else None
        if a_val is None or b_val is None or b_val == 0:
            return default
        result = a_val / b_val
        if math.isnan(result) or math.isinf(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _get_table_values(fundamentals, table_name, row_name, num_years=5):
    """
    Extract values for a specific row from a fundamentals table.
    Returns a list of floats (most recent first).
    """
    table = fundamentals.get(table_name)
    if table is None:
        return []

    if isinstance(table, str):
        try:
            table = json.loads(table)
        except Exception:
            return []

    # Handle DataFrame
    if hasattr(table, 'to_dict') and not isinstance(table, (dict, list)):
        try:
            df = table
            matching_rows = [idx for idx in df.index if row_name.lower() in str(idx).lower()]
            if not matching_rows:
                return []
            row = df.loc[matching_rows[0]]
            values = row.values[-num_years:] if len(row) >= num_years else row.values
            return [_safe_float(v) for v in reversed(values)]
        except Exception:
            return []

    # Handle list of records
    if isinstance(table, list):
        for row in table:
            if not isinstance(row, dict):
                continue
            row_label = next(iter(row.values()), "")
            if row_name.lower() in str(row_label).lower():
                val_keys = list(row.keys())[1:]
                values = [row[k] for k in val_keys if row[k] is not None]
                recent = values[-num_years:] if len(values) >= num_years else values
                return [_safe_float(v) for v in reversed(recent)]

    # Handle dict format
    if isinstance(table, dict):
        # Check for 'data' key (common serialized format)
        if 'data' in table and 'index' in table:
            try:
                index = table.get('index', [])
                data = table.get('data', [])
                columns = table.get('columns', [])
                for i, idx_name in enumerate(index):
                    if row_name.lower() in str(idx_name).lower():
                        if i < len(data):
                            row_data = data[i]
                            recent = row_data[-num_years:] if len(row_data) >= num_years else row_data
                            return [_safe_float(v) for v in reversed(recent)]
            except Exception:
                pass
        
        for key, values in table.items():
            if row_name.lower() in key.lower():
                if isinstance(values, list):
                    cleaned = [v for v in values if v is not None]
                    recent = cleaned[-num_years:] if len(cleaned) >= num_years else cleaned
                    return [_safe_float(v) for v in reversed(recent)]
                elif isinstance(values, dict):
                    sorted_vals = sorted(values.items(), reverse=True)[:num_years]
                    return [_safe_float(v) for _, v in sorted_vals]

    return []


# =====================================================================
# VALUATION MODEL IMPLEMENTATIONS (Pure Python — No AI Calls)
# =====================================================================

def compute_dcf_fcff(assumptions, scenario='base'):
    """
    Discounted Cash Flow — Free Cash Flow to Firm (FCFF).
    Projects revenue, computes FCFF, discounts at WACC.
    """
    try:
        dcf = assumptions.get('dcf_assumptions', {})
        wacc_comp = assumptions.get('wacc_components', {})
        rel = assumptions.get('relative_valuation', {})

        # Get scenario-specific values
        def get_val(d, key, scen=scenario):
            v = d.get(key, 0)
            if isinstance(v, dict):
                return v.get(scen, v.get('base', 0))
            return v

        # Revenue growth rates for 5 years
        growth_rates = [
            get_val(dcf, 'revenue_growth_y1') / 100
        ] + [
            get_val(dcf, 'revenue_growth_y2_to_y5') / 100
        ] * 4

        ebitda_margin = get_val(dcf, 'ebitda_margin') / 100
        da_pct = get_val(dcf, 'da_pct_of_revenue', scenario) / 100 if isinstance(dcf.get('da_pct_of_revenue'), dict) else dcf.get('da_pct_of_revenue', 4) / 100
        capex_pct = get_val(dcf, 'capex_pct_of_revenue', scenario) / 100 if isinstance(dcf.get('capex_pct_of_revenue'), dict) else dcf.get('capex_pct_of_revenue', 7) / 100
        tax_rate = dcf.get('tax_rate', 25) / 100
        wc_pct = dcf.get('working_capital_pct_of_revenue', 10) / 100
        terminal_growth = get_val(dcf, 'terminal_growth') / 100

        # WACC calculation
        rf = wacc_comp.get('risk_free_rate', 7) / 100
        erp = get_val(wacc_comp, 'equity_risk_premium') / 100
        beta = get_val(wacc_comp, 'beta')
        cost_of_debt_pretax = wacc_comp.get('cost_of_debt_pretax', 9) / 100
        debt_pct = wacc_comp.get('debt_to_total_capital', 30) / 100
        equity_pct = 1 - debt_pct

        cost_of_equity = rf + beta * erp
        cost_of_debt_posttax = cost_of_debt_pretax * (1 - tax_rate)
        wacc = equity_pct * cost_of_equity + debt_pct * cost_of_debt_posttax

        # Sanity guard: perpetual terminal growth cannot exceed the risk-free
        # rate (a long-run GDP/nominal-growth proxy). Clamp rather than error.
        terminal_growth_capped = False
        if terminal_growth >= rf:
            terminal_growth = max(0.0, rf - 0.005)
            terminal_growth_capped = True

        if wacc <= terminal_growth or wacc <= 0:
            return {'fair_value': None, 'error': 'WACC must exceed terminal growth rate'}

        # Base off TRAILING (current) revenue and project forward exactly once.
        # Using forward_revenue as the base double-counts Y1 growth (forward is
        # already next-year revenue). Fall back to forward only if trailing absent.
        base_revenue = get_val(rel, 'trailing_revenue_cr')
        if not base_revenue or base_revenue <= 0:
            base_revenue = get_val(rel, 'forward_revenue_cr')
        if not base_revenue or base_revenue <= 0:
            return {'fair_value': None, 'error': 'No base revenue available'}

        net_debt = get_val(rel, 'net_debt_cr')
        if net_debt is None:
            net_debt = 0
        shares = get_val(rel, 'shares_outstanding_cr')
        if not shares or shares <= 0:
            return {'fair_value': None, 'error': 'No shares outstanding data'}

        # Project FCFF for 5 years
        projected_fcff = []
        revenue = base_revenue
        prev_revenue = base_revenue

        for i, g in enumerate(growth_rates):
            revenue = prev_revenue * (1 + g) if i > 0 else base_revenue * (1 + g)
            ebitda = revenue * ebitda_margin
            da = revenue * da_pct
            ebit = ebitda - da
            nopat = ebit * (1 - tax_rate)
            capex = revenue * capex_pct
            delta_wc = (revenue - prev_revenue) * wc_pct
            fcff = nopat + da - capex - delta_wc
            projected_fcff.append(fcff)
            prev_revenue = revenue

        # Terminal Value
        terminal_fcff = projected_fcff[-1] * (1 + terminal_growth)
        terminal_value = terminal_fcff / (wacc - terminal_growth)

        # Discount all cash flows
        pv_fcff = sum(
            fcff / ((1 + wacc) ** (i + 1))
            for i, fcff in enumerate(projected_fcff)
        )
        pv_terminal = terminal_value / ((1 + wacc) ** 5)

        enterprise_value = pv_fcff + pv_terminal
        equity_value = enterprise_value - net_debt
        fair_value_per_share = max(0.0, equity_value / shares)

        return {
            'fair_value': round(fair_value_per_share, 2),
            'enterprise_value': round(enterprise_value, 2),
            'equity_value': round(equity_value, 2),
            'wacc': round(wacc * 100, 2),
            'cost_of_equity': round(cost_of_equity * 100, 2),
            'terminal_value_pct': round(pv_terminal / enterprise_value * 100, 1) if enterprise_value > 0 else 0,
            'projected_fcff': [round(f, 2) for f in projected_fcff],
            'terminal_growth_capped': terminal_growth_capped,
            'scenario': scenario
        }
    except Exception as e:
        return {'fair_value': None, 'error': str(e)}


def compute_dcf_fcfe(assumptions, scenario='base'):
    """
    DCF — Free Cash Flow to Equity. Discounts at Cost of Equity.

    Net income is grown from TRAILING EPS at the model's growth rates (the FCFE
    sliders are labelled "Earnings Growth"); the equity-funded portion of net
    reinvestment is then deducted. Reinvestment is sized off a revenue projection
    that grows at the same rate — i.e. a constant-margin simplification (margins
    are modelled explicitly in FCFF). NI and revenue are advanced together within
    each year, so they stay in sync.
    """
    try:
        dcf = assumptions.get('dcf_assumptions', {})
        wacc_comp = assumptions.get('wacc_components', {})
        rel = assumptions.get('relative_valuation', {})

        def get_val(d, key, scen=scenario):
            v = d.get(key, 0)
            if isinstance(v, dict):
                return v.get(scen, v.get('base', 0))
            return v

        # Cost of Equity
        rf = wacc_comp.get('risk_free_rate', 7) / 100
        erp = get_val(wacc_comp, 'equity_risk_premium') / 100
        beta = get_val(wacc_comp, 'beta')
        coe = rf + beta * erp
        terminal_growth = get_val(dcf, 'terminal_growth') / 100

        # Sanity guard: perpetual terminal growth cannot exceed the risk-free rate.
        terminal_growth_capped = False
        if terminal_growth >= rf:
            terminal_growth = max(0.0, rf - 0.005)
            terminal_growth_capped = True

        if coe <= terminal_growth or coe <= 0:
            return {'fair_value': None, 'error': 'CoE must exceed terminal growth'}

        shares = get_val(rel, 'shares_outstanding_cr')
        if not shares or shares <= 0:
            return {'fair_value': None, 'error': 'No shares outstanding'}

        # Base earnings off TRAILING EPS (current actual) and grow ONCE — using
        # forward EPS as the base double-counts Y1 growth.
        trailing_eps = get_val(rel, 'trailing_eps')
        base_eps = trailing_eps if (trailing_eps and trailing_eps > 0) else get_val(rel, 'forward_eps')
        if not base_eps or base_eps <= 0:
            return {'fair_value': None, 'error': 'No EPS available'}

        # Growth rates (Y1, then Y2-Y5 average for 4 years)
        growth_rates = [
            get_val(dcf, 'revenue_growth_y1') / 100
        ] + [
            get_val(dcf, 'revenue_growth_y2_to_y5') / 100
        ] * 4

        # Proper FCFE: Net Income less the equity-funded portion of net
        # reinvestment (CapEx + ΔWC − D&A). Debt funds a `debt_ratio` share of
        # reinvestment, so equity funds (1 − debt_ratio). Reuses the same
        # revenue-driven CapEx/D&A/WC assumptions as FCFF for consistency.
        debt_ratio = wacc_comp.get('debt_to_total_capital', 30) / 100
        da_pct = get_val(dcf, 'da_pct_of_revenue', scenario) / 100 if isinstance(dcf.get('da_pct_of_revenue'), dict) else dcf.get('da_pct_of_revenue', 5) / 100
        capex_pct = get_val(dcf, 'capex_pct_of_revenue', scenario) / 100 if isinstance(dcf.get('capex_pct_of_revenue'), dict) else dcf.get('capex_pct_of_revenue', 7) / 100
        wc_pct = dcf.get('working_capital_pct_of_revenue', 10) / 100

        trailing_revenue = get_val(rel, 'trailing_revenue_cr')
        if not trailing_revenue or trailing_revenue <= 0:
            fwd_rev = get_val(rel, 'forward_revenue_cr')
            g1 = growth_rates[0]
            trailing_revenue = (fwd_rev / (1 + g1)) if (fwd_rev and (1 + g1) != 0) else 0

        net_income_0 = base_eps * shares
        projected_fcfe = []
        current_ni = net_income_0
        prev_revenue = trailing_revenue
        revenue = trailing_revenue
        for g in growth_rates:
            current_ni = current_ni * (1 + g)
            if trailing_revenue and trailing_revenue > 0:
                revenue = prev_revenue * (1 + g)
                da = revenue * da_pct
                capex = revenue * capex_pct
                delta_wc = (revenue - prev_revenue) * wc_pct
                net_reinvestment = (capex + delta_wc - da)
                fcfe = current_ni - (1 - debt_ratio) * net_reinvestment
                prev_revenue = revenue
            else:
                # No revenue base — fall back to reinvestment-rate proxy.
                reinvestment_rate = get_val(dcf, 'reinvestment_rate', scenario) / 100 if isinstance(dcf.get('reinvestment_rate'), dict) else dcf.get('reinvestment_rate', 50) / 100
                fcfe = current_ni * (1 - reinvestment_rate)
            projected_fcfe.append(max(0.0, fcfe))

        # Terminal Value
        terminal_fcfe = projected_fcfe[-1] * (1 + terminal_growth)
        terminal_value = terminal_fcfe / (coe - terminal_growth)

        pv_fcfe = sum(
            f / ((1 + coe) ** (i + 1))
            for i, f in enumerate(projected_fcfe)
        )
        pv_terminal = terminal_value / ((1 + coe) ** 5)

        equity_value = pv_fcfe + pv_terminal
        fair_value = max(0.0, equity_value / shares)

        return {
            'fair_value': round(fair_value, 2),
            'equity_value': round(equity_value, 2),
            'cost_of_equity': round(coe * 100, 2),
            'terminal_growth_capped': terminal_growth_capped,
            'scenario': scenario
        }
    except Exception as e:
        return {'fair_value': None, 'error': str(e)}


def compute_ddm_gordon(assumptions, scenario='base'):
    """
    Dividend Discount Model — Gordon Growth Model.
    Fair Value = DPS × (1 + g) / (CoE - g)
    """
    try:
        ddm = assumptions.get('ddm_assumptions', {})
        wacc_comp = assumptions.get('wacc_components', {})

        if not ddm.get('applicable', False):
            return {'fair_value': None, 'error': 'DDM not applicable', 'skipped': True}

        def get_val(d, key, scen=scenario):
            v = d.get(key, 0)
            if isinstance(v, dict):
                return v.get(scen, v.get('base', 0))
            return v

        dps = ddm.get('current_dps', 0)
        if not dps or dps <= 0:
            return {'fair_value': None, 'error': 'No dividends', 'skipped': True}

        g = get_val(ddm, 'dividend_growth_stable') / 100

        rf = wacc_comp.get('risk_free_rate', 7) / 100
        erp = get_val(wacc_comp, 'equity_risk_premium') / 100
        beta = get_val(wacc_comp, 'beta')
        coe = rf + beta * erp

        if coe <= g:
            return {'fair_value': None, 'error': 'CoE must exceed dividend growth rate'}

        # Calculate DPS value but cap logic to prevent negative
        fair_value = max(0.0, (dps * (1 + g)) / (coe - g))

        return {
            'fair_value': round(fair_value, 2),
            'dps': dps,
            'growth_rate': round(g * 100, 2),
            'cost_of_equity': round(coe * 100, 2),
            'scenario': scenario
        }
    except Exception as e:
        return {'fair_value': None, 'error': str(e)}


def compute_ddm_multistage(assumptions, scenario='base'):
    """
    Multi-Stage DDM — High-growth phase + stable phase.
    """
    try:
        ddm = assumptions.get('ddm_assumptions', {})
        wacc_comp = assumptions.get('wacc_components', {})

        if not ddm.get('applicable', False):
            return {'fair_value': None, 'error': 'DDM not applicable', 'skipped': True}

        def get_val(d, key, scen=scenario):
            v = d.get(key, 0)
            if isinstance(v, dict):
                return v.get(scen, v.get('base', 0))
            return v

        dps = ddm.get('current_dps', 0)
        if not dps or dps <= 0:
            return {'fair_value': None, 'error': 'No dividends', 'skipped': True}

        g_high = get_val(ddm, 'dividend_growth_high') / 100
        g_stable = get_val(ddm, 'dividend_growth_stable') / 100
        high_years = ddm.get('high_growth_years', 5)

        rf = wacc_comp.get('risk_free_rate', 7) / 100
        erp = get_val(wacc_comp, 'equity_risk_premium') / 100
        beta = get_val(wacc_comp, 'beta')
        coe = rf + beta * erp

        if coe <= g_stable:
            return {'fair_value': None, 'error': 'CoE must exceed stable growth rate'}

        # Phase 1: High growth dividends
        pv_dividends = 0
        current_dps = dps
        for year in range(1, high_years + 1):
            current_dps = current_dps * (1 + g_high)
            pv_dividends += current_dps / ((1 + coe) ** year)

        # Phase 2: Terminal value (Gordon Growth on stable growth)
        terminal_dps = current_dps * (1 + g_stable)
        terminal_value = terminal_dps / (coe - g_stable)
        pv_terminal = terminal_value / ((1 + coe) ** high_years)

        fair_value = max(0.0, pv_dividends + pv_terminal)

        return {
            'fair_value': round(fair_value, 2),
            'pv_high_growth': round(pv_dividends, 2),
            'pv_terminal': round(pv_terminal, 2),
            'scenario': scenario
        }
    except Exception as e:
        return {'fair_value': None, 'error': str(e)}


def compute_relative_pe(assumptions, scenario='base'):
    """Relative Valuation — P/E Multiple."""
    try:
        rel = assumptions.get('relative_valuation', {})

        def get_val(d, key, scen=scenario):
            v = d.get(key, 0)
            if isinstance(v, dict):
                return v.get(scen, v.get('base', 0))
            return v

        forward_eps = get_val(rel, 'forward_eps')
        peer_pe = get_val(rel, 'peer_median_pe')

        if not forward_eps or not peer_pe:
            return {'fair_value': None, 'error': 'Missing EPS or P/E data'}

        fair_value = max(0.0, forward_eps * peer_pe)

        return {
            'fair_value': round(fair_value, 2),
            'forward_eps': forward_eps,
            'peer_pe': peer_pe,
            'scenario': scenario
        }
    except Exception as e:
        return {'fair_value': None, 'error': str(e)}


def compute_relative_pb(assumptions, scenario='base'):
    """Relative Valuation — P/B Multiple."""
    try:
        rel = assumptions.get('relative_valuation', {})
        def get_val(d, key, scen=scenario):
            v = d.get(key, 0)
            if isinstance(v, dict):
                return v.get(scen, v.get('base', 0))
            return v

        bvps = get_val(rel, 'forward_bvps')
        peer_pb = get_val(rel, 'peer_median_pb')

        if not bvps or not peer_pb:
            return {'fair_value': None, 'error': 'Missing BVPS or P/B data'}

        fair_value = max(0.0, bvps * peer_pb)

        return {
            'fair_value': round(fair_value, 2),
            'bvps': bvps,
            'peer_pb': peer_pb,
            'scenario': scenario
        }
    except Exception as e:
        return {'fair_value': None, 'error': str(e)}


def compute_ev_ebitda(assumptions, scenario='base'):
    """Relative Valuation — EV/EBITDA Multiple."""
    try:
        rel = assumptions.get('relative_valuation', {})

        def get_val(d, key, scen=scenario):
            v = d.get(key, 0)
            if isinstance(v, dict):
                return v.get(scen, v.get('base', 0))
            return v

        forward_ebitda = get_val(rel, 'forward_ebitda_cr')
        peer_ev_ebitda = get_val(rel, 'peer_median_ev_ebitda')
        net_debt = get_val(rel, 'net_debt_cr')
        if net_debt is None:
            net_debt = 0
        shares = get_val(rel, 'shares_outstanding_cr')

        if not forward_ebitda or not peer_ev_ebitda or not shares:
            return {'fair_value': None, 'error': 'Missing EBITDA, multiple, or shares data'}

        enterprise_value = forward_ebitda * peer_ev_ebitda
        equity_value = enterprise_value - net_debt
        fair_value = max(0.0, equity_value / shares)

        return {
            'fair_value': round(fair_value, 2),
            'enterprise_value': round(enterprise_value, 2),
            'equity_value': round(equity_value, 2),
            'forward_ebitda': forward_ebitda,
            'peer_ev_ebitda': peer_ev_ebitda,
            'scenario': scenario
        }
    except Exception as e:
        return {'fair_value': None, 'error': str(e)}


def compute_mcap_sales(assumptions, scenario='base'):
    """Relative Valuation — Market Cap / Sales."""
    try:
        rel = assumptions.get('relative_valuation', {})

        def get_val(d, key, scen=scenario):
            v = d.get(key, 0)
            if isinstance(v, dict):
                return v.get(scen, v.get('base', 0))
            return v

        forward_revenue = get_val(rel, 'forward_revenue_cr')
        peer_mcap_sales = get_val(rel, 'peer_median_mcap_sales')
        shares = get_val(rel, 'shares_outstanding_cr')

        if not forward_revenue or not peer_mcap_sales or not shares:
            return {'fair_value': None, 'error': 'Missing revenue, multiple, or shares data'}

        implied_mcap = forward_revenue * peer_mcap_sales
        fair_value = max(0.0, implied_mcap / shares)

        return {
            'fair_value': round(fair_value, 2),
            'implied_mcap': round(implied_mcap, 2),
            'forward_revenue': forward_revenue,
            'peer_mcap_sales': peer_mcap_sales,
            'scenario': scenario
        }
    except Exception as e:
        return {'fair_value': None, 'error': str(e)}


def compute_residual_income(assumptions, scenario='base'):
    """Residual Income Model — best for banks, NBFCs, insurance."""
    try:
        ri = assumptions.get('residual_income_assumptions', {})

        if not ri.get('applicable', False):
            return {'fair_value': None, 'error': 'RI model not applicable', 'skipped': True}

        def get_val(d, key, scen=scenario):
            v = d.get(key, 0)
            if isinstance(v, dict):
                return v.get(scen, v.get('base', 0))
            return v

        bvps = ri.get('book_value_per_share', 0)
        roe = get_val(ri, 'roe_forecast') / 100
        coe = get_val(ri, 'cost_of_equity') / 100
        fade_years = ri.get('excess_return_fade_years', 10)

        # Book value grows only by RETAINED earnings (1 - payout), not full ROE.
        payout = ri.get('payout_ratio_current')
        if payout is None:
            payout = assumptions.get('ddm_assumptions', {}).get('payout_ratio_current', 0)
        retention = 1 - (_safe_float(payout, 0) or 0) / 100
        retention = max(0.0, min(1.0, retention))

        if not bvps or coe <= 0:
            return {'fair_value': None, 'error': 'Missing BVPS or CoE data'}

        # RI = BV + sum of (excess_return * BV_t / (1+CoE)^t)
        excess_return = roe - coe
        if excess_return <= 0:
            # No excess return — fair value = book value
            return {
                'fair_value': round(bvps, 2),
                'excess_return': 0,
                'scenario': scenario
            }

        pv_excess = 0
        current_bv = bvps
        for year in range(1, fade_years + 1):
            # Fade factor — excess return fades linearly to 0
            fade = max(0, 1 - (year - 1) / fade_years)
            ri_year = current_bv * excess_return * fade
            pv_excess += ri_year / ((1 + coe) ** year)
            # BV grows only by retained earnings at the (fading) ROE
            current_bv = current_bv * (1 + roe * fade * retention)

        fair_value = max(0.0, bvps + pv_excess)

        return {
            'fair_value': round(fair_value, 2),
            'book_value': bvps,
            'excess_return': round(excess_return * 100, 2),
            'roe': round(roe * 100, 2),
            'coe': round(coe * 100, 2),
            'scenario': scenario
        }
    except Exception as e:
        return {'fair_value': None, 'error': str(e)}


def compute_nav(assumptions, scenario='base'):
    """Net Asset Value — for holding companies, REITs."""
    try:
        nav = assumptions.get('nav_assumptions', {})

        if not nav.get('applicable', False):
            return {'fair_value': None, 'error': 'NAV not applicable', 'skipped': True}

        def get_val(d, key, scen=scenario):
            v = d.get(key, 0)
            if isinstance(v, dict):
                return v.get(scen, v.get('base', 0))
            return v

        total_assets = nav.get('total_assets_cr', 0)
        total_liabilities = nav.get('total_liabilities_cr', 0)
        shares = get_val(assumptions.get('relative_valuation', {}), 'shares_outstanding_cr')
        revaluation = get_val(nav, 'asset_revaluation_pct') / 100

        if not total_assets or not shares:
            return {'fair_value': None, 'error': 'Missing asset or shares data'}

        adjusted_assets = total_assets * (1 + revaluation)
        nav_value = adjusted_assets - total_liabilities
        fair_value = max(0.0, nav_value / shares)

        return {
            'fair_value': round(fair_value, 2),
            'nav_total': round(nav_value, 2),
            'adjusted_assets': round(adjusted_assets, 2),
            'scenario': scenario
        }
    except Exception as e:
        return {'fair_value': None, 'error': str(e)}


def compute_statistical_band(assumptions, key_metrics, scenario='base'):
    """
    Statistical/Historical P/E Band analysis.
    Uses historical P/E range + forward EPS to estimate fair value band.
    """
    try:
        rel = assumptions.get('relative_valuation', {})

        def get_val(d, key, scen=scenario):
            v = d.get(key, 0)
            if isinstance(v, dict):
                return v.get(scen, v.get('base', 0))
            return v

        forward_eps = get_val(rel, 'forward_eps')
        if not forward_eps or forward_eps <= 0:
            return {'fair_value': None, 'error': 'No forward EPS'}

        # Try to get current P/E from key_metrics
        current_pe = _safe_float(key_metrics.get('pe_ratio') or key_metrics.get('stock_pe') or key_metrics.get('Stock P/E'))

        peer_pe = get_val(rel, 'peer_median_pe')

        if not current_pe and not peer_pe:
            return {'fair_value': None, 'error': 'No P/E data for statistical analysis'}

        # Use available P/E data as the mean-reversion reference
        reference_pe = current_pe or peer_pe

        # Use the REAL historical P/E dispersion when available
        # (band = reference ± 1 std dev), else fall back to a ±25% proxy.
        pe_std = _safe_float(rel.get('pe_history_std'))
        if pe_std and pe_std > 0:
            # Cap the band so a noisy/volatile history can't produce an absurdly wide range
            pe_std = min(pe_std, reference_pe * 0.6)
            pe_low = max(0.0, reference_pe - pe_std)
            pe_high = reference_pe + pe_std
            band_basis = f"±1σ ({pe_std:.1f})"
        else:
            pe_low = reference_pe * 0.75
            pe_high = reference_pe * 1.25
            band_basis = "±25% (no history)"
        pe_mid = reference_pe

        if scenario == 'bull':
            fair_value = max(0.0, forward_eps * pe_high)
        elif scenario == 'bear':
            fair_value = max(0.0, forward_eps * pe_low)
        else:
            fair_value = max(0.0, forward_eps * pe_mid)

        return {
            'fair_value': round(fair_value, 2),
            'pe_used': round(pe_mid if scenario == 'base' else (pe_high if scenario == 'bull' else pe_low), 1),
            'pe_range': f"{pe_low:.1f}x — {pe_high:.1f}x",
            'band_basis': band_basis,
            'forward_eps': forward_eps,
            'scenario': scenario
        }
    except Exception as e:
        return {'fair_value': None, 'error': str(e)}


# =====================================================================
# MODEL RUNNER — Runs All Applicable Models
# =====================================================================

MODEL_CONFIGS = {
    'dcf_fcff': {
        'name': 'DCF (FCFF)',
        'icon': '💰',
        'description': 'The Discounted Cash Flow (FCFF) model values the entire enterprise by calculating the present value of its future Free Cash Flows to the Firm (Unlevered).<br><br><strong>Methodology:</strong> It projects operating revenue, subtracts operating expenses and taxes to find NOPAT, adds back non-cash depreciation, and subtracts capital expenditures and changes in working capital. The resulting cash flows are discounted back to the present using the Weighted Average Cost of Capital (WACC), which represents the blended cost of debt and equity based on the target capital structure.',
        'reasoning_keys': ['dcf_assumptions', 'wacc_components'],
        'compute_fn': compute_dcf_fcff,
        'slider_params': [
            {'key': 'dcf_assumptions.revenue_growth_y1', 'label': 'Revenue Growth Y1 (%)', 'min': -5, 'max': 40, 'step': 0.5},
            {'key': 'dcf_assumptions.revenue_growth_y2_to_y5', 'label': 'Revenue Y2-Y5 Avg (%)', 'min': -5, 'max': 35, 'step': 0.5},
            {'key': 'dcf_assumptions.ebitda_margin', 'label': 'EBITDA Margin (%)', 'min': 5, 'max': 50, 'step': 0.5},
            {'key': 'dcf_assumptions.da_pct_of_revenue', 'label': 'D&A (% of Rev)', 'min': 1, 'max': 20, 'step': 0.5},
            {'key': 'dcf_assumptions.capex_pct_of_revenue', 'label': 'CapEx (% of Rev)', 'min': 1, 'max': 30, 'step': 0.5},
            {'key': 'dcf_assumptions.working_capital_pct_of_revenue', 'label': 'WC (% of Rev)', 'min': 1, 'max': 30, 'step': 0.5},
            {'key': 'dcf_assumptions.tax_rate', 'label': 'Tax Rate (%)', 'min': 15, 'max': 35, 'step': 1},
            {'key': 'wacc_components.risk_free_rate', 'label': 'Risk-Free Rate (%)', 'min': 4, 'max': 10, 'step': 0.25},
            {'key': 'wacc_components.equity_risk_premium', 'label': 'Equity Risk Premium (%)', 'min': 4, 'max': 10, 'step': 0.25},
            {'key': 'wacc_components.beta', 'label': 'Beta', 'min': 0.4, 'max': 2.0, 'step': 0.05},
            {'key': 'wacc_components.cost_of_debt_pretax', 'label': 'Cost of Debt (%)', 'min': 5, 'max': 15, 'step': 0.5},
            {'key': 'wacc_components.debt_to_total_capital', 'label': 'Debt to Capital (%)', 'min': 0, 'max': 80, 'step': 1},
            {'key': 'dcf_assumptions.terminal_growth', 'label': 'Terminal Growth (%)', 'min': 1, 'max': 6, 'step': 0.25},
        ]
    },
    'dcf_fcfe': {
        'name': 'DCF (FCFE)',
        'icon': '📈',
        'description': 'The Discounted Cash Flow (FCFE) model values only the firm\'s equity directly by projecting Free Cash Flow to Equity (Levered).<br><br><strong>Methodology:</strong> Unlike FCFF, FCFE subtracts net debt payments (interest and principal repayments) to isolate the cash exclusively available to equity shareholders. Because it inherently prices in capital structure risks, these cash flows are discounted strictly using the Cost of Equity (via CAPM) rather than the blended WACC.',
        'reasoning_keys': ['dcf_assumptions', 'wacc_components'],
        'compute_fn': compute_dcf_fcfe,
        'slider_params': [
            {'key': 'dcf_assumptions.revenue_growth_y1', 'label': 'Earnings Growth Y1 (%)', 'min': -5, 'max': 40, 'step': 0.5},
            {'key': 'dcf_assumptions.revenue_growth_y2_to_y5', 'label': 'Earnings Growth Y2-Y5 Avg (%)', 'min': -5, 'max': 35, 'step': 0.5},
            {'key': 'dcf_assumptions.reinvestment_rate', 'label': 'Reinvestment Rate (%)', 'min': 10, 'max': 90, 'step': 1},
            {'key': 'wacc_components.risk_free_rate', 'label': 'Risk-Free Rate (%)', 'min': 4, 'max': 10, 'step': 0.25},
            {'key': 'wacc_components.equity_risk_premium', 'label': 'Equity Risk Premium (%)', 'min': 4, 'max': 10, 'step': 0.25},
            {'key': 'wacc_components.beta', 'label': 'Beta', 'min': 0.4, 'max': 2.0, 'step': 0.05},
            {'key': 'dcf_assumptions.terminal_growth', 'label': 'Terminal Growth (%)', 'min': 1, 'max': 6, 'step': 0.25},
        ]
    },
    'ddm_gordon': {
        'name': 'DDM (Gordon)',
        'icon': '🏦',
        'description': 'The Gordon Growth Model (DDM) assumes a mature firm will pay dividends that grow at a constant stable rate forever.<br><br><strong>Methodology:</strong> It calculates equity value as [Next Year Dividend / (Cost of Equity - Stable Growth Rate)]. It is predominantly applicable to extreme mature-stage institutions, dividend aristocrats, and utility conglomerates with zero probability of volatile disruption.',
        'reasoning_keys': ['ddm_assumptions', 'wacc_components'],
        'compute_fn': compute_ddm_gordon,
        'slider_params': [
            {'key': 'ddm_assumptions.current_dps', 'label': 'Dividend Per Share (₹)', 'min': 0, 'max': 200, 'step': 1},
            {'key': 'ddm_assumptions.dividend_growth_stable', 'label': 'Dividend Growth (%)', 'min': 1, 'max': 15, 'step': 0.5},
            {'key': 'wacc_components.equity_risk_premium', 'label': 'Equity Risk Premium (%)', 'min': 4, 'max': 10, 'step': 0.25},
        ]
    },
    'ddm_multistage': {
        'name': 'DDM (Multi-Stage)',
        'icon': '📊',
        'description': 'The Multi-Stage Dividend Discount Model values companies transitioning out of rapid growth phases into eventual maturity.<br><br><strong>Methodology:</strong> It projects an initial period of high, supra-normal dividend growth (e.g., 15-20% for 5 years), before mathematically fading down to a perpetual mature stable growth rate. The distinct cash flows from both phases are discounted back at the Cost of Equity.',
        'reasoning_keys': ['ddm_assumptions', 'wacc_components'],
        'compute_fn': compute_ddm_multistage,
        'slider_params': [
            {'key': 'ddm_assumptions.current_dps', 'label': 'Current DPS (₹)', 'min': 0, 'max': 200, 'step': 1},
            {'key': 'ddm_assumptions.dividend_growth_high', 'label': 'High Growth Rate (%)', 'min': 3, 'max': 25, 'step': 0.5},
            {'key': 'ddm_assumptions.dividend_growth_stable', 'label': 'Stable Growth (%)', 'min': 1, 'max': 10, 'step': 0.5},
        ]
    },
    'relative_pe': {
        'name': 'P/E Multiple',
        'icon': '📐',
        'description': 'The Price-to-Earnings (P/E) Relative Valuation model prices a stock based on the willingness of the market to pay for $1 of its net income compared to its direct industry rivals.<br><br><strong>Methodology:</strong> The AI extracts a peer aggregate median P/E multiple and applies it to the forward EPS estimate. A premium or discount multiplier is then applied based on fundamental comparative advantages like superior ROE, dominant market share, or lower systemic leverage.',
        'reasoning_keys': ['relative_valuation'],
        'compute_fn': compute_relative_pe,
        'slider_params': [
            {'key': 'relative_valuation.forward_eps', 'label': 'Forward EPS (₹)', 'min': 1, 'max': 1000, 'step': 1},
            {'key': 'relative_valuation.peer_median_pe', 'label': 'Target P/E Multiple', 'min': 5, 'max': 80, 'step': 0.5},
        ]
    },
    'relative_pb': {
        'name': 'P/B Multiple',
        'icon': '🏢',
        'description': 'The Price-to-Book (P/B) Relative Valuation model prices a firm relative to its total accounting net asset value.<br><br><strong>Methodology:</strong> Most effectively deployed for banking institutions and highly asset-heavy industrials, the AI estimates a fair P/B multiple extracted from the peer group. It algorithmically accounts for whether the firm\'s ROE justifies trading at a substantial premium to its liquidation book value.',
        'reasoning_keys': ['relative_valuation'],
        'compute_fn': compute_relative_pb,
        'slider_params': [
            {'key': 'relative_valuation.forward_bvps', 'label': 'Book Value / Share (₹)', 'min': 1, 'max': 5000, 'step': 5},
            {'key': 'relative_valuation.peer_median_pb', 'label': 'Target P/B Multiple', 'min': 0.5, 'max': 20, 'step': 0.1},
        ]
    },
    'ev_ebitda': {
        'name': 'EV/EBITDA',
        'icon': '⚡',
        'description': 'The EV/EBITDA multiple is a capital-structure-neutral valuation metric heavily utilized in M&A (Mergers and Acquisitions) modeling.<br><br><strong>Methodology:</strong> By dividing Enterprise Value by EBITDA, it strips out the effects of taxation, D&A accounting policies, and debt loads. The AI calculates the target firm\'s forward Enterprise Value using peer medians, then subtracts Net Debt to arrive directly at the implied Fair Market Equity Value.',
        'reasoning_keys': ['relative_valuation'],
        'compute_fn': compute_ev_ebitda,
        'slider_params': [
            {'key': 'relative_valuation.forward_ebitda_cr', 'label': 'Forward EBITDA (₹ Cr)', 'min': 100, 'max': 500000, 'step': 100},
            {'key': 'relative_valuation.peer_median_ev_ebitda', 'label': 'Target EV/EBITDA', 'min': 3, 'max': 50, 'step': 0.5},
        ]
    },
    'mcap_sales': {
        'name': 'MCap/Sales',
        'icon': '📦',
        'description': 'The Market Cap / Sales model values companies purely based on their top-line revenue generation.<br><br><strong>Methodology:</strong> This is exceptionally critical for high-growth, early-stage, or tech firms that are currently unprofitable and possess negative earnings/EBITDA. The AI derives a peer median Price-to-Sales (P/S) multiple and projects it against the company\'s forward 12-month revenue trajectory.',
        'reasoning_keys': ['relative_valuation'],
        'compute_fn': compute_mcap_sales,
        'slider_params': [
            {'key': 'relative_valuation.forward_revenue_cr', 'label': 'Forward Revenue (₹ Cr)', 'min': 100, 'max': 1000000, 'step': 500},
            {'key': 'relative_valuation.peer_median_mcap_sales', 'label': 'Target MCap/Sales', 'min': 0.1, 'max': 30, 'step': 0.1},
        ]
    },
    'residual_income': {
        'name': 'Residual Income',
        'icon': '🏛️',
        'description': 'The Residual Income Model determines equity value by adding the current Book Value to the present value of all expected future excess returns.<br><br><strong>Methodology:</strong> "Excess Returns" are generated when a firm achieves a Return on Equity (ROE) strictly higher than its Cost of Equity. This model mathematically prevents growth-trap overvaluation and is considered the absolute gold standard for valuing Banks, Insurance, and NBFCs.',
        'reasoning_keys': ['residual_income_assumptions'],
        'compute_fn': compute_residual_income,
        'slider_params': [
            {'key': 'residual_income_assumptions.book_value_per_share', 'label': 'Book Value / Share (₹)', 'min': 10, 'max': 5000, 'step': 5},
            {'key': 'residual_income_assumptions.roe_forecast', 'label': 'Forecast ROE (%)', 'min': 5, 'max': 30, 'step': 0.5},
            {'key': 'residual_income_assumptions.cost_of_equity', 'label': 'Cost of Equity (%)', 'min': 8, 'max': 18, 'step': 0.25},
        ]
    },
    'nav': {
        'name': 'Net Asset Value',
        'icon': '🏗️',
        'description': 'The Net Asset Value (NAV) model calculates intrinsic value by marking a firm\'s individual assets and liabilities to their current fair market prices rather than historical accounting costs.<br><br><strong>Methodology:</strong> Exclusively useful for Holding Companies, Conglomerates (like Reliance), and Real Estate Investment Trusts (REITs). The AI estimates a portfolio NAV discount or premium based on market holding company trends.',
        'reasoning_keys': ['nav_assumptions'],
        'compute_fn': compute_nav,
        'slider_params': [
            {'key': 'nav_assumptions.total_assets_cr', 'label': 'Total Assets (₹ Cr)', 'min': 100, 'max': 5000000, 'step': 1000},
            {'key': 'nav_assumptions.total_liabilities_cr', 'label': 'Total Liabilities (₹ Cr)', 'min': 0, 'max': 5000000, 'step': 1000},
            {'key': 'nav_assumptions.asset_revaluation_pct', 'label': 'Asset Revaluation (%)', 'min': -20, 'max': 30, 'step': 1},
        ]
    },
    'statistical_band': {
        'name': 'Statistical Band',
        'icon': '📉',
        'description': 'Statistical Arbitrage Banding calculates intrinsic targets purely through historical mean-reversion mechanics instead of future projection parameters.<br><br><strong>Methodology:</strong> Assuming no structural permanent damage to the corporate thesis, markets mathematically revert to trailing 5-year and 10-year average P/E multiples. The AI projects forward EPS and tests standard deviations off the trailing anchor average.',
        'reasoning_keys': ['relative_valuation'],
        'compute_fn': None,  # handled separately because it takes key_metrics
        'slider_params': [
            {'key': 'relative_valuation.forward_eps', 'label': 'Forward EPS (₹)', 'min': 1, 'max': 1000, 'step': 1},
        ]
    }
}


def run_all_models(assumptions, key_metrics):
    """
    Run all applicable valuation models across Bull/Base/Bear scenarios.
    Returns dict of model_key -> {bull, base, bear, config, applicable}.
    """
    results = {}
    has_guidance = bool(assumptions.get('guidance_meta', {}).get('has_guidance'))

    for model_key, config in MODEL_CONFIGS.items():
        model_result = {'config': {
            'name': config['name'],
            'icon': config['icon'],
            'description': config['description'],
            'reasoning_keys': config['reasoning_keys'],
            'slider_params': config['slider_params']
        }}

        try:
            scenarios = {}
            scen_list = ['bull', 'base', 'bear']
            if has_guidance and model_key in GUIDANCE_MODELS:
                scen_list = scen_list + ['guidance']
            for scenario in scen_list:
                if model_key == 'statistical_band':
                    out = compute_statistical_band(assumptions, key_metrics, scenario)
                else:
                    out = config['compute_fn'](assumptions, scenario)

                scenarios[scenario] = out

            model_result['bull'] = scenarios['bull']
            model_result['base'] = scenarios['base']
            model_result['bear'] = scenarios['bear']
            if 'guidance' in scenarios:
                model_result['guidance'] = scenarios['guidance']

            # Check if model was skipped
            if scenarios['base'].get('skipped'):
                model_result['applicable'] = False
            elif scenarios['base'].get('fair_value') is not None:
                model_result['applicable'] = True
            else:
                model_result['applicable'] = False

        except Exception as e:
            model_result['applicable'] = False
            model_result['base'] = {'fair_value': None, 'error': str(e)}
            model_result['bull'] = {'fair_value': None, 'error': str(e)}
            model_result['bear'] = {'fair_value': None, 'error': str(e)}

        results[model_key] = model_result

    return results


def recalculate_single_model(model_key, assumptions, key_metrics):
    """
    Recalculate a single model with updated assumptions.
    Used for real-time slider updates. Returns all 3 scenarios.
    """
    config = MODEL_CONFIGS.get(model_key)
    if not config:
        return {'error': f'Unknown model: {model_key}'}

    has_guidance = bool(assumptions.get('guidance_meta', {}).get('has_guidance'))
    scen_list = ['bull', 'base', 'bear']
    if has_guidance and model_key in GUIDANCE_MODELS:
        scen_list = scen_list + ['guidance']

    results = {}
    for scenario in scen_list:
        if model_key == 'statistical_band':
            results[scenario] = compute_statistical_band(assumptions, key_metrics, scenario)
        else:
            results[scenario] = config['compute_fn'](assumptions, scenario)

    return results


# =====================================================================
# ROUTE REGISTRATION
# =====================================================================

def register_forecasting_routes(app, call_gemini_api_fn, call_perplexity_api_fn,
                                 call_openai_api_fn, get_cache_fn,
                                 fetch_peer_fn, get_full_analysis_fn=None):
    """
    Register all Forecasting Agent API routes with the Flask app.
    """

    @app.route('/agent/forecasting/analyze', methods=['POST'])
    def agent_forecasting_analyze():
        """Start a Forecasting Agent valuation job."""
        try:
            data = request.get_json(force=True)
            ticker = data.get('ticker', '').strip().upper()

            if not ticker:
                return jsonify({'error': 'Ticker is required'}), 400

            print(f"FORECASTING_AGENT: Starting valuation for {ticker}", file=sys.stderr)

            # Check for recent cached result
            existing = get_latest_result('forecasting', ticker)
            if existing and data.get('force_refresh') is not True:
                age_minutes = (time.time() - existing['stored_at']) / 60
                if age_minutes < 120:
                    print(f"FORECASTING_AGENT: Returning cached result for {ticker} ({age_minutes:.0f}m old)", file=sys.stderr)
                    return jsonify({
                        'status': 'complete',
                        'result': existing['result'],
                        'cached': True,
                        'age_minutes': round(age_minutes)
                    })

            # Get base financial data
            cached_data = get_cache_fn(ticker)

            if not cached_data and get_full_analysis_fn:
                print(f"FORECASTING_AGENT: No cache. Running full analysis fallback for {ticker}...", file=sys.stderr)
                try:
                    fallback_result, cached_data = get_full_analysis_fn(ticker, skip_ai_summary=True)
                    # On a critical fetch failure get_analysis_for_ticker returns
                    # ({'error': ...}, 500), i.e. cached_data is a status code, not a dict.
                    if not isinstance(cached_data, dict) or not cached_data:
                        reason = fallback_result.get('error', '') if isinstance(fallback_result, dict) else ''
                        print(f"FORECASTING_AGENT: Fallback failed for {ticker}: {reason or 'no data returned'}", file=sys.stderr)
                        return jsonify({
                            'error': 'no_base_data',
                            'message': f"{FORECAST_NO_DATA_MSG} (Reason: {reason})" if reason else FORECAST_NO_DATA_MSG
                        }), 400
                except Exception as fe:
                    print(f"FORECASTING_AGENT ERROR during fallback: {fe}", file=sys.stderr)
                    return jsonify({
                        'error': 'fallback_failed',
                        'message': f"Could not pull financial data for {ticker}."
                    }), 500

            if not isinstance(cached_data, dict) or not cached_data:
                return jsonify({
                    'error': 'no_base_data',
                    'message': FORECAST_NO_DATA_MSG
                }), 400

            # Create background job
            job_id = create_agent_job('forecasting', ticker)

            thread = threading.Thread(
                target=_run_forecasting_pipeline,
                args=(job_id, ticker, cached_data, call_gemini_api_fn, 
                      call_openai_api_fn, call_perplexity_api_fn, fetch_peer_fn)
            )
            thread.daemon = True
            thread.start()

            return jsonify({
                'job_id': job_id,
                'status': 'processing',
                'message': f'Valuation started for {ticker}.'
            })

        except Exception as e:
            print(f"FORECASTING_AGENT ERROR: {e}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return jsonify({'error': str(e)}), 500

    @app.route('/agent/forecasting/<job_id>/status', methods=['GET'])
    def agent_forecasting_status(job_id):
        """Poll endpoint for Forecasting Agent job status."""
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

    @app.route('/agent/forecasting/recalculate', methods=['POST'])
    def agent_forecasting_recalculate():
        """
        Real-time recalculation endpoint for slider-driven updates.
        Pure Python math — no AI calls. ~10ms response.
        """
        try:
            data = request.get_json(force=True)
            model_key = data.get('model')
            assumptions = data.get('assumptions', {})
            key_metrics = data.get('key_metrics', {})

            if not model_key:
                # Recalculate ALL models
                results = run_all_models(assumptions, key_metrics)
                return jsonify({'status': 'success', 'models': results})
            else:
                result = recalculate_single_model(model_key, assumptions, key_metrics)
                return jsonify({'status': 'success', 'model': model_key, 'result': result})

        except Exception as e:
            print(f"FORECASTING_RECALC ERROR: {e}", file=sys.stderr)
            return jsonify({'error': str(e)}), 500

    @app.route('/agent/forecasting/recalculate_peers', methods=['POST'])
    def agent_forecasting_recalculate_peers():
        """
        Recompute peer medians from a user-curated peer set and re-run all
        models. Pure Python — no AI calls. Used by the editable peer table.
        """
        try:
            data = request.get_json(force=True)
            assumptions = data.get('assumptions', {})
            key_metrics = data.get('key_metrics', {})
            peers = data.get('peers', [])
            company = data.get('company', {})
            target_meds = data.get('target_historical_medians', {})
            target_snapshot = data.get('target_value_snapshot', {})

            peer_data = {
                'company': company,
                'peers': peers,
                'target_identified': True,
                'match_method': 'user'
            }

            peer_confidence = apply_authoritative_peer_medians(
                assumptions, peer_data, target_meds, target_snapshot, source='user', recenter=True
            )
            models = run_all_models(assumptions, key_metrics)

            return jsonify({
                'status': 'success',
                'models': models,
                'assumptions': assumptions,
                'peer_confidence': peer_confidence,
                'guidance_meta': assumptions.get('guidance_meta', {})
            })
        except Exception as e:
            print(f"FORECASTING_PEER_RECALC ERROR: {e}", file=sys.stderr)
            return jsonify({'error': str(e)}), 500

    @app.route('/agent/forecasting/chat', methods=['POST'])
    def agent_forecasting_chat():
        """Chat about the valuation results."""
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
                return jsonify({'error': 'Run the valuation first.'}), 400

            chat_prompt = FORECAST_CHAT_PROMPT.format(
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

            return jsonify({'answer': answer, 'status': 'success'})

        except Exception as e:
            print(f"FORECASTING_CHAT ERROR: {e}", file=sys.stderr)
            return jsonify({'error': str(e)}), 500

    @app.route('/agent/forecasting/latest', methods=['GET'])
    def agent_forecasting_latest():
        """Get the latest cached result for tab persistence."""
        ticker = request.args.get('ticker', '').strip().upper()
        if not ticker:
            return jsonify({'error': 'Ticker is required'}), 400

        result = get_latest_result('forecasting', ticker)
        if result:
            return jsonify({
                'status': 'complete',
                'result': result['result'],
                'age_minutes': round((time.time() - result['stored_at']) / 60)
            })
        return jsonify({'status': 'none', 'message': 'No cached result found'})


# =====================================================================
# BACKGROUND PIPELINE
# =====================================================================

def _run_forecasting_pipeline(job_id, ticker, cached_data, call_gemini_api_fn,
                               call_openai_api_fn, call_perplexity_api_fn, fetch_peer_fn):
    """
    5-Stage background pipeline:
    1. Extract financial data from cache
    2. AI Research (parallel: Gemini for assumptions, Perplexity for market data)
    3. Compute all valuation models (pure Python)
    4. AI Triangulation synthesis
    5. Store results
    """
    start_time = time.time()

    try:
        # ==========================================================
        # STAGE 1: Extract Financial Data
        # ==========================================================
        update_agent_job(job_id, {'progress': f'Reading financial data for {ticker}...'})
        print(f"FORECASTING_AGENT: Stage 1 — Extracting financial data", file=sys.stderr)

        fundamentals = cached_data.get('fundamentals', {})
        key_metrics = cached_data.get('key_metrics', {})
        valuation_history = cached_data.get('valuation_and_margin_data') or cached_data.get('scanx_data') or {}
        company_name = cached_data.get('company_name', ticker)

        if isinstance(fundamentals, str):
            try:
                fundamentals = json.loads(fundamentals)
            except json.JSONDecodeError:
                fundamentals = {}
                
        # Calculate Target Historical Medians
        target_meds = {}
        if valuation_history:
            import pandas as pd
            for label, rows in valuation_history.items():
                if not rows: continue
                try:
                    df = pd.DataFrame(rows)
                    if 'date' in df.columns:
                        df['date'] = pd.to_datetime(df['date'])
                        df = df.set_index('date')
                        now = pd.Timestamp.now()
                        metrics = [c for c in df.columns if c != 'date']
                        
                        res = {}
                        for m in metrics:
                            s = pd.to_numeric(df[m], errors='coerce').dropna()
                            if s.empty: continue
                            m_clean = str(m).replace('%', '').strip()
                            res[f"{m_clean}_1yr"] = round(s[s.index >= now - pd.DateOffset(years=1)].median(), 2) if not s[s.index >= now - pd.DateOffset(years=1)].empty else None
                            res[f"{m_clean}_3yr"] = round(s[s.index >= now - pd.DateOffset(years=3)].median(), 2) if not s[s.index >= now - pd.DateOffset(years=3)].empty else None
                            res[f"{m_clean}_5yr"] = round(s[s.index >= now - pd.DateOffset(years=5)].median(), 2) if not s[s.index >= now - pd.DateOffset(years=5)].empty else None
                        if res:
                            target_meds[label] = res
                except Exception as e:
                    pass

        # Latest snapshot of each multiple + historical P/E std-dev (for the
        # own-history peer fallback and the statistical band).
        target_snapshot, pe_std = _compute_target_snapshot_and_std(valuation_history)

        # Build financial data context for AI
        financial_context = _build_financial_context(ticker, company_name, fundamentals, key_metrics, target_meds)

        elapsed_s1 = int(time.time() - start_time)
        print(f"FORECASTING_AGENT: Stage 1 complete — {elapsed_s1}s", file=sys.stderr)

        # ==========================================================
        # STAGE 2: AI Research (2-Phase Hybrid)
        # Phase 1: Fetch peer data + market research in parallel
        # Phase 2: Feed ALL data into Gemini for assumption generation
        # ==========================================================
        update_agent_job(job_id, {
            'progress': f'AI is researching {company_name} — fetching peer data, market research, and generating assumptions...'
        })
        print(f"FORECASTING_AGENT: Stage 2 — AI Research (2-phase hybrid)", file=sys.stderr)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            assumptions, market_research, peer_data = loop.run_until_complete(
                _fetch_ai_research_async(
                    ticker, company_name, financial_context,
                    call_gemini_api_fn, call_perplexity_api_fn, fetch_peer_fn
                )
            )
        finally:
            loop.close()

        # ── Make peer multiples deterministic/authoritative ──
        # Computes real peer medians (P/E, P/B) and uses the company's own
        # historical multiples for EV/EBITDA, MCap/Sales (and as a fallback when
        # peers are missing/thin). The AI's value is kept only as a clamped
        # premium/discount.
        try:
            peer_confidence = apply_authoritative_peer_medians(
                assumptions, peer_data, target_meds, target_snapshot, source='screener'
            )
        except Exception as pe_err:
            print(f"FORECASTING_AGENT: peer median override failed: {pe_err}", file=sys.stderr)
            peer_confidence = {'n_peers': len(peer_data.get('peers', []) or []),
                               'per_multiple_source': {}, 'source': 'screener'}

        # Thread historical P/E std-dev into the statistical band input.
        if pe_std:
            assumptions.setdefault('relative_valuation', {})['pe_history_std'] = pe_std

        # ── Management guidance ("Guidance" scenario) ──
        update_agent_job(job_id, {
            'progress': f'Checking latest concall for management guidance on {company_name}...'
        })
        guidance_meta = build_guidance(ticker, company_name, assumptions, call_gemini_api_fn, allow_fetch=True)
        assumptions['guidance_meta'] = guidance_meta
        print(f"FORECASTING_AGENT: guidance — has_guidance={guidance_meta.get('has_guidance')}, "
              f"source={guidance_meta.get('source')}", file=sys.stderr)

        elapsed_s2 = int(time.time() - start_time)
        print(f"FORECASTING_AGENT: Stage 2 complete — {elapsed_s2}s", file=sys.stderr)

        # ==========================================================
        # STAGE 3: Compute All Valuation Models
        # ==========================================================
        update_agent_job(job_id, {
            'progress': f'Computing 12 valuation models for {ticker}...'
        })
        print(f"FORECASTING_AGENT: Stage 3 — Computing models", file=sys.stderr)

        model_results = run_all_models(assumptions, key_metrics)

        # Count applicable models
        applicable_count = sum(1 for m in model_results.values() if m.get('applicable'))
        print(f"FORECASTING_AGENT: Stage 3 complete — {applicable_count} models applicable", file=sys.stderr)

        elapsed_s3 = int(time.time() - start_time)

        # ==========================================================
        # STAGE 4: AI Triangulation
        # ==========================================================
        update_agent_job(job_id, {
            'progress': f'AI is triangulating {applicable_count} model results for {ticker}...'
        })
        print(f"FORECASTING_AGENT: Stage 4 — AI Triangulation", file=sys.stderr)

        # Build model results summary for AI
        model_summary = _build_model_summary(model_results)

        cmp = key_metrics.get('current_price') or key_metrics.get('Current Price') or key_metrics.get('cmp') or '0'
        market_cap = key_metrics.get('market_cap') or key_metrics.get('Market Cap') or '0'

        triangulation_prompt = FORECAST_TRIANGULATION_PROMPT.format(
            company_name=company_name,
            ticker=ticker,
            model_results=model_summary,
            cmp=cmp,
            market_cap=market_cap
        )

        triangulation_analysis = call_openai_api_fn(
            [{"role": "user", "content": triangulation_prompt}],
            model="gpt-5.4-mini",
            temperature=1
        )

        elapsed_s4 = int(time.time() - start_time)
        print(f"FORECASTING_AGENT: Stage 4 complete — {elapsed_s4}s", file=sys.stderr)

        # ----------------------------------------------------------
        # STAGE 4b: Extract structured JSON from triangulation output
        # ----------------------------------------------------------
        triangulated_values = None
        triangulation_display = triangulation_analysis  # Markdown for rendering

        try:
            import re
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', triangulation_analysis, re.DOTALL)
            if json_match:
                triangulated_values = json.loads(json_match.group(1))
                # Strip the JSON block from the display markdown
                triangulation_display = triangulation_analysis[:json_match.start()].rstrip()
                print(f"FORECASTING_AGENT: Extracted triangulated values — "
                      f"Bull: ₹{triangulated_values.get('weighted_bull')}, "
                      f"Base: ₹{triangulated_values.get('weighted_base')}, "
                      f"Bear: ₹{triangulated_values.get('weighted_bear')}, "
                      f"Verdict: {triangulated_values.get('verdict')}", file=sys.stderr)
            else:
                print("FORECASTING_AGENT: WARNING — No structured JSON found in triangulation output", file=sys.stderr)
        except (json.JSONDecodeError, Exception) as parse_err:
            print(f"FORECASTING_AGENT: WARNING — Failed to parse triangulated JSON: {parse_err}", file=sys.stderr)

        # ==========================================================
        # STAGE 5: Store Results
        # ==========================================================
        result_data = {
            'ticker': ticker,
            'company_name': company_name,
            'assumptions': assumptions,
            'models': model_results,
            'triangulation': triangulation_display,
            'triangulated_values': triangulated_values,
            'market_research': str(market_research)[:5000] if market_research else None,
            'peer_data': peer_data,
            'peer_confidence': peer_confidence,
            'guidance_meta': guidance_meta,
            'target_historical_medians': target_meds,
            'target_value_snapshot': target_snapshot,
            'key_metrics': key_metrics,
            'cmp': str(cmp),
            'market_cap': str(market_cap),
            'applicable_model_count': applicable_count,
            'analyzed_at': time.time(),
            'analysis_time_seconds': elapsed_s4
        }

        store_latest_result('forecasting', ticker, result_data)

        update_agent_job(job_id, {
            'status': 'complete',
            'progress': 'Valuation complete!',
            'result': result_data,
            'completed_at': time.time(),
            'total_time': elapsed_s4
        })

        print(f"FORECASTING_AGENT: ✓ Valuation complete for {ticker} in {elapsed_s4}s — {applicable_count} models", file=sys.stderr)

    except Exception as e:
        elapsed = int(time.time() - start_time)
        print(f"FORECASTING_AGENT ERROR: Job {job_id} failed after {elapsed}s: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        update_agent_job(job_id, {
            'status': 'error',
            'error': f'Forecasting analysis failed: {str(e)}',
            'failed_at': time.time(),
            'elapsed': elapsed
        })


# =====================================================================
# ASYNC RESEARCH FUNCTIONS
# =====================================================================

async def _fetch_ai_research_async(ticker, company_name, financial_context,
                                     call_gemini_api_fn, call_perplexity_api_fn, fetch_peer_fn):
    """
    2-Phase AI research pipeline:
    Phase 1 (parallel): Fetch market research + peer data from external sources
    Phase 2 (sequential): Feed ALL data into Gemini for assumption generation
    
    This ensures the AI sees real peer multiples, analyst consensus, and sector
    data when generating assumptions — not hallucinated numbers.
    """

    async def get_market_research():
        """Fetch market data via Perplexity."""
        try:
            prompt = FORECAST_RESEARCH_PROMPT.format(
                company_name=company_name,
                ticker=ticker
            )

            result = await asyncio.to_thread(
                call_perplexity_api_fn,
                [{"role": "user", "content": prompt}],
                "sonar-pro", 0.1, 90
            )
            return result

        except Exception as e:
            print(f"FORECASTING_AGENT: Market research failed: {e}", file=sys.stderr)
            return f"Error: {e}"

    async def get_peer_data():
        """Fetch peer comparison from Screener."""
        try:
            result = await asyncio.to_thread(
                fetch_peer_fn, ticker, company_name
            )
            return result
        except Exception as e:
            print(f"FORECASTING_AGENT: Peer data fetch failed: {e}", file=sys.stderr)
            return {'company': {}, 'peers': []}

    # ── PHASE 1: Fetch external data in parallel ──
    print(f"FORECASTING_AGENT: Phase 1 — Fetching market research + peer data in parallel", file=sys.stderr)
    phase1_results = await asyncio.gather(
        get_market_research(),
        get_peer_data(),
        return_exceptions=True
    )

    market_research = phase1_results[0] if not isinstance(phase1_results[0], Exception) else "Error fetching market data"
    peer_data = phase1_results[1] if not isinstance(phase1_results[1], Exception) else {'company': {}, 'peers': []}

    print(f"FORECASTING_AGENT: Phase 1 complete — peer_count={len(peer_data.get('peers', []))}", file=sys.stderr)

    # ── PHASE 2: Generate assumptions with ALL available data ──
    print(f"FORECASTING_AGENT: Phase 2 — Generating assumptions with full context", file=sys.stderr)

    peer_context = _build_peer_context(peer_data)
    market_research_str = str(market_research)[:8000] if market_research else "No market research available."

    async def get_assumptions():
        """Generate valuation assumptions using Gemini with full context."""
        try:
            prompt = FORECAST_ASSUMPTIONS_PROMPT.format(
                company_name=company_name,
                ticker=ticker,
                financial_data=financial_context,
                peer_context=peer_context,
                market_research=market_research_str
            )

            raw_response = await asyncio.to_thread(
                call_gemini_api_fn,
                [{"role": "user", "content": prompt}],
                "gemini-3.1-pro-preview",
                0.3,
                True,
                'HIGH'
            )

            # Parse JSON from response
            cleaned = raw_response.strip()
            # Remove markdown code fences if present
            if cleaned.startswith('```json'):
                cleaned = cleaned[7:]
            elif cleaned.startswith('```'):
                cleaned = cleaned[3:]
            if cleaned.endswith('```'):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            # Find the JSON object
            start_idx = cleaned.find('{')
            end_idx = cleaned.rfind('}')
            if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                cleaned = cleaned[start_idx:end_idx + 1]

            assumptions = json.loads(cleaned)
            print(f"FORECASTING_AGENT: Assumptions generated — company_type={assumptions.get('company_type', 'unknown')}", file=sys.stderr)
            return assumptions

        except json.JSONDecodeError as je:
            print(f"FORECASTING_AGENT: JSON parse error in assumptions: {je}", file=sys.stderr)
            print(f"FORECASTING_AGENT: Raw response (first 500): {raw_response[:500]}", file=sys.stderr)
            # Return sensible defaults
            return _get_default_assumptions(ticker, company_name)
        except Exception as e:
            print(f"FORECASTING_AGENT: Assumption generation failed: {e}", file=sys.stderr)
            return _get_default_assumptions(ticker, company_name)

    assumptions = await get_assumptions()
    if isinstance(assumptions, Exception):
        assumptions = _get_default_assumptions(ticker, company_name)

    return assumptions, market_research, peer_data


# =====================================================================
# MANAGEMENT GUIDANCE ("Guidance" scenario)
# Sources the latest concall (cached Concall Agent result, else a quick
# transcript-only fetch), extracts structured forward guidance, and merges
# it into the assumptions so the guidance-consumable models can run a
# scenario='guidance' driven by management's own numbers.
# =====================================================================

# Models that can consume management guidance (DCF + revenue/EPS-multiple based).
GUIDANCE_MODELS = {'dcf_fcff', 'dcf_fcfe', 'relative_pe', 'ev_ebitda', 'mcap_sales'}


async def _quick_fetch_transcript_async(ticker, company_name):
    """
    Bounded, transcript-ONLY fetch reused from the Concall Agent helpers
    (no full concall analysis). Tries Screener REC audio, then YouTube.
    Returns transcript text or ''.
    """
    try:
        from fetchers.screener_fetcher import fetch_concall_rec_url_async
        from agents.concall_agent import (
            _extract_youtube_transcript, _transcribe_audio_video_from_url,
            _search_youtube_concall
        )
    except Exception as e:
        print(f"FORECASTING_AGENT: concall helpers unavailable: {e}", file=sys.stderr)
        return ''

    async def _transcribe(url):
        is_yt = ('youtube' in url.lower() or 'youtu.be' in url.lower())
        text = ''
        if is_yt:
            try:
                text = await _extract_youtube_transcript(url, max_chars=80000)
            except Exception:
                text = ''
        if not text or text.startswith('Error'):
            try:
                text = await _transcribe_audio_video_from_url(url, 'audio')
            except Exception:
                text = ''
        return text or ''

    # 1. Screener REC audio link
    audio_url = ''
    try:
        rec_info = await fetch_concall_rec_url_async(ticker)
        if rec_info and rec_info.get('url'):
            audio_url = rec_info.get('url')
    except Exception:
        audio_url = ''

    text = ''
    if audio_url:
        text = await _transcribe(audio_url)

    # 2. YouTube fallback
    if not text or text.startswith('Error'):
        try:
            yt_url = await asyncio.to_thread(_search_youtube_concall, company_name, ticker, '')
        except Exception:
            yt_url = ''
        if yt_url:
            text = await _transcribe(yt_url)

    return text if (text and not text.startswith('Error')) else ''


def _get_concall_text(ticker, company_name, allow_fetch=True, fetch_timeout=180):
    """
    Obtain the latest concall text for guidance extraction.
    Returns (text, source) where source ∈ {'cached','transcript_only','unavailable'}.
    """
    # 1. Cached Concall Agent result (preferred — fast)
    try:
        cached = get_latest_result('concall', ticker)
    except Exception:
        cached = None
    if cached and cached.get('result'):
        r = cached['result']
        txt = r.get('transcript_text') or r.get('analysis') or ''
        if txt and len(str(txt).strip()) > 200:
            return str(txt), 'cached'

    # 2. Quick transcript-only fetch (bounded)
    if allow_fetch:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                txt = loop.run_until_complete(
                    asyncio.wait_for(_quick_fetch_transcript_async(ticker, company_name),
                                     timeout=fetch_timeout)
                )
            finally:
                loop.close()
            if txt and len(str(txt).strip()) > 200:
                return str(txt), 'transcript_only'
        except Exception as e:
            print(f"FORECASTING_AGENT: quick transcript fetch failed: {e}", file=sys.stderr)

    return '', 'unavailable'


def _extract_guidance(text, ticker, company_name, call_gemini_api_fn):
    """Extract structured forward guidance JSON from concall text via Gemini."""
    if not text:
        return {'has_guidance': False}
    try:
        prompt = FORECAST_GUIDANCE_EXTRACTION_PROMPT.format(
            company_name=company_name, ticker=ticker, transcript_text=text[:60000]
        )
        raw = call_gemini_api_fn(
            [{"role": "user", "content": prompt}],
            model="gemini-3.5-flash-lite", temperature=0.2,
            thinking_level='HIGH'
        )
        cleaned = (raw or '').strip()
        if cleaned.startswith('```json'):
            cleaned = cleaned[7:]
        elif cleaned.startswith('```'):
            cleaned = cleaned[3:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        s, e = cleaned.find('{'), cleaned.rfind('}')
        if s != -1 and e != -1 and e > s:
            cleaned = cleaned[s:e + 1]
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else {'has_guidance': False}
    except Exception as e:
        print(f"FORECASTING_AGENT: guidance extraction failed: {e}", file=sys.stderr)
        return {'has_guidance': False}


def _guidance_field(guidance, key):
    """Pull a numeric value from a guidance field that may be {value,...} or scalar/null."""
    v = guidance.get(key)
    if isinstance(v, dict):
        return _safe_float(v.get('value'))
    return _safe_float(v)


def merge_guidance_into_assumptions(assumptions, guidance):
    """
    Add a 'guidance' key to the relevant per-metric assumption dicts so the
    scenario machinery (get_val with scen='guidance') uses management's numbers,
    falling back to base for anything not guided. Returns guidance_meta.
    """
    if not guidance or not guidance.get('has_guidance'):
        return {'has_guidance': False}

    dcf = assumptions.setdefault('dcf_assumptions', {})
    rel = assumptions.setdefault('relative_valuation', {})

    def set_guidance(container, key, value):
        if value is None:
            return
        cur = container.get(key)
        if isinstance(cur, dict):
            cur['guidance'] = value
        else:
            # promote scalar/missing to a scenario dict preserving base
            base = _safe_float(cur)
            container[key] = {'bull': base, 'base': base, 'bear': base, 'guidance': value} if base is not None else {'guidance': value}

    rev_growth = _guidance_field(guidance, 'revenue_growth_pct')
    ebitda_margin = _guidance_field(guidance, 'ebitda_margin_pct')
    capex_cr = _guidance_field(guidance, 'capex_cr')
    eps = _guidance_field(guidance, 'eps')
    revenue_cr = _guidance_field(guidance, 'revenue_cr')
    ebitda_cr = _guidance_field(guidance, 'ebitda_cr')

    # DCF growth: apply guided Y1 growth; leave Y2-Y5 to fall back to base.
    set_guidance(dcf, 'revenue_growth_y1', rev_growth)
    set_guidance(dcf, 'ebitda_margin', ebitda_margin)

    # CapEx guidance → % of revenue. Convert against the revenue the GUIDANCE
    # scenario will actually project (trailing × (1+guided growth)), so the
    # year-1 CapEx ≈ the absolute amount management guided. Fall back to
    # management's guided revenue, then the base forward revenue.
    if capex_cr is not None:
        ref_rev = None
        trailing_rev = _safe_float(rel.get('trailing_revenue_cr'))
        if trailing_rev and trailing_rev > 0 and rev_growth is not None:
            ref_rev = trailing_rev * (1 + rev_growth / 100.0)
        if not ref_rev or ref_rev <= 0:
            ref_rev = revenue_cr  # management's own guided revenue (if any)
        if not ref_rev or ref_rev <= 0:
            ref_rev = _safe_float(_get_scenario_val(rel.get('forward_revenue_cr'), 'base'))
        if ref_rev and ref_rev > 0.1:
            set_guidance(dcf, 'capex_pct_of_revenue', round(capex_cr / ref_rev * 100, 2))
        else:
            print(f"FORECASTING_AGENT: capex guidance skipped — no valid revenue reference", file=sys.stderr)

    # Relative-valuation forward figures
    set_guidance(rel, 'forward_eps', eps)
    set_guidance(rel, 'forward_revenue_cr', revenue_cr)
    set_guidance(rel, 'forward_ebitda_cr', ebitda_cr)

    return {
        'has_guidance': True,
        'guidance_horizon': guidance.get('guidance_horizon'),
        'management_tone': guidance.get('management_tone'),
        'notes': guidance.get('notes'),
        'fields': {
            k: guidance.get(k) for k in (
                'revenue_growth_pct', 'ebitda_margin_pct', 'ebit_margin_pct',
                'capex_cr', 'revenue_cr', 'ebitda_cr', 'eps'
            ) if guidance.get(k) is not None
        }
    }


def _get_scenario_val(v, scen='base'):
    """Read a scenario value from a {bull,base,bear,...} dict or scalar."""
    if isinstance(v, dict):
        return v.get(scen, v.get('base'))
    return v


def build_guidance(ticker, company_name, assumptions, call_gemini_api_fn, allow_fetch=True):
    """
    Orchestrate: source concall text → extract guidance → merge into assumptions.
    Mutates `assumptions` in place. Returns guidance_meta (always a dict with
    'has_guidance' and 'source'). Never raises.
    """
    try:
        text, source = _get_concall_text(ticker, company_name, allow_fetch=allow_fetch)
        if not text:
            return {'has_guidance': False, 'source': 'unavailable'}
        guidance = _extract_guidance(text, ticker, company_name, call_gemini_api_fn)
        meta = merge_guidance_into_assumptions(assumptions, guidance)
        meta['source'] = source if meta.get('has_guidance') else 'unavailable'
        return meta
    except Exception as e:
        print(f"FORECASTING_AGENT: build_guidance failed: {e}", file=sys.stderr)
        return {'has_guidance': False, 'source': 'unavailable'}


# =====================================================================
# HELPER FUNCTIONS
# =====================================================================

def _build_peer_context(peer_data):
    """
    Format Screener peer data into a clean, structured context string
    for the assumptions prompt. Extracts real multiples the AI can reference.
    """
    if not peer_data or (not peer_data.get('company') and not peer_data.get('peers')):
        return "No peer comparison data available."

    lines = []

    # Company's own metrics
    company = peer_data.get('company', {})
    if company:
        lines.append("### Target Company Metrics")
        lines.append(f"- Name: {company.get('name', 'N/A')}")
        for k, v in company.items():
            if k != 'name' and v is not None:
                lines.append(f"- {k}: {v}")
        lines.append("")

    # Peer comparison table
    peers = peer_data.get('peers', [])
    if peers:
        lines.append("### Peer Comparison Table")

        # Determine all available columns from peer data
        all_keys = set()
        for p in peers:
            all_keys.update(p.keys())

        # Prioritize useful columns
        priority_cols = ['name', 'CMP', 'Mar Cap', 'P/E', 'P/B', 'EV/EBITDA',
                         'MCap/Sales', 'ROE', 'ROCE', 'Div Yld', 'Sales', 'NP']
        columns = [c for c in priority_cols if c in all_keys]
        # Add any remaining columns
        for k in sorted(all_keys):
            if k not in columns:
                columns.append(k)

        # Build markdown table
        header = "| " + " | ".join(columns) + " |"
        separator = "| " + " | ".join(["---"] * len(columns)) + " |"
        lines.append(header)
        lines.append(separator)

        for p in peers:
            row = "| " + " | ".join(str(p.get(c, 'N/A')) for c in columns) + " |"
            lines.append(row)

        # Calculate medians for numeric columns
        lines.append("")
        lines.append("### Peer Medians (computed)")
        numeric_cols = [c for c in columns if c not in ('name', 'Name')]
        for col in numeric_cols:
            vals = []
            for p in peers:
                v = _safe_float(p.get(col))
                if v is not None and v > 0:
                    vals.append(v)
            if vals:
                vals.sort()
                mid = len(vals) // 2
                median = vals[mid] if len(vals) % 2 == 1 else (vals[mid - 1] + vals[mid]) / 2
                mean = sum(vals) / len(vals)
                lines.append(f"- {col}: Median = {median:.2f}, Mean = {mean:.2f} (n={len(vals)})")

    return "\n".join(lines) if lines else "No peer comparison data available."


# =====================================================================
# DETERMINISTIC PEER MEDIANS & OWN-HISTORY FALLBACK
# The Screener peer table only carries P/E and P/B (plus ROE/ROCE). EV/EBITDA
# and MCap/Sales are NOT in that table, so for those we fall back to the
# company's OWN "Valuation & Margin History" (target medians + latest snapshot).
# =====================================================================

def _median(vals):
    """Median of a non-empty list of numbers."""
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 == 1 else (s[mid - 1] + s[mid]) / 2.0


def compute_peer_medians(peer_data):
    """
    Compute real peer medians from the scraped peer table.
    Only P/E and P/B are available from Screener's peer comparison table.
    Returns {'pe', 'pb', 'n_pe', 'n_pb'} (values None if no valid data).
    """
    out = {'pe': None, 'pb': None, 'n_pe': 0, 'n_pb': 0}
    if not isinstance(peer_data, dict):
        return out
    peers = peer_data.get('peers') or []
    pe_vals, pb_vals = [], []
    for p in peers:
        pe = _safe_float(p.get('pe_ratio'))
        if pe is not None and pe > 0:
            pe_vals.append(pe)
        pb = _safe_float(p.get('pb_ratio'))
        if pb is not None and pb > 0:
            pb_vals.append(pb)
    if pe_vals:
        out['pe'] = round(_median(pe_vals), 2)
        out['n_pe'] = len(pe_vals)
    if pb_vals:
        out['pb'] = round(_median(pb_vals), 2)
        out['n_pb'] = len(pb_vals)
    return out


def _own_history_multiples(target_meds, target_snapshot):
    """
    Pull the company's OWN historical multiples (P/E, P/B, EV/EBITDA, MCap/Sales)
    from the Valuation & Margin History medians, preferring the 3yr median and
    falling back to 5yr/1yr, then the latest snapshot.
    """
    out = {'pe': None, 'pb': None, 'ev_ebitda': None, 'mcap_sales': None}
    target_meds = target_meds or {}
    target_snapshot = target_snapshot or {}

    def pick(label, prefix):
        d = target_meds.get(label) or {}
        for suffix in ('_3yr', '_5yr', '_1yr'):
            v = _safe_float(d.get(prefix + suffix))
            if v is not None and v > 0:
                return round(v, 2)
        return None

    out['pe'] = pick('PE Ratio', 'PE') or target_snapshot.get('pe')
    out['pb'] = pick('PB Ratio', 'Price to BV') or target_snapshot.get('pb')
    out['ev_ebitda'] = pick('EV / EBITDA', 'EV / EBITDA') or target_snapshot.get('ev_ebitda')
    out['mcap_sales'] = pick('Market Cap / Sales', 'Market Cap / Sales') or target_snapshot.get('mcap_sales')
    return out


def _clamp_multiple_to_authoritative(ai_val, auth, band=0.30):
    """
    Make the code-computed `auth` median authoritative while preserving the AI's
    justified premium/discount and bull/base/bear spread — clamped to ±band of auth.
    Returns a {'bull','base','bear'} dict (or scalar if AI gave a scalar).
    """
    lo, hi = auth * (1 - band), auth * (1 + band)

    def cl(x):
        x = _safe_float(x)
        if x is None or x <= 0:
            return round(auth, 2)
        return round(min(hi, max(lo, x)), 2)

    if isinstance(ai_val, dict):
        out = {}
        for scen in ('bull', 'base', 'bear'):
            if scen in ai_val:
                out[scen] = cl(ai_val.get(scen))
        if 'base' not in out:
            out['base'] = round(auth, 2)
        # carry through any non-scenario keys (e.g. a pre-existing guidance value)
        for k, v in ai_val.items():
            if k not in ('bull', 'base', 'bear'):
                out[k] = v
        return out
    if ai_val is None:
        return {'bull': round(auth * 1.1, 2), 'base': round(auth, 2), 'bear': round(auth * 0.9, 2)}
    return cl(ai_val)


def apply_authoritative_peer_medians(assumptions, peer_data, target_meds, target_snapshot, source='screener', recenter=False):
    """
    Override the AI's peer multiples with deterministic, authoritative values.
      - P/E and P/B: real peer-table median when >=3 valid peers, else the
        company's own historical multiple.
      - EV/EBITDA and MCap/Sales: always the company's own historical multiple
        (not available in Screener's peer table).
    On the first pipeline pass the AI's chosen value is kept as a premium/discount
    clamped to ±30%. When `recenter=True` (a user-curated peer set), the multiple
    is re-centered directly on the new computed median (the user is the authority).
    Returns a `peer_confidence` dict describing what was used per multiple.
    """
    rel = assumptions.setdefault('relative_valuation', {})
    peer_med = compute_peer_medians(peer_data)
    own = _own_history_multiples(target_meds, target_snapshot)
    peers = (peer_data.get('peers') or []) if isinstance(peer_data, dict) else []
    # Default conservatively: only "identified" if the key says so, or (legacy
    # payloads without the key) if a company row is present.
    target_identified = peer_data.get('target_identified', bool(peer_data.get('company'))) if isinstance(peer_data, dict) else False
    MIN_PEERS = 3
    sources = {}

    def resolve(metric_key, peer_val, peer_n):
        if peer_val is not None and peer_n >= MIN_PEERS:
            return peer_val, 'peer'
        own_val = own.get(metric_key)
        if own_val is not None and own_val > 0:
            return own_val, 'own_history'
        if peer_val is not None and peer_n > 0:
            return peer_val, 'peer_thin'
        return None, 'default'

    plan = [
        ('pe', 'peer_median_pe', peer_med.get('pe'), peer_med.get('n_pe', 0)),
        ('pb', 'peer_median_pb', peer_med.get('pb'), peer_med.get('n_pb', 0)),
        ('ev_ebitda', 'peer_median_ev_ebitda', None, 0),
        ('mcap_sales', 'peer_median_mcap_sales', None, 0),
    ]
    for metric_key, rel_key, pv, pn in plan:
        auth, src = resolve(metric_key, pv, pn)
        sources[metric_key] = src
        if auth is None:
            continue  # leave AI / default value untouched
        if recenter:
            rel[rel_key] = {'bull': round(auth * 1.1, 2), 'base': round(auth, 2), 'bear': round(auth * 0.9, 2)}
        else:
            rel[rel_key] = _clamp_multiple_to_authoritative(rel.get(rel_key), auth)

    return {
        'n_peers': len(peers),
        'target_identified': target_identified,
        'match_method': peer_data.get('match_method', 'unknown') if isinstance(peer_data, dict) else 'unknown',
        'source': source,
        'peer_median_pe': peer_med.get('pe'),
        'peer_median_pb': peer_med.get('pb'),
        'own_history': own,
        'per_multiple_source': sources,
    }


def _compute_target_snapshot_and_std(valuation_history):
    """
    From the Valuation & Margin History timeseries, return:
      - snapshot: latest value per multiple {'pe','pb','ev_ebitda','mcap_sales'}
      - pe_std: std-dev of the historical P/E (for the statistical band)
    """
    snapshot = {}
    pe_std = None
    if not valuation_history:
        return snapshot, pe_std
    try:
        import pandas as pd
    except Exception:
        return snapshot, pe_std

    label_map = {
        'PE Ratio': ('pe', 'PE'),
        'PB Ratio': ('pb', 'Price to BV'),
        'EV / EBITDA': ('ev_ebitda', 'EV / EBITDA'),
        'Market Cap / Sales': ('mcap_sales', 'Market Cap / Sales'),
    }
    for label, (key, col) in label_map.items():
        rows = valuation_history.get(label)
        if not rows:
            continue
        try:
            df = pd.DataFrame(rows)
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'], errors='coerce')
                df = df.dropna(subset=['date']).sort_values('date')
            if col not in df.columns:
                cand = [c for c in df.columns if c != 'date']
                if not cand:
                    continue
                col = cand[0]
            s = pd.to_numeric(df[col], errors='coerce').dropna()
            if s.empty:
                continue
            snapshot[key] = round(float(s.iloc[-1]), 2)
            if key == 'pe' and len(s) >= 5:
                # Drop extreme outliers (1.5×IQR fences) before measuring dispersion
                q1, q3 = s.quantile(0.25), s.quantile(0.75)
                iqr = q3 - q1
                s_f = s[(s >= q1 - 1.5 * iqr) & (s <= q3 + 1.5 * iqr)] if (iqr and iqr > 0) else s
                if len(s_f) >= 5:
                    std_val = float(s_f.std())
                    if std_val == std_val:  # not NaN
                        pe_std = round(std_val, 2)
        except Exception:
            continue
    return snapshot, pe_std


def _build_financial_context(ticker, company_name, fundamentals, key_metrics, target_meds=None):
    """Build a comprehensive financial data context string for AI prompts."""
    sections = []

    sections.append(f"### KEY METRICS FOR {company_name} ({ticker})")
    if key_metrics:
        for k, v in key_metrics.items():
            sections.append(f"- {k}: {v}")

    for table_name in ['Annual Results', 'Profit & Loss', 'Balance Sheet',
                        'Cash Flow', 'Financial Ratios', 'Quarterly Results']:
        table = fundamentals.get(table_name)
        if table is not None:
            sections.append(f"\n### {table_name}:")
            try:
                if hasattr(table, 'to_string'):
                    sections.append(table.to_string()[:3000])
                elif isinstance(table, dict):
                    sections.append(json.dumps(table, indent=2, default=str)[:3000])
                elif isinstance(table, str):
                    sections.append(table[:3000])
            except Exception:
                pass

    if target_meds:
        sections.append(f"\n### TARGET COMPANY HISTORICAL MULTIPLES & MARGINS:")
        sections.append(json.dumps(target_meds, indent=2, default=str))

    return "\n".join(sections)


def _build_model_summary(model_results):
    """Build a summary of all model results for the triangulation prompt."""
    lines = []
    for model_key, model_data in model_results.items():
        if not model_data.get('applicable'):
            continue

        config = model_data.get('config', {})
        base = model_data.get('base', {})
        bull = model_data.get('bull', {})
        bear = model_data.get('bear', {})

        name = config.get('name', model_key)
        base_val = base.get('fair_value', 'N/A')
        bull_val = bull.get('fair_value', 'N/A')
        bear_val = bear.get('fair_value', 'N/A')

        lines.append(f"### {name}")
        lines.append(f"- Bull Fair Value: ₹{bull_val}")
        lines.append(f"- Base Fair Value: ₹{base_val}")
        lines.append(f"- Bear Fair Value: ₹{bear_val}")

        # Add key details
        for k, v in base.items():
            if k not in ('fair_value', 'error', 'scenario', 'skipped'):
                lines.append(f"- {k}: {v}")
        lines.append("")

    return "\n".join(lines)


def _get_default_assumptions(ticker, company_name):
    """Return sensible default assumptions if AI fails."""
    return {
        'company_type': 'general',
        'company_type_reasoning': 'Default fallback — AI assumption generation failed',
        'industry': 'Unknown',
        'currency': 'INR',
        'dcf_assumptions': {
            'revenue_growth_y1': {'bull': 15, 'base': 10, 'bear': 5},
            'revenue_growth_y2_to_y5': {'bull': 13, 'base': 9, 'bear': 4},
            'ebitda_margin': {'bull': 20, 'base': 17, 'bear': 14},
            'da_pct_of_revenue': {'bull': 4, 'base': 5, 'bear': 6},
            'capex_pct_of_revenue': 7,
            'reinvestment_rate': {'bull': 40, 'base': 50, 'bear': 60},
            'tax_rate': 25,
            'working_capital_pct_of_revenue': 10,
            'terminal_growth': {'bull': 5, 'base': 4, 'bear': 3},
            'reasoning': 'Default assumptions used — AI generation was unavailable'
        },
        'wacc_components': {
            'risk_free_rate': 7.0,
            'equity_risk_premium': {'bull': 5.5, 'base': 6.5, 'bear': 7.5},
            'beta': {'bull': 0.9, 'base': 1.0, 'bear': 1.1},
            'cost_of_debt_pretax': 9.0,
            'debt_to_total_capital': 25,
            'reasoning': 'Default WACC components'
        },
        'ddm_assumptions': {
            'current_dps': 0,
            'dividend_growth_high': {'bull': 10, 'base': 8, 'bear': 5},
            'dividend_growth_stable': {'bull': 6, 'base': 4, 'bear': 2},
            'high_growth_years': 5,
            'payout_ratio_current': 0,
            'applicable': False,
            'reasoning': 'No dividend data available'
        },
        'relative_valuation': {
            'forward_eps': {'bull': 0, 'base': 0, 'bear': 0},
            'forward_bvps': 0,
            'forward_ebitda_cr': {'bull': 0, 'base': 0, 'bear': 0},
            'forward_revenue_cr': {'bull': 0, 'base': 0, 'bear': 0},
            'peer_median_pe': 20,
            'peer_median_pb': 3,
            'peer_median_ev_ebitda': 12,
            'peer_median_mcap_sales': 2,
            'net_debt_cr': 0,
            'shares_outstanding_cr': 1,
            'reasoning': 'Default relative valuation — please check underlying data'
        },
        'residual_income_assumptions': {
            'book_value_per_share': 0,
            'roe_forecast': {'bull': 15, 'base': 12, 'bear': 9},
            'cost_of_equity': {'bull': 12, 'base': 14, 'bear': 16},
            'excess_return_fade_years': 10,
            'applicable': False,
            'reasoning': 'Default — not a financial company'
        },
        'nav_assumptions': {
            'total_assets_cr': 0,
            'total_liabilities_cr': 0,
            'shares_outstanding_cr': 1,
            'asset_revaluation_pct': {'bull': 5, 'base': 0, 'bear': -5},
            'applicable': False,
            'reasoning': 'Default — not a holding company'
        },
        'analyst_consensus': {
            'median_target_price': 0,
            'mean_target_price': 0,
            'highest_target': 0,
            'lowest_target': 0,
            'buy_count': 0,
            'hold_count': 0,
            'sell_count': 0
        },
        'key_risks': ['Assumption generation failed — please verify all inputs'],
        'key_catalysts': ['AI assumption generation was unavailable']
    }
