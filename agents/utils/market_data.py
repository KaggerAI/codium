import sys
import traceback
import datetime
import yfinance as yf
try:
    from tvDatafeed import TvDatafeed, Interval
except ImportError:
    TvDatafeed = None

def get_live_market_data():
    """
    Fetches exact, live prices for critical macro indicators using yfinance (primary)
    and tvDatafeed (fallback).
    """
    tickers = {
        # Global Indices
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
        # Indian Indices
        "Nifty 50": "^NSEI",
        "BSE Sensex": "^BSESN",
        # Commodities (Global)
        "Gold (COMEX)": "GC=F",
        "Silver (COMEX)": "SI=F",
        "Crude Oil (WTI)": "CL=F",
        "Copper": "HG=F",
        # FX and Bonds
        "USD/INR": "INR=X",
        "US 10Y Yield": "^TNX",
        "India 10Y Yield": "^IN10YT=RR"
    }

    import os
    from dotenv import load_dotenv
    load_dotenv()
    
    tv = None
    if TvDatafeed:
        try:
            _tv_user = os.environ.get('TV_USERNAME', '')
            _tv_pass = os.environ.get('TV_PASSWORD', '')
            if _tv_user and _tv_pass:
                tv = TvDatafeed(username=_tv_user, password=_tv_pass)
            else:
                tv = TvDatafeed()
        except Exception:
            tv = None

    tv_fallbacks = {
        "Nifty 50": ("NIFTY", "NSE"),
        "BSE Sensex": ("SENSEX", "BSE"),
        "USD/INR": ("USDINR", "FX_IDC"),
        "US 10Y Yield": ("US10Y", "TVC"),
        "India 10Y Yield": ("IN10Y", "TVC")
    }

    results = []
    results.append("## LIVE MARKET PRICES (Deterministic Ground Truth)\n")

    # First pass: gather global prices to use for MCX conversion
    global_prices = {}
    usd_inr = 84.0 # default fallback
    
    for name, symbol in tickers.items():
        price = None
        change_pct = None
        
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="5d")
            if not hist.empty and len(hist) >= 2:
                closes = hist['Close'].dropna()
                if len(closes) >= 2:
                    price = closes.iloc[-1]
                    prev_close = closes.iloc[-2]
                    change_pct = ((price - prev_close) / prev_close) * 100
        except Exception:
            pass

        if price is None and tv and name in tv_fallbacks:
            try:
                tv_sym, tv_exc = tv_fallbacks[name]
                data = tv.get_hist(symbol=tv_sym, exchange=tv_exc, interval=Interval.in_daily, n_bars=3)
                if data is not None and not data.empty:
                    price = data['close'].iloc[-1]
                    prev_close = data['close'].iloc[-2]
                    change_pct = ((price - prev_close) / prev_close) * 100
            except Exception:
                pass

        if price is not None:
            global_prices[name] = {"price": price, "change": change_pct}
            if name == "USD/INR":
                usd_inr = price
                
            if "Yield" in name:
                results.append(f"- **{name}**: {price:.3f}% ({change_pct:+.2f}% daily change)")
            elif "USD/INR" in name:
                results.append(f"- **{name}**: {price:.2f} ({change_pct:+.2f}%)")
            elif "MCX" not in name:
                results.append(f"- **{name}**: {price:,.2f} ({change_pct:+.2f}%)")
        else:
             if "MCX" not in name:
                 results.append(f"- **{name}**: Data temporarily unavailable")
                 
    # Second pass: Calculate MCX India equivalents based on Global Prices and USD/INR
    # 1 Troy Ounce = 31.103 grams
    mcx_commodities = {
        "Gold (MCX)": {"sym": "GOLD1!", "exc": "MCX", "global_key": "Gold (COMEX)", "unit": "per 10g", "math": lambda g, r: (g / 31.103) * 10 * r * 1.06},
        "Silver (MCX)": {"sym": "SILVER1!", "exc": "MCX", "global_key": "Silver (COMEX)", "unit": "per 1kg", "math": lambda s, r: (s / 31.103) * 1000 * r * 1.06},
        "Crude Oil (MCX)": {"sym": "CRUDEOIL1!", "exc": "MCX", "global_key": "Crude Oil (WTI)", "unit": "per bbl", "math": lambda c, r: c * r},
        "Copper (MCX)": {"sym": "COPPER1!", "exc": "MCX", "global_key": "Copper", "unit": "per kg", "math": lambda cop, r: cop * 2.20462 * r * 1.05}
    }
    
    for mcx_name, conf in mcx_commodities.items():
        mcx_price = None
        mcx_change = None
        
        # 1. Primary: Try tvDatafeed
        if tv:
            try:
                data = tv.get_hist(symbol=conf["sym"], exchange=conf["exc"], interval=Interval.in_daily, n_bars=3)
                if data is not None and not data.empty:
                    mcx_price = data['close'].iloc[-1]
                    prev_mcx = data['close'].iloc[-2]
                    mcx_change = ((mcx_price - prev_mcx) / prev_mcx) * 100
            except Exception:
                pass
                
        # 2. Fallback: Math Calculation
        if mcx_price is None:
            if conf["global_key"] in global_prices:
                g_data = global_prices[conf["global_key"]]
                mcx_price = conf["math"](g_data["price"], usd_inr)
                mcx_change = g_data["change"] # use global change pct as proxy
        
        if mcx_price is not None:
            results.append(f"- **{mcx_name}**: Rs. {mcx_price:,.2f} {conf['unit']} (approx {mcx_change:+.2f}%)")
        else:
            results.append(f"- **{mcx_name}**: Data temporarily unavailable")
            
    return "\n".join(results)

if __name__ == "__main__":
    print(get_live_market_data())
