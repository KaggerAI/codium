# calculations.py

import math # For isnan, isinf

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
        if math.isnan(value) or math.isinf(value): # Handle actual NaN/inf from data
            return default
        return float(value)
    if isinstance(value, str):
        s_value = value.replace(',', '').strip()
        is_percentage = s_value.endswith('%')
        if is_percentage:
            s_value = s_value[:-1].strip()
        
        if not s_value: # Handle empty strings after stripping
             return default
        try:
            num = float(s_value)
            if math.isnan(num) or math.isinf(num):
                return default
            return num / 100.0 if is_percentage else num
        except ValueError:
            return default
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
            # Get available period keys, excluding the metric name key ("") and any "index" key
            # Sort them to ensure consistent indexing if Screener ever changes column order (unlikely for periods)
            # For now, rely on existing order.
            available_period_keys = [k for k in row.keys() if k not in ["", "index"]]
            if not available_period_keys:
                return None

            if isinstance(period_identifier, str): # Period is a string like "Mar 2024"
                return row.get(period_identifier)
            elif isinstance(period_identifier, int): # Period is a relative index
                num_periods = len(available_period_keys)
                actual_idx = -1
                if period_identifier < 0: # Negative index, from the end
                    if abs(period_identifier) <= num_periods:
                        actual_idx = num_periods + period_identifier
                    else: # Index out of bounds
                        return None 
                elif period_identifier >= 0: # Positive index, from the start
                    if period_identifier < num_periods:
                        actual_idx = period_identifier
                    else: # Index out of bounds
                        return None
                
                if 0 <= actual_idx < num_periods:
                    actual_period_key = available_period_keys[actual_idx]
                    return row.get(actual_period_key)
    return None

def _get_input_value(retrieved_data, input_spec, allow_zero=True):
    """
    Extracts a single float value based on the input_spec from the AI plan.
    input_spec: e.g., {"table": "Annual Results", "metric": "Net Profit", "period": -1} 
    allow_zero: If False, a parsed value of 0.0 will be treated as None (useful for denominators).
    """
    table_data = retrieved_data.get("Fundamentals", {}).get(input_spec["table"])
    if not table_data:
        return None
    
    raw_val = _get_metric_from_table(table_data, input_spec["metric"], input_spec["period"])
    float_val = _safe_float(raw_val)
    
    if float_val == 0.0 and not allow_zero:
        return None
    return float_val

def _get_sum_of_metrics(retrieved_data, source_specs_list, allow_zero_components=True):
    """
    Sums values for a list of metric sources.
    source_specs_list: list of dicts, e.g., 
        [{"table": "Balance Sheet", "metric": "Equity Capital", "period": -1}, ...]
    Returns the sum, or None if any essential component is missing.
    """
    total_sum = 0
    all_essential_found = True
    for spec in source_specs_list:
        is_optional = spec.get("optional", False) # Planner can mark if a component is optional
        val = _get_input_value(retrieved_data, spec, allow_zero=allow_zero_components)
        
        if val is None:
            if not is_optional:
                all_essential_found = False
                break 
            # If optional and missing, just skip (effectively adds 0)
        else:
            total_sum += val
            
    return total_sum if all_essential_found else None

# --- Calculation Functions ---

@register_calculation("calculate_current_ratio")
def calculate_current_ratio(retrieved_data, calculation_spec):
    """
    Calculates Current Ratio = Current Assets / Current Liabilities.
    Expected inputs in calculation_spec:
      "current_assets": {"table": "Balance Sheet", "metric": "Total Current Assets", "period": -1},
      "current_liabilities": {"table": "Balance Sheet", "metric": "Total Current Liabilities", "period": -1}
    """
    try:
        inputs = calculation_spec['inputs']
        current_assets = _get_input_value(retrieved_data, inputs["current_assets"])
        current_liabilities = _get_input_value(retrieved_data, inputs["current_liabilities"], allow_zero=False)

        if current_assets is None: return {"error": "Current Assets not found/parseable."}
        if current_liabilities is None: return {"value": None, "note": "Current Liabilities are zero, zero or not found/parseable."}
        
        ratio = current_assets / current_liabilities
        return {"value": round(ratio, 2)}
    except Exception as e:
        return {"error": f"Error calculating Current Ratio: {str(e)}"}

@register_calculation("calculate_quick_ratio")
def calculate_quick_ratio(retrieved_data, calculation_spec):
    """
    Calculates Quick Ratio = (Current Assets - Inventory) / Current Liabilities.
    Expected inputs:
      "current_assets": {"table": "Balance Sheet", "metric": "Total Current Assets", "period": -1},
      "inventory": {"table": "Balance Sheet", "metric": "Inventories", "period": -1},
      "current_liabilities": {"table": "Balance Sheet", "metric": "Total Current Liabilities", "period": -1}
    """
    try:
        inputs = calculation_spec['inputs']
        current_assets = _get_input_value(retrieved_data, inputs["current_assets"])
        inventory = _get_input_value(retrieved_data, inputs["inventory"]) # Inventory can be 0
        current_liabilities = _get_input_value(retrieved_data, inputs["current_liabilities"], allow_zero=False)

        if current_assets is None: return {"error": "Current Assets not found/parseable."}
        if inventory is None: inventory = 0 # Assume 0 if not found, or planner can make it non-optional
        if current_liabilities is None: return {"value": None, "note": "Current Liabilities are zero or not found/parseable."}

        quick_assets = current_assets - inventory
        ratio = quick_assets / current_liabilities
        return {"value": round(ratio, 2)}
    except Exception as e:
        return {"error": f"Error calculating Quick Ratio: {str(e)}"}

@register_calculation("calculate_debt_to_equity")
def calculate_debt_to_equity(retrieved_data, calculation_spec):
    """
    Calculates Debt-to-Equity Ratio = Total Debt / Total Shareholder Equity.
    Expected inputs:
      "total_debt_sources": [
          {"table": "Balance Sheet", "metric": "Borrowings", "period": -1} 
          // Could be more detailed: "Short Term Borrowings", "Long Term Borrowings"
      ],
      "total_equity_sources": [
          {"table": "Balance Sheet", "metric": "Equity Capital", "period": -1},
          {"table": "Balance Sheet", "metric": "Reserves", "period": -1, "optional": True} 
          // Assuming Reserves can be optional for sum, if not Equity Capital.
      ]
    """
    try:
        inputs = calculation_spec['inputs']
        total_debt = _get_sum_of_metrics(retrieved_data, inputs["total_debt_sources"], allow_zero_components=True)
        total_equity = _get_sum_of_metrics(retrieved_data, inputs["total_equity_sources"], allow_zero_components=False) # Equity as denominator can't be 0

        if total_debt is None: return {"error": "Total Debt components not found/parseable."}
        if total_equity is None: return {"value": None, "note": "Total Equity is zero or not found/parseable."}
            
        ratio = total_debt / total_equity
        return {"value": round(ratio, 2)}
    except Exception as e:
        return {"error": f"Error calculating Debt-to-Equity: {str(e)}"}

@register_calculation("calculate_return_on_equity")
def calculate_return_on_equity(retrieved_data, calculation_spec):
    """
    Calculates Return on Equity (ROE) = Net Income / Average Shareholder Equity.
    Expected inputs:
      "net_income": {"table": "Annual Results", "metric": "Net Profit ", "period": -1},
      "equity_current_sources": [{"table": "Balance Sheet", "metric": "Equity Capital", "period": -1}, ...],
      "equity_previous_sources": [{"table": "Balance Sheet", "metric": "Equity Capital", "period": -2}, ...],
      "use_current_equity_if_avg_fails": False (optional flag, defaults to False)
    """
    try:
        inputs = calculation_spec['inputs']
        net_income = _get_input_value(retrieved_data, inputs["net_income"])

        equity_current = _get_sum_of_metrics(retrieved_data, inputs["equity_current_sources"])
        equity_previous = _get_sum_of_metrics(retrieved_data, inputs["equity_previous_sources"])
        
        avg_equity = None
        note = None

        if equity_current is not None and equity_previous is not None:
            avg_equity = (equity_current + equity_previous) / 2.0
        elif equity_current is not None and inputs.get("use_current_equity_if_avg_fails", False):
            avg_equity = equity_current
            note = "Calculated using current period equity only (previous period data unavailable/unparseable)."
        
        if net_income is None: return {"error": "Net Income not found/parseable for ROE."}
        if avg_equity is None: return {"error": "Shareholder Equity for averaging not found/parseable for ROE."}
        if avg_equity == 0: return {"value": None, "note": "Average Shareholder Equity is zero."}
        
        roe = (net_income / avg_equity) * 100
        result = {"value": round(roe, 2), "unit": "%"}
        if note: result["note"] = note
        return result
    except Exception as e:
        return {"error": f"Error calculating ROE: {str(e)}"}

@register_calculation("calculate_return_on_assets")
def calculate_return_on_assets(retrieved_data, calculation_spec):
    """
    Calculates Return on Assets (ROA) = Net Income / Average Total Assets.
    Expected inputs:
      "net_income": {"table": "Annual Results", "metric": "Net Profit ", "period": -1},
      "assets_current": {"table": "Balance Sheet", "metric": "Total Assets", "period": -1},
      "assets_previous": {"table": "Balance Sheet", "metric": "Total Assets", "period": -2},
      "use_current_assets_if_avg_fails": False (optional flag, defaults to False)
    """
    try:
        inputs = calculation_spec['inputs']
        net_income = _get_input_value(retrieved_data, inputs["net_income"])
        assets_current = _get_input_value(retrieved_data, inputs["assets_current"])
        assets_previous = _get_input_value(retrieved_data, inputs["assets_previous"])

        avg_assets = None
        note = None

        if assets_current is not None and assets_previous is not None:
            avg_assets = (assets_current + assets_previous) / 2.0
        elif assets_current is not None and inputs.get("use_current_assets_if_avg_fails", False):
            avg_assets = assets_current
            note = "Calculated using current period assets only (previous period data unavailable/unparseable)."

        if net_income is None: return {"error": "Net Income not found/parseable for ROA."}
        if avg_assets is None: return {"error": "Total Assets for averaging not found/parseable for ROA."}
        if avg_assets == 0: return {"value": None, "note": "Average Total Assets are zero."}

        roa = (net_income / avg_assets) * 100
        result = {"value": round(roa, 2), "unit": "%"}
        if note: result["note"] = note
        return result
    except Exception as e:
        return {"error": f"Error calculating ROA: {str(e)}"}

@register_calculation("calculate_roce")
def calculate_return_on_capital_employed(retrieved_data, calculation_spec):
    """
    Calculates Return on Capital Employed (ROCE) = EBIT / (Total Assets - Current Liabilities).
    Using end-of-period capital employed.
    Expected inputs:
      "ebit": {"table": "Annual Results", "metric": "Operating Profit ", "period": -1},
      "total_assets": {"table": "Balance Sheet", "metric": "Total Assets", "period": -1}, 
      "current_liabilities_sources": [
          {"table": "Balance Sheet", "metric": "Total Current Liabilities", "period": -1} 
          // Or sum of individual current liability components
      ] 
    """
    try:
        inputs = calculation_spec['inputs']
        ebit = _get_input_value(retrieved_data, inputs["ebit"])
        total_assets = _get_input_value(retrieved_data, inputs["total_assets"])
        current_liabilities = _get_sum_of_metrics(retrieved_data, inputs["current_liabilities_sources"])

        if ebit is None: return {"error": "EBIT (Operating Profit) not found/parseable for ROCE."}
        if total_assets is None: return {"error": "Total Assets not found/parseable for ROCE."}
        if current_liabilities is None: current_liabilities = 0 # Or make non-optional by planner

        capital_employed = total_assets - current_liabilities
        if capital_employed == 0: return {"value": None, "note": "Capital Employed (Total Assets - Current Liabilities) is zero."}

        roce = (ebit / capital_employed) * 100
        return {"value": round(roce, 2), "unit": "%"}
    except Exception as e:
        return {"error": f"Error calculating ROCE: {str(e)}"}

@register_calculation("calculate_ebitda")
def calculate_ebitda(retrieved_data, calculation_spec):
    """
    Calculates EBITDA = EBIT (Operating Profit) + Depreciation & Amortization.
    Expected inputs:
      "ebit": {"table": "Annual Results", "metric": "Operating Profit ", "period": -1},
      "depreciation": {"table": "Annual Results", "metric": "Depreciation ", "period": -1, "optional": True}
    """
    try:
        inputs = calculation_spec['inputs']
        ebit = _get_input_value(retrieved_data, inputs["ebit"])
        depreciation = _get_input_value(retrieved_data, inputs["depreciation"])

        if ebit is None: return {"error": "EBIT (Operating Profit) not found/parseable."}
        
        note = None
        if depreciation is None: 
            if not inputs["depreciation"].get("optional", False):
                 return {"error": "Depreciation not found/parseable and was marked as non-optional."}
            depreciation = 0 
            note = "Depreciation data not found/parsed or was optional, assumed zero for EBITDA calculation."

        ebitda = ebit + depreciation
        result = {"value": round(ebitda, 2)}
        if note: result['note'] = note
        return result
    except Exception as e:
        return {"error": f"Error calculating EBITDA: {str(e)}"}

@register_calculation("calculate_ebitda_margin")
def calculate_ebitda_margin(retrieved_data, calculation_spec):
    """
    Calculates EBITDA Margin = (EBITDA / Sales) * 100.
    Requires EBITDA to be available (either from a previous calculation step or directly fetched).
    Expected inputs:
      "ebitda_input": { // Specifies how to get EBITDA
          "type": "calculated", // "calculated" or "direct"
          "source_key_or_spec": "Calculated_EBITDA_Key" // if "calculated", key in CalculatedMetrics
                               // OR {"table": "...", "metric": "EBITDA", "period": -1} if "direct"
      },
      "sales": {"table": "Annual Results", "metric": "Sales ", "period": -1} // Period should match EBITDA's period
    """
    try:
        inputs = calculation_spec['inputs']
        
        ebitda_val = None
        ebitda_input_spec = inputs["ebitda_input"]

        if ebitda_input_spec["type"] == "calculated":
            calc_metrics = retrieved_data.get("CalculatedMetrics", {}) # Assumes orchestrator puts results here
            ebitda_data_obj = calc_metrics.get(ebitda_input_spec["source_key_or_spec"])
            if ebitda_data_obj and "value" in ebitda_data_obj:
                ebitda_val = _safe_float(ebitda_data_obj["value"])
            else:
                return {"error": f"Prerequisite calculated EBITDA '{ebitda_input_spec['source_key_or_spec']}' not found or invalid."}
        elif ebitda_input_spec["type"] == "direct":
             ebitda_val = _get_input_value(retrieved_data, ebitda_input_spec["source_key_or_spec"])
        else:
            return {"error": "Invalid 'type' for ebitda_input in spec."}

        sales = _get_input_value(retrieved_data, inputs["sales"], allow_zero=False)

        if ebitda_val is None: return {"error": "EBITDA value not available for margin calculation."}
        if sales is None: return {"value": None, "note": "Sales are zero or not found/parseable for EBITDA margin."}

        margin = (ebitda_val / sales) * 100
        return {"value": round(margin, 2), "unit": "%"}
    except Exception as e:
        return {"error": f"Error calculating EBITDA Margin: {str(e)}"}

@register_calculation("calculate_sales_yoy_growth")
def calculate_sales_yoy_growth(retrieved_data, calculation_spec):
    """
    Calculates Sales YoY Growth = ((Current Sales / Previous Sales) - 1) * 100.
    Expected inputs:
      "current_sales": {"table": "Annual Results", "metric": "Sales ", "period": -1}, // Most recent
      "previous_sales": {"table": "Annual Results", "metric": "Sales ", "period": -2}  // One before
    """
    try:
        inputs = calculation_spec['inputs']
        current_sales = _get_input_value(retrieved_data, inputs["current_sales"])
        previous_sales = _get_input_value(retrieved_data, inputs["previous_sales"], allow_zero=False)

        if current_sales is None: return {"error": "Current sales data not found/parseable."}
        if previous_sales is None: return {"value": None, "note": "Previous period sales are zero or not found/parseable."}
        
        growth = ((current_sales / previous_sales) - 1) * 100
        return {"value": round(growth, 2), "unit": "%"}
    except Exception as e:
        return {"error": f"Error calculating Sales YoY Growth: {str(e)}"}

@register_calculation("calculate_free_cash_flow")
def calculate_free_cash_flow(retrieved_data, calculation_spec):
    """
    Calculates Free Cash Flow (FCF) = Operating Cash Flow + Capex (where Capex is typically negative).
    Screener.in "Cash Flow" usually has "Fixed Assets Purchased" as a negative value for capex.
    Expected inputs:
      "operating_cash_flow": {"table": "Cash Flow", "metric": "Cash from Operating Activity ", "period": -1},
      "capex_metric_name": "Fixed Assets Purchased", // Name of the capex line item in Cash Flow table
      "capex_period": -1 // Period for capex metric
    """
    try:
        inputs = calculation_spec['inputs']
        ocf = _get_input_value(retrieved_data, inputs["operating_cash_flow"])
        
        # Capex from Screener.in is usually negative for "Fixed Assets Purchased"
        capex_spec = {
            "table": "Cash Flow", # Assuming Capex is in "Cash Flow" table
            "metric": inputs["capex_metric_name"],
            "period": inputs["capex_period"]
        }
        capex_val = _get_input_value(retrieved_data, capex_spec)

        if ocf is None: return {"error": "Operating Cash Flow not found/parseable."}
        if capex_val is None: 
            return {"error": f"Capex metric '{inputs['capex_metric_name']}' not found/parseable."}
        
        # If capex_val is for "Fixed Assets Purchased", it's an outflow (negative).
        # FCF = OCF + (negative capex_val for purchase)
        # e.g. OCF = 100, Capex (Fixed Assets Purchased) = -30. FCF = 100 + (-30) = 70.
        # If the planner provides a capex metric that's a positive number representing expenditure,
        # then the formula would be OCF - Capex. The planner needs to be aware.
        # This function assumes the capex_val provided is signed correctly as per CF statement.
        fcf = ocf + capex_val 
        
        return {"value": round(fcf, 2)}
    except Exception as e:
        return {"error": f"Error calculating Free Cash Flow: {str(e)}"}

# --- Add more calculation functions as needed ---
# Example: Interest Coverage Ratio
@register_calculation("calculate_interest_coverage_ratio")
def calculate_interest_coverage_ratio(retrieved_data, calculation_spec):
    """
    Calculates Interest Coverage Ratio = EBIT / Interest Expense.
    Expected inputs:
      "ebit": {"table": "Annual Results", "metric": "Operating Profit ", "period": -1},
      "interest_expense": {"table": "Annual Results", "metric": "Interest ", "period": -1}
    """
    try:
        inputs = calculation_spec['inputs']
        ebit = _get_input_value(retrieved_data, inputs["ebit"])
        interest = _get_input_value(retrieved_data, inputs["interest_expense"], allow_zero=False) # Interest as denominator

        if ebit is None: return {"error": "EBIT (Operating Profit) not found/parseable."}
        if interest is None: return {"value": None, "note": "Interest expense is zero or not found/parseable."}
        
        ratio = ebit / interest
        return {"value": round(ratio, 2)}
    except Exception as e:
        return {"error": f"Error calculating Interest Coverage Ratio: {str(e)}"}