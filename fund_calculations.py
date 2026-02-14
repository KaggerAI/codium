# calculations.py

import math

CALCULATION_REGISTRY = {}

def register_calculation(name):
    """Decorator to register a calculation function."""
    def decorator(func):
        CALCULATION_REGISTRY[name] = func
        return func
    return decorator

# --- Helper Functions ---

def _safe_float(value, default=None):
    """Safely convert a value to float, handling strings, None, commas, and percentages."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if math.isnan(value) or math.isinf(value):
            return default
        return float(value)
    if isinstance(value, str):
        s_value = value.replace(',', '').strip()
        is_percentage = s_value.endswith('%')
        if is_percentage:
            s_value = s_value[:-1].strip()
        
        if not s_value:
             return default
        try:
            num = float(s_value)
            if math.isnan(num) or math.isinf(num):
                return default
            # If original was a percentage string, it's already in % units.
            # If we want to convert it to decimal for calculation (e.g., 50% -> 0.5),
            # this is where it would happen.
            # For now, _safe_float returns the number as is.
            # Calculations using percentages will need to handle units.
            return num # return num / 100.0 if is_percentage else num #<- if conversion to decimal is needed
        except ValueError:
            return default
    return default

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

def _get_metric_from_table(data_table, metric_name, period_identifier):
    """
    Helper to extract a specific metric for a given period from a table.
    data_table: list of dicts (e.g., from last_analysis['fundamentals']['Annual Results'])
    metric_name: The value in the "" key (e.g., "Sales ", "Net Profit ")
    period_identifier: The column header for the period (e.g., "Mar 2024") or an integer index.
                       If int, -1 is latest, -2 is second latest, etc., from available data columns.
                       0 is the first data column, 1 is the second, etc.
    """
    if not data_table or not isinstance(data_table, list):
        return None
    
    for row in data_table:
        if row.get("") == metric_name:
            available_period_keys = [k for k in row.keys() if k not in ["", "index"]]
            if not available_period_keys:
                return None

            if isinstance(period_identifier, str):
                return row.get(period_identifier)
            elif isinstance(period_identifier, int):
                num_periods = len(available_period_keys)
                actual_idx = -1
                if period_identifier < 0:
                    if abs(period_identifier) <= num_periods:
                        actual_idx = num_periods + period_identifier
                    else: return None 
                elif period_identifier >= 0:
                    if period_identifier < num_periods:
                        actual_idx = period_identifier
                    else: return None
                
                if 0 <= actual_idx < num_periods:
                    actual_period_key = available_period_keys[actual_idx]
                    return row.get(actual_period_key)
    return None

def _get_input_value(retrieved_data, input_spec, allow_zero=True, treat_percentage_as_decimal=False):
    """
    Extracts a single float value based on the input_spec from the AI plan.
    input_spec: e.g., {"table": "Annual Results", "metric": "Net Profit", "period": -1} 
    allow_zero: If False, a parsed value of 0.0 will be treated as None (useful for denominators).
    treat_percentage_as_decimal: If True and value is like "50%", returns 0.5. Otherwise returns 50.0.
    """
    # Check if the input is already a calculated value from a previous step
    if input_spec.get("type") == "calculated":
        calc_metrics = retrieved_data.get("CalculatedMetrics", {})
        calc_data = calc_metrics.get(input_spec["source_key"])
        if calc_data and "value" in calc_data:
            val = _safe_float(calc_data["value"])
            if val == 0.0 and not allow_zero: return None
            # Percentage handling for calculated inputs should be based on how they were stored
            return val 
        else:
            return None

    # Standard retrieval from fundamental tables
    table_data = retrieved_data.get("Fundamentals", {}).get(input_spec["table"])
    if not table_data:
        return None
    
    raw_val_str = _get_metric_from_table(table_data, input_spec["metric"], input_spec["period"])
    
    # Special handling for strings that might be percentages
    is_percentage_string = isinstance(raw_val_str, str) and raw_val_str.strip().endswith('%')
    
    float_val = _safe_float(raw_val_str) # _safe_float currently doesn't convert % to decimal
    
    if float_val is None:
        return None

    if treat_percentage_as_decimal and is_percentage_string: # If it was like "50%", convert 50.0 to 0.5
        float_val /= 100.0
    
    if float_val == 0.0 and not allow_zero:
        return None
    return float_val

def _get_sum_of_metrics(retrieved_data, source_specs_list, allow_zero_components=True, treat_percentage_as_decimal=False):
    """
    Sums values for a list of metric sources.
    Returns the sum, or None if any essential component is missing.
    """
    total_sum = 0
    all_essential_found = True
    for spec in source_specs_list:
        is_optional = spec.get("optional", False)
        val = _get_input_value(retrieved_data, spec, allow_zero=allow_zero_components, treat_percentage_as_decimal=treat_percentage_as_decimal)
        
        if val is None:
            if not is_optional:
                all_essential_found = False
                break 
        else:
            total_sum += val
            
    return total_sum if all_essential_found else None

# --- A. Profitability Ratios ---

@register_calculation("calculate_gross_profit_margin")
def calculate_gross_profit_margin(retrieved_data, calculation_spec):
    """ GPM = (Gross Profit / Revenue) * 100 """
    inputs = calculation_spec['inputs']
    gross_profit = _get_input_value(retrieved_data, inputs["gross_profit"])
    revenue = _get_input_value(retrieved_data, inputs["revenue"], allow_zero=False)

    if gross_profit is None: return {"error": "Gross Profit not found/parseable."}
    if revenue is None: return {"value": None, "note": "Revenue is zero or not found/parseable."}
    
    margin = (gross_profit / revenue) * 100
    return {"value": round(margin, 2), "unit": "%"}

@register_calculation("calculate_operating_profit_margin")
def calculate_operating_profit_margin(retrieved_data, calculation_spec):
    """ OPM = (Operating Profit (EBIT) / Revenue) * 100 """
    inputs = calculation_spec['inputs']
    ebit = _get_input_value(retrieved_data, inputs["ebit"]) # Operating Profit
    revenue = _get_input_value(retrieved_data, inputs["revenue"], allow_zero=False)

    if ebit is None: return {"error": "Operating Profit (EBIT) not found/parseable."}
    if revenue is None: return {"value": None, "note": "Revenue is zero or not found/parseable."}
    
    margin = (ebit / revenue) * 100
    return {"value": round(margin, 2), "unit": "%"}

@register_calculation("calculate_net_profit_margin")
def calculate_net_profit_margin(retrieved_data, calculation_spec):
    """ NPM = (Net Profit / Revenue) * 100 """
    inputs = calculation_spec['inputs']
    net_profit = _get_input_value(retrieved_data, inputs["net_profit"])
    revenue = _get_input_value(retrieved_data, inputs["revenue"], allow_zero=False)

    if net_profit is None: return {"error": "Net Profit not found/parseable."}
    if revenue is None: return {"value": None, "note": "Revenue is zero or not found/parseable."}
    
    margin = (net_profit / revenue) * 100
    return {"value": round(margin, 2), "unit": "%"}

# EPS is usually directly available, but a calculation could be:
# (Net Income - Preferred Dividends) / Average Outstanding Shares
# Preferred dividends and average shares are often not directly in Screener basic tables.
# For now, assume EPS is fetched directly.

@register_calculation("calculate_return_on_equity") # Already provided, enhanced earlier
def calculate_return_on_equity(retrieved_data, calculation_spec):
    """ ROE = (Net Income / Average Shareholder Equity) * 100 """
    inputs = calculation_spec['inputs']
    net_income = _get_input_value(retrieved_data, inputs["net_income"])
    equity_current = _get_sum_of_metrics(retrieved_data, inputs["equity_current_sources"])
    equity_previous = _get_sum_of_metrics(retrieved_data, inputs["equity_previous_sources"])
    
    avg_equity, note = None, None
    if equity_current is not None and equity_previous is not None:
        avg_equity = (equity_current + equity_previous) / 2.0
    elif equity_current is not None and inputs.get("use_current_equity_if_avg_fails", False):
        avg_equity = equity_current
        note = "Calculated using current period equity only."
    
    if net_income is None: return {"error": "Net Income not found/parseable."}
    if avg_equity is None: return {"error": "Shareholder Equity for averaging not found/parseable."}
    if avg_equity == 0: return {"value": None, "note": "Average Shareholder Equity is zero."}
    
    roe = (net_income / avg_equity) * 100
    result = {"value": round(roe, 2), "unit": "%"}
    if note: result["note"] = note
    return result

@register_calculation("calculate_return_on_assets") # Already provided, enhanced earlier
def calculate_return_on_assets(retrieved_data, calculation_spec):
    """ ROA = (Net Income / Average Total Assets) * 100 """
    inputs = calculation_spec['inputs']
    net_income = _get_input_value(retrieved_data, inputs["net_income"])
    assets_current = _get_input_value(retrieved_data, inputs["assets_current"])
    assets_previous = _get_input_value(retrieved_data, inputs["assets_previous"])

    avg_assets, note = None, None
    if assets_current is not None and assets_previous is not None:
        avg_assets = (assets_current + assets_previous) / 2.0
    elif assets_current is not None and inputs.get("use_current_assets_if_avg_fails", False):
        avg_assets = assets_current
        note = "Calculated using current period assets only."

    if net_income is None: return {"error": "Net Income not found/parseable."}
    if avg_assets is None: return {"error": "Total Assets for averaging not found/parseable."}
    if avg_assets == 0: return {"value": None, "note": "Average Total Assets are zero."}

    roa = (net_income / avg_assets) * 100
    result = {"value": round(roa, 2), "unit": "%"}
    if note: result["note"] = note
    return result

@register_calculation("calculate_roce") # Already provided, enhanced earlier
def calculate_return_on_capital_employed(retrieved_data, calculation_spec):
    """ ROCE = EBIT / (Total Assets - Current Liabilities) * 100 (using end-of-period capital employed) """
    inputs = calculation_spec['inputs']
    ebit = _get_input_value(retrieved_data, inputs["ebit"])
    total_assets = _get_input_value(retrieved_data, inputs["total_assets"])
    current_liabilities = _get_sum_of_metrics(retrieved_data, inputs["current_liabilities_sources"])

    if ebit is None: return {"error": "EBIT not found/parseable."}
    if total_assets is None: return {"error": "Total Assets not found/parseable."}
    if current_liabilities is None: current_liabilities = 0 

    capital_employed = total_assets - current_liabilities
    if capital_employed == 0: return {"value": None, "note": "Capital Employed is zero."}

    roce = (ebit / capital_employed) * 100
    return {"value": round(roce, 2), "unit": "%"}

# --- B. Liquidity Ratios ---

@register_calculation("calculate_current_ratio") # Already provided
def calculate_current_ratio(retrieved_data, calculation_spec):
    inputs = calculation_spec['inputs']
    current_assets = _get_input_value(retrieved_data, inputs["current_assets"])
    current_liabilities = _get_input_value(retrieved_data, inputs["current_liabilities"], allow_zero=False)
    if current_assets is None: return {"error": "Current Assets not found/parseable."}
    if current_liabilities is None: return {"value": None, "note": "Current Liabilities are zero or not found/parseable."}
    return {"value": round(current_assets / current_liabilities, 2)}

@register_calculation("calculate_quick_ratio") # Already provided
def calculate_quick_ratio(retrieved_data, calculation_spec):
    inputs = calculation_spec['inputs']
    current_assets = _get_input_value(retrieved_data, inputs["current_assets"])
    inventory = _get_input_value(retrieved_data, inputs["inventory"])
    current_liabilities = _get_input_value(retrieved_data, inputs["current_liabilities"], allow_zero=False)
    if current_assets is None: return {"error": "Current Assets not found/parseable."}
    if inventory is None: inventory = 0 
    if current_liabilities is None: return {"value": None, "note": "Current Liabilities are zero or not found/parseable."}
    quick_assets = current_assets - inventory
    return {"value": round(quick_assets / current_liabilities, 2)}

# --- C. Solvency Ratios (Leverage Ratios) ---

@register_calculation("calculate_debt_to_equity") # Already provided
def calculate_debt_to_equity(retrieved_data, calculation_spec):
    inputs = calculation_spec['inputs']
    total_debt = _get_sum_of_metrics(retrieved_data, inputs["total_debt_sources"])
    total_equity = _get_sum_of_metrics(retrieved_data, inputs["total_equity_sources"], allow_zero_components=False)
    if total_debt is None: return {"error": "Total Debt components not found/parseable."}
    if total_equity is None: return {"value": None, "note": "Total Equity is zero or not found/parseable."}
    return {"value": round(total_debt / total_equity, 2)}

@register_calculation("calculate_debt_to_assets")
def calculate_debt_to_assets(retrieved_data, calculation_spec):
    """ Debt-to-Assets = Total Debt / Total Assets """
    inputs = calculation_spec['inputs']
    total_debt = _get_sum_of_metrics(retrieved_data, inputs["total_debt_sources"])
    total_assets = _get_input_value(retrieved_data, inputs["total_assets"], allow_zero=False)

    if total_debt is None: return {"error": "Total Debt components not found/parseable."}
    if total_assets is None: return {"value": None, "note": "Total Assets are zero or not found/parseable."}
    
    ratio = total_debt / total_assets
    return {"value": round(ratio, 2)}

@register_calculation("calculate_interest_coverage_ratio") # Already provided
def calculate_interest_coverage_ratio(retrieved_data, calculation_spec):
    inputs = calculation_spec['inputs']
    ebit = _get_input_value(retrieved_data, inputs["ebit"])
    interest = _get_input_value(retrieved_data, inputs["interest_expense"], allow_zero=False)
    if ebit is None: return {"error": "EBIT not found/parseable."}
    if interest is None: return {"value": None, "note": "Interest expense is zero or not found/parseable."}
    return {"value": round(ebit / interest, 2)}

# --- D. Efficiency Ratios ---

@register_calculation("calculate_asset_turnover_ratio")
def calculate_asset_turnover_ratio(retrieved_data, calculation_spec):
    """ Asset Turnover = Net Sales / Average Total Assets """
    inputs = calculation_spec['inputs']
    sales = _get_input_value(retrieved_data, inputs["sales"])
    assets_current = _get_input_value(retrieved_data, inputs["assets_current"])
    assets_previous = _get_input_value(retrieved_data, inputs["assets_previous"])

    avg_assets, note = None, None
    if assets_current is not None and assets_previous is not None:
        avg_assets = (assets_current + assets_previous) / 2.0
    elif assets_current is not None and inputs.get("use_current_assets_if_avg_fails", False):
        avg_assets = assets_current
        note = "Calculated using current period assets only."
    
    if sales is None: return {"error": "Sales not found/parseable."}
    if avg_assets is None: return {"error": "Assets for averaging not found/parseable."}
    if avg_assets == 0: return {"value": None, "note": "Average Total Assets are zero."}
    
    ratio = sales / avg_assets
    result = {"value": round(ratio, 2)}
    if note: result["note"] = note
    return result

@register_calculation("calculate_inventory_turnover_ratio")
def calculate_inventory_turnover_ratio(retrieved_data, calculation_spec):
    """ Inventory Turnover = COGS / Average Inventory """
    inputs = calculation_spec['inputs']
    # COGS might be a single metric or sum of components. Assume planner provides a single "cogs" value source.
    cogs = _get_input_value(retrieved_data, inputs["cogs"])
    inventory_current = _get_input_value(retrieved_data, inputs["inventory_current"])
    inventory_previous = _get_input_value(retrieved_data, inputs["inventory_previous"])

    avg_inventory, note = None, None
    if inventory_current is not None and inventory_previous is not None:
        avg_inventory = (inventory_current + inventory_previous) / 2.0
    elif inventory_current is not None and inputs.get("use_current_inventory_if_avg_fails", False):
        avg_inventory = inventory_current
        note = "Calculated using current period inventory only."

    if cogs is None: return {"error": "COGS not found/parseable."}
    if avg_inventory is None: return {"error": "Inventory for averaging not found/parseable."}
    if avg_inventory == 0: return {"value": None, "note": "Average Inventory is zero."}
    
    ratio = cogs / avg_inventory
    result = {"value": round(ratio, 2)}
    if note: result["note"] = note
    return result

@register_calculation("calculate_days_sales_outstanding")
def calculate_days_sales_outstanding(retrieved_data, calculation_spec):
    """ DSO = (Average Accounts Receivable / Revenue) * 365 """
    inputs = calculation_spec['inputs']
    # Screener "Financial Ratios" often has "Debtor Days". This function is if we need to calculate it.
    receivables_current = _get_input_value(retrieved_data, inputs["receivables_current"])
    receivables_previous = _get_input_value(retrieved_data, inputs["receivables_previous"])
    revenue = _get_input_value(retrieved_data, inputs["revenue"], allow_zero=False) # Revenue for the period matching receivables
    
    days_in_period = inputs.get("days_in_period", 365) # Annual default

    avg_receivables, note = None, None
    if receivables_current is not None and receivables_previous is not None:
        avg_receivables = (receivables_current + receivables_previous) / 2.0
    elif receivables_current is not None and inputs.get("use_current_receivables_if_avg_fails", False):
        avg_receivables = receivables_current
        note = "Calculated using current period receivables only."

    if avg_receivables is None: return {"error": "Accounts Receivable for averaging not found/parseable."}
    if revenue is None: return {"value": None, "note": "Revenue is zero or not found/parseable for DSO."}

    dso = (avg_receivables / revenue) * days_in_period
    result = {"value": round(dso, 1), "unit": "days"}
    if note: result["note"] = note
    return result
    
# --- E. Market Value Ratios ---
# These often require current market price, which is in 'summary'.
# The calculation orchestrator will need to pass this if required.

@register_calculation("calculate_price_to_earnings_ratio")
def calculate_price_to_earnings_ratio(retrieved_data, calculation_spec):
    """ P/E = Market Price per Share / EPS """
    inputs = calculation_spec['inputs']
    market_price = _safe_float(inputs.get("market_price")) # Passed directly by orchestrator
    # Accept both 'eps' and 'eps_source' keys for compatibility with different AI planner outputs
    eps_spec = inputs.get("eps") or inputs.get("eps_source")
    if eps_spec is None:
        return {"error": "EPS source specification not found in inputs (expected 'eps' or 'eps_source')."}
    eps = _get_input_value(retrieved_data, eps_spec, allow_zero=False) # EPS from fundamentals

    if market_price is None: return {"error": "Market Price not available."}
    if eps is None: return {"value": None, "note": "EPS is zero, negative, or not found/parseable."}
    if eps <= 0 : return {"value": None, "note": "EPS is zero or negative, P/E not meaningful."}
    
    pe_ratio = market_price / eps
    return {"value": round(pe_ratio, 2)}

@register_calculation("calculate_price_to_book_ratio")
def calculate_price_to_book_ratio(retrieved_data, calculation_spec):
    """ P/B = Market Price per Share / Book Value per Share """
    inputs = calculation_spec['inputs']
    market_price = _safe_float(inputs["market_price"])
    
    # Book Value per Share = Total Equity / Number of Shares
    # Number of shares might be tricky. Screener 'Equity Capital' is face value * num_shares.
    # If 'Face Value' is available, Num Shares = Equity Capital (from BS) / Face Value (from BS or summary)
    # Simpler: if 'Book Value Per Share' metric is directly available in fundamentals.
    
    bvps_input_type = inputs["book_value_per_share_source"].get("type", "direct_metric")
    book_value_per_share = None

    if bvps_input_type == "direct_metric":
        book_value_per_share = _get_input_value(retrieved_data, inputs["book_value_per_share_source"], allow_zero=False)
    elif bvps_input_type == "calculate":
        # Requires: total_equity_sources, num_shares_source (which itself might need equity_capital and face_value)
        # This becomes a nested calculation. For MVP, assume direct_metric or pre-calculated BVPS.
        # If we enhance, this function could call another to get BVPS.
        equity_sources = inputs["book_value_per_share_source"]["total_equity_sources"]
        num_shares_source = inputs["book_value_per_share_source"]["num_shares_source"] # Could be direct or calc
        
        total_equity = _get_sum_of_metrics(retrieved_data, equity_sources)
        
        num_shares = None
        if num_shares_source.get("type") == "direct_value": # If planner directly passes num_shares
            num_shares = _safe_float(num_shares_source.get("value"), default=0)
        elif num_shares_source.get("type") == "from_equity_and_face_value":
            equity_capital_val = _get_input_value(retrieved_data, num_shares_source["equity_capital_spec"])
            face_value_val = _get_input_value(retrieved_data, num_shares_source["face_value_spec"], allow_zero=False)
            if equity_capital_val is not None and face_value_val is not None:
                num_shares = equity_capital_val / face_value_val
        
        if total_equity is not None and num_shares is not None and num_shares > 0:
            book_value_per_share = total_equity / num_shares
        else:
            return {"error": "Could not calculate Book Value per Share from components."}
        if book_value_per_share == 0: book_value_per_share = None # for allow_zero=False behavior

    if market_price is None: return {"error": "Market Price not available."}
    if book_value_per_share is None or book_value_per_share <= 0: 
        return {"value": None, "note": "Book Value per Share is zero, negative or not found/parseable."}
        
    pb_ratio = market_price / book_value_per_share
    return {"value": round(pb_ratio, 2)}

@register_calculation("calculate_dividend_yield")
def calculate_dividend_yield(retrieved_data, calculation_spec):
    """ Dividend Yield = (Annual Dividends per Share / Market Price per Share) * 100 """
    inputs = calculation_spec['inputs']
    market_price = _safe_float(inputs["market_price"], default=0)
    annual_dps = _get_input_value(retrieved_data, inputs["annual_dps_source"]) # DPS directly or calculated

    if market_price == 0: return {"value": None, "note": "Market Price is zero or not available."}
    if annual_dps is None: return {"error": "Annual Dividend Per Share not found/parseable."}
    if annual_dps < 0: return {"value": None, "note": "Annual Dividend Per Share is negative."}

    yield_val = (annual_dps / market_price) * 100
    return {"value": round(yield_val, 2), "unit": "%"}

@register_calculation("calculate_dividend_payout_ratio")
def calculate_dividend_payout_ratio(retrieved_data, calculation_spec):
    """ Dividend Payout Ratio = (Dividend Per Share / EPS) * 100 """
    inputs = calculation_spec['inputs']
    dps = _get_input_value(retrieved_data, inputs["dps_source"])
    eps = _get_input_value(retrieved_data, inputs["eps_source"], allow_zero=False) # EPS as denominator

    if dps is None: return {"error": "Dividend Per Share not found/parseable."}
    if eps is None: return {"value": None, "note": "EPS is zero, negative or not found/parseable for payout ratio."}
    if eps <= 0 : return {"value": None, "note": "EPS is zero or negative, payout ratio not meaningful."}
    if dps < 0: return {"value": None, "note": "Dividend per share is negative."}
    
    payout_ratio = (dps / eps) * 100
    return {"value": round(payout_ratio, 2), "unit": "%"}
    
# --- II. Advanced Financial Data & Ratios ---

# A. Profitability & Performance

@register_calculation("calculate_ebitda") # Already provided
def calculate_ebitda(retrieved_data, calculation_spec):
    inputs = calculation_spec['inputs']
    ebit = _get_input_value(retrieved_data, inputs["ebit"])
    depreciation = _get_input_value(retrieved_data, inputs["depreciation"])
    note = None
    if ebit is None: return {"error": "EBIT (Operating Profit) not found/parseable."}
    if depreciation is None: 
        if not inputs["depreciation"].get("optional", True): # Default to optional
             return {"error": "Depreciation not found/parseable and was marked as non-optional."}
        depreciation = 0 
        note = "Depreciation data not found/parsed or was optional, assumed zero."
    ebitda = ebit + depreciation
    result = {"value": round(ebitda, 2)}
    if note: result['note'] = note
    return result

@register_calculation("calculate_ebitda_margin") # Already provided
def calculate_ebitda_margin(retrieved_data, calculation_spec):
    inputs = calculation_spec['inputs']
    ebitda_val = _get_input_value(retrieved_data, inputs["ebitda_input"]) # Assumes ebitda_input handles calculated/direct
    sales = _get_input_value(retrieved_data, inputs["sales"], allow_zero=False)
    if ebitda_val is None: return {"error": "EBITDA value not available for margin."}
    if sales is None: return {"value": None, "note": "Sales are zero or not found/parseable for EBITDA margin."}
    margin = (ebitda_val / sales) * 100
    return {"value": round(margin, 2), "unit": "%"}

@register_calculation("calculate_roic")
def calculate_return_on_invested_capital(retrieved_data, calculation_spec):
    """
    ROIC = (NOPAT / Invested Capital) * 100
    NOPAT = EBIT * (1 - Effective Tax Rate)
    Invested Capital = Total Debt + Total Equity - Non-Operating Cash - Other Non-Operating Assets (simplified)
    For Screener: Invested Capital = Total Assets - Non-Interest Bearing Current Liabilities (NIBCL)
                                  OR  Fixed Assets + Net Working Capital (excluding cash and debt)
    Simpler Screener approach for Invested Capital: (Shareholder Equity + Total Debt) - Cash & Equivalents
    """
    inputs = calculation_spec['inputs']
    
    # NOPAT
    ebit = _get_input_value(retrieved_data, inputs["ebit_for_nopat"])
    tax_rate_input = inputs["tax_rate_for_nopat"] # e.g. {"type": "direct_value", "value": 0.25} or {"metric": "Tax %", ...}
    
    tax_rate = None
    if tax_rate_input.get("type") == "direct_value":
        tax_rate = _safe_float(tax_rate_input.get("value"))
    elif tax_rate_input.get("type") == "from_income_statement":
        # Effective Tax Rate = Tax Expense / Profit Before Tax
        tax_expense = _get_input_value(retrieved_data, tax_rate_input["tax_expense_spec"])
        pbt = _get_input_value(retrieved_data, tax_rate_input["pbt_spec"], allow_zero=False)
        if tax_expense is not None and pbt is not None:
            tax_rate = tax_expense / pbt
        elif tax_expense is not None and tax_expense == 0 and pbt == 0 : # Both zero tax and zero PBT
            tax_rate = 0.0

    if ebit is None: return {"error": "EBIT not found for NOPAT calculation."}
    if tax_rate is None or tax_rate < 0 or tax_rate > 1: # Basic validation for tax rate as decimal
        # If tax rate is from a "Tax %" metric, it might be like 25.0, not 0.25
        # _get_input_value needs to handle this if `treat_percentage_as_decimal` is used.
        # For now, assume tax_rate is decimal if calculated, or planner needs to ensure it.
        return {"error": "Valid Tax Rate (0-1) not found/calculable for NOPAT."}
        
    nopat = ebit * (1 - tax_rate)

    # Invested Capital (Simplified: Total Debt + Shareholder Equity - Cash & Equivalents)
    total_debt = _get_sum_of_metrics(retrieved_data, inputs["total_debt_for_ic"])
    total_equity = _get_sum_of_metrics(retrieved_data, inputs["total_equity_for_ic"])
    cash_equivalents = _get_sum_of_metrics(retrieved_data, inputs["cash_equivalents_for_ic"]) # e.g., "Cash & Bank"

    if total_debt is None or total_equity is None:
        return {"error": "Debt or Equity components not found for Invested Capital."}
    if cash_equivalents is None: cash_equivalents = 0 # Assume zero if not specified/found

    invested_capital = (total_debt + total_equity) - cash_equivalents
    
    if invested_capital == 0: return {"value": None, "note": "Invested Capital is zero."}
    
    roic = (nopat / invested_capital) * 100
    return {"value": round(roic, 2), "unit": "%"}

# B. Cash Flow Ratios

@register_calculation("calculate_price_to_cash_flow_ratio")
def calculate_price_to_cash_flow_ratio(retrieved_data, calculation_spec):
    """ P/CF = Market Price per Share / Operating Cash Flow per Share """
    inputs = calculation_spec['inputs']
    market_price = _safe_float(inputs["market_price"])
    
    ocf = _get_input_value(retrieved_data, inputs["operating_cash_flow_source"])
    num_shares_source = inputs["num_shares_source"] # Similar to P/B's num_shares logic
    
    num_shares = None
    # Logic to get num_shares (copied from P/B, can be refactored to a helper)
    if num_shares_source.get("type") == "direct_value":
        num_shares = _safe_float(num_shares_source.get("value"), default=0)
    elif num_shares_source.get("type") == "from_equity_and_face_value":
        equity_capital_val = _get_input_value(retrieved_data, num_shares_source["equity_capital_spec"])
        face_value_val = _get_input_value(retrieved_data, num_shares_source["face_value_spec"], allow_zero=False)
        if equity_capital_val is not None and face_value_val is not None:
            num_shares = equity_capital_val / face_value_val
            
    if ocf is None: return {"error": "Operating Cash Flow not found/parseable."}
    if num_shares is None or num_shares == 0: return {"error": "Number of shares not available/zero."}
    
    ocf_per_share = ocf / num_shares
    
    if market_price is None: return {"error": "Market Price not available."}
    if ocf_per_share == 0: return {"value": None, "note": "Operating Cash Flow per Share is zero."}
    
    pcf_ratio = market_price / ocf_per_share
    return {"value": round(pcf_ratio, 2)}

@register_calculation("calculate_free_cash_flow") # Already provided
def calculate_free_cash_flow(retrieved_data, calculation_spec):
    inputs = calculation_spec['inputs']
    ocf = _get_input_value(retrieved_data, inputs["operating_cash_flow"])
    capex_val = _get_input_value(retrieved_data, {
        "table": "Cash Flow", 
        "metric": inputs["capex_metric_name"], 
        "period": inputs["capex_period"]
    })
    if ocf is None: return {"error": "Operating Cash Flow not found/parseable."}
    if capex_val is None: return {"error": f"Capex metric '{inputs['capex_metric_name']}' not found/parseable."}
    # Assumes capex_val is negative for purchase, so OCF + (negative_capex)
    fcf = ocf + capex_val 
    return {"value": round(fcf, 2)}

@register_calculation("calculate_fcf_yield")
def calculate_fcf_yield(retrieved_data, calculation_spec):
    """ FCF Yield = (FCF per Share / Market Price per Share) * 100 """
    inputs = calculation_spec['inputs']
    
    fcf_input = inputs["fcf_source"] # e.g. {"type": "calculated", "source_key": "Calculated_FCF"}
    fcf_val = _get_input_value(retrieved_data, fcf_input) # Helper handles "calculated" type

    market_price = _safe_float(inputs["market_price"], default = 0)
    num_shares_source = inputs["num_shares_source"]
    num_shares = None # Logic to get num_shares (as in P/CF)
    if num_shares_source.get("type") == "direct_value":
        num_shares = _safe_float(num_shares_source.get("value"), default=0)
    elif num_shares_source.get("type") == "from_equity_and_face_value":
        # ... (num_shares calculation as above) ...
        equity_capital_val = _get_input_value(retrieved_data, num_shares_source["equity_capital_spec"])
        face_value_val = _get_input_value(retrieved_data, num_shares_source["face_value_spec"], allow_zero=False)
        if equity_capital_val is not None and face_value_val is not None:
            num_shares = equity_capital_val / face_value_val

    if fcf_val is None: return {"error": "FCF not available for yield calculation."}
    if market_price == 0: return {"value": None, "note": "Market Price is zero or not available."}
    if num_shares is None or num_shares == 0: return {"error": "Number of shares not available/zero for FCF per share."}

    fcf_per_share = fcf_val / num_shares
    fcf_yield = (fcf_per_share / market_price) * 100
    return {"value": round(fcf_yield, 2), "unit": "%"}

# C. Solvency & Stability (Advanced) - DSCR and FCCR are very complex with typical Screener data.
# Placeholder structure, real implementation would need much more detailed schema or external data.
@register_calculation("calculate_dscr")
def calculate_debt_service_coverage_ratio(retrieved_data, calculation_spec):
    return {"value": None, "note": "DSCR calculation not fully implemented due to complexity of identifying total debt service from schema."}

@register_calculation("calculate_fccr")
def calculate_fixed_charge_coverage_ratio(retrieved_data, calculation_spec):
    return {"value": None, "note": "FCCR calculation not fully implemented due to complexity of identifying fixed charges from schema."}

# D. Efficiency (Advanced)

@register_calculation("calculate_days_payable_outstanding")
def calculate_days_payable_outstanding(retrieved_data, calculation_spec):
    """ DPO = (Average Accounts Payable / COGS) * 365 """
    inputs = calculation_spec['inputs']
    payables_current = _get_input_value(retrieved_data, inputs["payables_current"])
    payables_previous = _get_input_value(retrieved_data, inputs["payables_previous"])
    cogs = _get_input_value(retrieved_data, inputs["cogs"], allow_zero=False)
    days_in_period = inputs.get("days_in_period", 365)

    avg_payables, note = None, None
    if payables_current is not None and payables_previous is not None:
        avg_payables = (payables_current + payables_previous) / 2.0
    elif payables_current is not None and inputs.get("use_current_payables_if_avg_fails", False):
        avg_payables = payables_current
        note = "Calculated using current period payables only."

    if avg_payables is None: return {"error": "Accounts Payable for averaging not found/parseable."}
    if cogs is None: return {"value": None, "note": "COGS is zero or not found/parseable for DPO."}

    dpo = (avg_payables / cogs) * days_in_period
    result = {"value": round(dpo, 1), "unit": "days"}
    if note: result["note"] = note
    return result

@register_calculation("calculate_cash_conversion_cycle")
def calculate_cash_conversion_cycle(retrieved_data, calculation_spec):
    """ CCC = DSO + Inventory Outstanding Days - DPO """
    inputs = calculation_spec['inputs']
    # Assumes DSO, IOD, DPO are either direct values or calculated in prior steps
    dso = _get_input_value(retrieved_data, inputs["dso_source"]) # e.g. {"type":"calculated", "source_key":"Calculated_DSO"}
    
    # Inventory Outstanding Days = (Average Inventory / COGS) * 365
    # This might be a sub-calculation or a separate calculable metric.
    # For simplicity, let's assume IOD is also passed as a source.
    iod = _get_input_value(retrieved_data, inputs["inventory_outstanding_days_source"])
    dpo = _get_input_value(retrieved_data, inputs["dpo_source"])

    if dso is None: return {"error": "DSO not available for CCC."}
    if iod is None: return {"error": "Inventory Outstanding Days not available for CCC."}
    if dpo is None: return {"error": "DPO not available for CCC."}
    
    ccc = dso + iod - dpo
    return {"value": round(ccc, 1), "unit": "days"}

# E. Valuation (Advanced)

@register_calculation("calculate_enterprise_value")
def calculate_enterprise_value(retrieved_data, calculation_spec):
    """ EV = Market Cap + Total Debt - Cash & Cash Equivalents """
    inputs = calculation_spec['inputs']
    market_cap = _safe_float(inputs["market_cap"]) # Passed directly
    total_debt = _get_sum_of_metrics(retrieved_data, inputs["total_debt_sources"])
    cash_equivalents = _get_sum_of_metrics(retrieved_data, inputs["cash_equivalents_sources"])

    if market_cap is None: return {"error": "Market Cap not available."}
    if total_debt is None: return {"error": "Total Debt not found/parseable for EV."}
    if cash_equivalents is None: cash_equivalents = 0 # Assume zero if not found

    ev = market_cap + total_debt - cash_equivalents
    return {"value": round(ev, 2)} # Typically large number, not percentage

@register_calculation("calculate_ev_ebitda_ratio")
def calculate_ev_ebitda_ratio(retrieved_data, calculation_spec):
    """ EV/EBITDA = Enterprise Value / EBITDA """
    inputs = calculation_spec['inputs']
    ev = _get_input_value(retrieved_data, inputs["ev_source"]) # {"type":"calculated", "source_key":"Calculated_EV"}
    ebitda = _get_input_value(retrieved_data, inputs["ebitda_source"], allow_zero=False)

    if ev is None: return {"error": "Enterprise Value not available for EV/EBITDA."}
    if ebitda is None: return {"value": None, "note": "EBITDA is zero or not available for EV/EBITDA."}
    
    ratio = ev / ebitda
    return {"value": round(ratio, 2)}

@register_calculation("calculate_price_sales_ratio")
def calculate_price_sales_ratio(retrieved_data, calculation_spec):
    """ P/S = Market Cap / Total Revenue """
    inputs = calculation_spec['inputs']
    market_cap = _safe_float(inputs["market_cap"])
    revenue = _get_input_value(retrieved_data, inputs["revenue_source"], allow_zero=False)

    if market_cap is None: return {"error": "Market Cap not available."}
    if revenue is None: return {"value": None, "note": "Revenue is zero or not found/parseable for P/S."}
    
    ratio = market_cap / revenue
    return {"value": round(ratio, 2)}

@register_calculation("calculate_peg_ratio")
def calculate_peg_ratio(retrieved_data, calculation_spec):
    """ PEG = (P/E Ratio) / Annual EPS Growth Rate (%) """
    inputs = calculation_spec['inputs']
    pe_ratio = _get_input_value(retrieved_data, inputs["pe_ratio_source"]) # e.g. from calculated P/E
    # EPS growth rate should be in percentage form, e.g., 15 for 15%
    eps_growth_rate = _get_input_value(retrieved_data, inputs["eps_growth_rate_source"], allow_zero=False, treat_percentage_as_decimal=False)


    if pe_ratio is None: return {"error": "P/E Ratio not available for PEG."}
    if pe_ratio <=0 : return {"value": None, "note": "P/E ratio is zero or negative."}
    if eps_growth_rate is None: return {"value": None, "note": "EPS Growth Rate is zero or not available for PEG."}
    if eps_growth_rate == 0: return {"value": None, "note": "EPS Growth Rate is zero."} # Avoid division by zero
    # If growth rate is negative, PEG can be negative, which is sometimes reported.
    
    peg_ratio = pe_ratio / eps_growth_rate # eps_growth_rate expected as a percentage number e.g. 10 for 10%
    return {"value": round(peg_ratio, 2)}

# F. Growth Rates (YoY examples, QoQ and CAGR would need more specific period handling)

@register_calculation("calculate_sales_yoy_growth") # Already provided
def calculate_sales_yoy_growth(retrieved_data, calculation_spec):
    inputs = calculation_spec['inputs']
    current_sales = _get_input_value(retrieved_data, inputs["current_sales"])
    previous_sales = _get_input_value(retrieved_data, inputs["previous_sales"], allow_zero=False)
    if current_sales is None: return {"error": "Current sales data not found/parseable."}
    if previous_sales is None: return {"value": None, "note": "Previous period sales are zero or not found/parseable."}
    growth = ((current_sales / previous_sales) - 1) * 100
    return {"value": round(growth, 2), "unit": "%"}

@register_calculation("calculate_eps_yoy_growth")
def calculate_eps_yoy_growth(retrieved_data, calculation_spec):
    """ EPS YoY Growth """
    inputs = calculation_spec['inputs']
    current_eps = _get_input_value(retrieved_data, inputs["current_eps"])
    previous_eps = _get_input_value(retrieved_data, inputs["previous_eps"]) # Not allowing zero for previous_eps if it implies no growth from 0.

    if current_eps is None: return {"error": "Current EPS not found/parseable."}
    if previous_eps is None: return {"error": "Previous EPS not found/parseable."}

    # Handle growth from zero or negative base
    if previous_eps == 0:
        return {"value": None, "note": "Cannot calculate YoY growth from zero previous EPS." if current_eps != 0 else "No change from zero EPS."}
    if previous_eps < 0:
        # Growth from a negative base can be misleading if it turns positive, or becomes less negative.
        # Standard percentage change is ((New - Old) / abs(Old)) if a meaningful direction is needed.
        # Or simply state the change. For now, a simple calculation:
        growth = ((current_eps - previous_eps) / abs(previous_eps)) * 100 # Growth relative to magnitude of previous
        return {"value": round(growth, 2), "unit": "%", "note": "Calculated based on absolute value of previous negative EPS."}

    growth = ((current_eps / previous_eps) - 1) * 100
    return {"value": round(growth, 2), "unit": "%"}

@register_calculation("calculate_net_profit_yoy_growth")
def calculate_net_profit_yoy_growth(retrieved_data, calculation_spec):
    """ Net Profit YoY Growth """
    inputs = calculation_spec['inputs']
    current_profit = _get_input_value(retrieved_data, inputs["current_profit"])
    previous_profit = _get_input_value(retrieved_data, inputs["previous_profit"])

    if current_profit is None: return {"error": "Current Net Profit not found/parseable."}
    if previous_profit is None: return {"error": "Previous Net Profit not found/parseable."}

    if previous_profit == 0:
        return {"value": None, "note": "Cannot calculate YoY growth from zero previous profit." if current_profit != 0 else "No change from zero profit."}
    if previous_profit < 0:
        growth = ((current_profit - previous_profit) / abs(previous_profit)) * 100
        return {"value": round(growth, 2), "unit": "%", "note": "Calculated based on absolute value of previous negative profit."}

    growth = ((current_profit / previous_profit) - 1) * 100
    return {"value": round(growth, 2), "unit": "%"}

# and the calculation function to iterate/process them. For MVP, focusing on YoY.

# --- III. Forensic Ratios & Scores ---

@register_calculation("calculate_altman_zscore")
def calculate_altman_zscore(retrieved_data, calculation_spec):
    """
    Altman Z-Score (Manufacturing): Z = 1.2*X1 + 1.4*X2 + 3.3*X3 + 0.6*X4 + 1.0*X5
    X1 = Working Capital / Total Assets
    X2 = Retained Earnings (Reserves) / Total Assets
    X3 = Operating Profit (EBIT) / Total Assets
    X4 = Market Cap / Total Liabilities
    X5 = Sales / Total Assets
    """
    inputs = calculation_spec.get('inputs', {})
    period = inputs.get('period', -1)
    
    total_assets = _get_input_value(retrieved_data, {"table": "Balance Sheet", "metric": "Total Assets", "period": period}, allow_zero=False)
    if total_assets is None: return {"error": "Total Assets not found/parseable."}

    # X1: Working Capital / Total Assets
    # Fallback: Working Capital = Other Assets - Other Liabilities
    other_assets = _get_input_value(retrieved_data, {"table": "Balance Sheet", "metric": "Other Assets", "period": period})
    other_liabilities = _get_input_value(retrieved_data, {"table": "Balance Sheet", "metric": "Other Liabilities", "period": period})
    
    working_cap = None
    if other_assets is not None and other_liabilities is not None:
        working_cap = other_assets - other_liabilities
    
    x1 = _safe_div(working_cap, total_assets) if working_cap is not None else None

    # X2: Retained Earnings / Total Assets
    # In Screener, Reserves is a proxy for Retained Earnings
    reserves = _get_input_value(retrieved_data, {"table": "Balance Sheet", "metric": "Reserves", "period": period})
    x2 = _safe_div(reserves, total_assets)

    # X3: Operating Profit / Total Assets
    op_profit = _get_input_value(retrieved_data, {"table": "Annual Results", "metric": "Operating Profit", "period": period})
    x3 = _safe_div(op_profit, total_assets)

    # X4: Market Cap / Total Liabilities
    # Market Cap is usually passed by the orchestrator in calculation_spec inputs
    market_cap = _safe_float(inputs.get("market_cap"))
    total_liabilities = _get_input_value(retrieved_data, {"table": "Balance Sheet", "metric": "Total Liabilities", "period": period}, allow_zero=False)
    
    # Fallback for Total Liabilities: Borrowings + Other Liabilities (approx) or Total Assets - Equity
    if total_liabilities is None:
        equity_cap = _get_input_value(retrieved_data, {"table": "Balance Sheet", "metric": "Equity Capital", "period": period})
        if equity_cap is not None and reserves is not None:
            # Total Liabilities (External) = Total Assets - Total Equity
            total_liabilities = total_assets - (equity_cap + reserves)
            
    x4 = _safe_div(market_cap, total_liabilities)

    # X5: Sales / Total Assets
    sales = _get_input_value(retrieved_data, {"table": "Annual Results", "metric": "Sales", "period": period})
    x5 = _safe_div(sales, total_assets)

    variables = {
        "X1 (WC/TA)": round(x1, 3) if x1 is not None else "N/A",
        "X2 (Reserves/TA)": round(x2, 3) if x2 is not None else "N/A",
        "X3 (EBIT/TA)": round(x3, 3) if x3 is not None else "N/A",
        "X4 (MCap/Liab)": round(x4, 3) if x4 is not None else "N/A",
        "X5 (Sales/TA)": round(x5, 3) if x5 is not None else "N/A"
    }

    # Calculate final score if at least 4 variables are present
    valid_vars = [v for v in [x1, x2, x3, x4, x5] if v is not None]
    if len(valid_vars) < 4:
        return {"value": None, "variables": variables, "note": "Insufficient base metrics for Z-Score calculation."}

    # Use defaults for missing
    z_score = 1.2*(x1 or 0) + 1.4*(x2 or 0) + 3.3*(x3 or 0) + 0.6*(x4 or 0) + 1.0*(x5 or 0)
    
    flag = "🟢" if z_score > 2.99 else ("🔴" if z_score < 1.81 else "🟡")
    interpretation = "Safe" if z_score > 2.99 else ("Distress" if z_score < 1.81 else "Grey Zone")

    return {
        "value": round(z_score, 2),
        "flag": flag,
        "interpretation": interpretation,
        "variables": variables,
        "unit": "indices"
    }


@register_calculation("calculate_beneish_mscore")
def calculate_beneish_mscore(retrieved_data, calculation_spec):
    """
    Beneish M-Score (5-Variable Model): 
    M = -6.065 + 0.823*DSRI + 0.906*GMI + 0.593*AQI + 0.717*SGI + 0.107*LVGI
    """
    inputs = calculation_spec.get('inputs', {})
    p0 = inputs.get('period', -1)   # Current period
    p1 = p0 - 1                     # Prior period

    def get_annual(metric, period):
        return _get_input_value(retrieved_data, {"table": "Annual Results", "metric": metric, "period": period})
    
    def get_bs(metric, period):
        return _get_input_value(retrieved_data, {"table": "Balance Sheet", "metric": metric, "period": period})

    # DSRI: Days Sales in Receivables Index
    dd0 = _get_input_value(retrieved_data, {"table": "Financial Ratios", "metric": "Debtor Days", "period": p0})
    dd1 = _get_input_value(retrieved_data, {"table": "Financial Ratios", "metric": "Debtor Days", "period": p1})
    dsri = _safe_div(dd0, dd1)

    # GMI: Gross Margin Index
    s0, s1 = get_annual("Sales", p0), get_annual("Sales", p1)
    e0, e1 = get_annual("Expenses", p0), get_annual("Expenses", p1)
    
    gmi = None
    if all(v is not None for v in [s0, s1, e0, e1]) and s0 > 0 and s1 > 0:
        m0 = (s0 - e0) / s0
        m1 = (s1 - e1) / s1
        gmi = _safe_div(m1, m0)

    # AQI: Asset Quality Index
    fixed0, fixed1 = get_bs("Fixed Assets", p0), get_bs("Fixed Assets", p1)
    ta0, ta1 = get_bs("Total Assets", p0), get_bs("Total Assets", p1)
    
    aqi = None
    if all(v is not None for v in [fixed0, fixed1, ta0, ta1]) and ta0 > 0 and ta1 > 0:
        q0 = 1 - (fixed0 / ta0)
        q1 = 1 - (fixed1 / ta1)
        aqi = _safe_div(q0, q1)

    # SGI: Sales Growth Index
    sgi = _safe_div(s0, s1)

    # LVGI: Leverage Index
    debt0, debt1 = get_bs("Borrowings", p0), get_bs("Borrowings", p1)
    liab0, liab1 = get_bs("Other Liabilities", p0), get_bs("Other Liabilities", p1)
    
    lvgi = None
    if all(v is not None for v in [debt0, debt1, liab0, liab1, ta0, ta1]) and ta0 > 0 and ta1 > 0:
        l0 = (debt0 + liab0) / ta0
        l1 = (debt1 + liab1) / ta1
        lvgi = _safe_div(l0, l1)

    variables = {
        "DSRI": round(dsri, 3) if dsri is not None else "N/A",
        "GMI": round(gmi, 3) if gmi is not None else "N/A",
        "AQI": round(aqi, 3) if aqi is not None else "N/A",
        "SGI": round(sgi, 3) if sgi is not None else "N/A",
        "LVGI": round(lvgi, 3) if lvgi is not None else "N/A"
    }

    # Calculate M-Score if at least 3 variables are present
    valid_vars = [v for v in [dsri, gmi, aqi, sgi, lvgi] if v is not None]
    if len(valid_vars) < 3:
        return {"value": None, "variables": variables, "note": "Insufficient data (multi-period) for M-Score."}

    # Use 1.0 as neutral for missing variables
    m = -6.065 + 0.823*(dsri or 1.0) + 0.906*(gmi or 1.0) + 0.593*(aqi or 1.0) + 0.717*(sgi or 1.0) + 0.107*(lvgi or 1.0)
    
    flag = "🟢" if m < -2.22 else ("🔴" if m > -1.78 else "🟡")
    interpretation = "Unlikely Manipulation" if m < -2.22 else ("Likely Manipulation" if m > -1.78 else "Warning")

    return {
        "value": round(m, 2),
        "flag": flag,
        "interpretation": interpretation,
        "variables": variables,
        "unit": "indices"
    }
