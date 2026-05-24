import sys
import re
import json
import traceback
import datetime
import pandas as pd
import requests
import yfinance as yf
try:
    from tvDatafeed import TvDatafeed, Interval
except ImportError:
    TvDatafeed = None

_MCX_URL = "https://www.mcxindia.com/market-data/most-active-contracts"
_MCX_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# MCX LTP units: GOLD=per 10g, SILVER=per kg, CRUDEOIL=per bbl, COPPER=per kg
_MCX_SYMBOL_MAP = {
    "Gold (MCX)":      ("GOLD",     "per 10g"),
    "Silver (MCX)":    ("SILVER",   "per kg"),
    "Crude Oil (MCX)": ("CRUDEOIL", "per bbl"),
    "Copper (MCX)":    ("COPPER",   "per kg"),
}


def _fetch_mcx_prices():
    """
    Scrape live MCX LTPs from mcxindia.com/market-data/most-active-contracts.
    Returns dict: {display_name: {"price": float, "unit": str, "expiry": str}}
    The page embeds two JSON arrays (by-value and by-volume) in the HTML.
    We use the first (by-value = highest liquidity) and pick the nearest expiry
    FUTCOM contract for each target symbol.
    """
    try:
        r = requests.get(_MCX_URL, headers=_MCX_HEADERS, timeout=15)
        r.raise_for_status()
        # Two var x = [...] blocks are embedded; block 0 = most active by value
        json_blocks = re.findall(r'var\s+\w+\s*=\s*(\[.*?\]);', r.text, re.DOTALL)
        if not json_blocks:
            return {}
        records = json.loads(json_blocks[0])
    except Exception as e:
        print(f"MCX_SCRAPE: fetch/parse error: {e}", file=sys.stderr)
        return {}

    # Build a lookup: EngSymbol -> first FUTCOM record (already sorted by value desc)
    seen = {}
    for rec in records:
        sym = rec.get("EngSymbol", "")
        if rec.get("InstrumentName") == "FUTCOM" and sym not in seen:
            seen[sym] = rec

    result = {}
    for display_name, (mcx_sym, unit) in _MCX_SYMBOL_MAP.items():
        rec = seen.get(mcx_sym)
        if rec and rec.get("LTP"):
            result[display_name] = {
                "price":  float(rec["LTP"]),
                "unit":   unit,
                "expiry": rec.get("ExpiryDate", ""),
            }
    return result


def _fetch_india_10y_yield_trading_economics():
    """Scrape India 10Y government bond yield from Trading Economics."""
    try:
        tables = pd.read_html("https://tradingeconomics.com/india/government-bond-yield")
        row = tables[0][tables[0]['Bonds'] == 'India 10Y'].iloc[0]
        return float(row['Yield'])
    except Exception:
        return None


def get_live_market_data():
    """
    Fetches exact, live prices for critical macro indicators.
    Sources:
      - yfinance (primary) + tvDatafeed (fallback) for global indices/FX/bonds
      - Trading Economics for India 10Y yield
      - MCX India website for MCX commodity prices (Gold, Silver, Crude, Copper)
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
    }

    import os
    from dotenv import load_dotenv
    load_dotenv()

    tv = None
    if TvDatafeed:
        try:
            _tv_token = os.environ.get('TV_SESSION_TOKEN', '').strip()
            _tv_user = os.environ.get('TV_USERNAME', '')
            _tv_pass = os.environ.get('TV_PASSWORD', '')
            if _tv_token:
                tv = TvDatafeed(token=_tv_token)
            elif _tv_user and _tv_pass:
                tv = TvDatafeed(username=_tv_user, password=_tv_pass)
            else:
                tv = TvDatafeed()
            probe = tv.get_hist(symbol="NIFTY", exchange="NSE", interval=Interval.in_daily, n_bars=2)
            if probe is None or probe.empty:
                tv = None
        except Exception:
            tv = None

    tv_fallbacks = {
        "Nifty 50": ("NIFTY", "NSE"),
        "BSE Sensex": ("SENSEX", "BSE"),
        "USD/INR": ("USDINR", "FX_IDC"),
        "US 10Y Yield": ("US10Y", "TVC"),
    }

    results = []
    results.append("## LIVE MARKET PRICES (Deterministic Ground Truth)\n")

    global_prices = {}
    usd_inr = 84.0

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
            else:
                results.append(f"- **{name}**: {price:,.2f} ({change_pct:+.2f}%)")
        else:
            results.append(f"- **{name}**: Data temporarily unavailable")

    # India 10Y Yield — Trading Economics
    india_10y = _fetch_india_10y_yield_trading_economics()
    if india_10y is not None:
        results.append(f"- **India 10Y Yield**: {india_10y:.3f}% (Trading Economics)")
    else:
        india_10y_tv = None
        if tv:
            try:
                data = tv.get_hist(symbol="IN10Y", exchange="TVC", interval=Interval.in_daily, n_bars=3)
                if data is not None and not data.empty:
                    india_10y_tv = data['close'].iloc[-1]
                    prev = data['close'].iloc[-2]
                    chg = ((india_10y_tv - prev) / prev) * 100
                    results.append(f"- **India 10Y Yield**: {india_10y_tv:.3f}% ({chg:+.2f}% daily change)")
            except Exception:
                pass
        if india_10y_tv is None:
            results.append("- **India 10Y Yield**: Data temporarily unavailable")

    # MCX India prices — scraped directly from mcxindia.com
    mcx_prices = _fetch_mcx_prices()

    # Math fallback config (only used if MCX scrape fails for a symbol)
    mcx_math_fallback = {
        "Gold (MCX)":      {"global_key": "Gold (COMEX)",    "unit": "per 10g",  "math": lambda g, r: (g / 31.103) * 10 * r * 1.06},
        "Silver (MCX)":    {"global_key": "Silver (COMEX)",  "unit": "per kg",   "math": lambda s, r: (s / 31.103) * 1000 * r * 1.06},
        "Crude Oil (MCX)": {"global_key": "Crude Oil (WTI)", "unit": "per bbl",  "math": lambda c, r: c * r},
        "Copper (MCX)":    {"global_key": "Copper",          "unit": "per kg",   "math": lambda cop, r: cop * 2.20462 * r * 1.05},
    }

    for mcx_name in mcx_math_fallback:
        if mcx_name in mcx_prices:
            d = mcx_prices[mcx_name]
            results.append(f"- **{mcx_name}**: Rs. {d['price']:,.2f} {d['unit']} (MCX, expiry {d['expiry']})")
        else:
            # Fallback to math calculation
            conf = mcx_math_fallback[mcx_name]
            if conf["global_key"] in global_prices:
                g_data = global_prices[conf["global_key"]]
                mcx_price = conf["math"](g_data["price"], usd_inr)
                results.append(f"- **{mcx_name}**: Rs. {mcx_price:,.2f} {conf['unit']} (calc approx {g_data['change']:+.2f}%)")
            else:
                results.append(f"- **{mcx_name}**: Data temporarily unavailable")

    return "\n".join(results)


if __name__ == "__main__":
    print(get_live_market_data())
