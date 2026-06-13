import os
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
try:
    import nsepython
except ImportError:
    nsepython = None

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


def _trend_metrics(closes):
    """
    Given a pandas Series of daily closes (oldest -> newest), return multi-session
    trend metrics. Only includes windows for which enough data exists.
    Keys: chg_1d, chg_5d, chg_10d, chg_20d, down_n, sess_n, vs_sma_pct, sma_w.
    Returns {} if fewer than 2 closes.
    """
    closes = closes.dropna()
    n = len(closes)
    if n < 2:
        return {}
    price = float(closes.iloc[-1])

    def pct_back(back):
        if n > back:
            ref = float(closes.iloc[-1 - back])
            if ref:
                return (price - ref) / ref * 100.0
        return None

    m = {
        "chg_1d": pct_back(1),
        "chg_5d": pct_back(5),
        "chg_10d": pct_back(10),
        "chg_20d": pct_back(20),
    }

    # Down-session count over the last up-to-10 daily moves
    diffs = closes.diff().dropna()
    last = diffs.iloc[-10:]
    m["sess_n"] = int(len(last))
    m["down_n"] = int((last < 0).sum())

    # Position vs short SMA (use up to 20 sessions, need at least 5)
    w = min(20, n)
    if w >= 5:
        sma = float(closes.iloc[-w:].mean())
        if sma:
            m["vs_sma_pct"] = (price - sma) / sma * 100.0
            m["sma_w"] = w

    return m


def _format_trend(m):
    """Render trend metrics from _trend_metrics into a compact one-line string."""
    if not m:
        return "trend unavailable"
    parts = []
    for key, label in (("chg_1d", "1d"), ("chg_5d", "5d"), ("chg_10d", "10d"), ("chg_20d", "20d")):
        if m.get(key) is not None:
            parts.append(f"{label} {m[key]:+.1f}%")
    if m.get("sess_n"):
        parts.append(f"{m['down_n']}/{m['sess_n']} sessions down")
    if m.get("vs_sma_pct") is not None:
        pos = "above" if m["vs_sma_pct"] >= 0 else "below"
        parts.append(f"{abs(m['vs_sma_pct']):.1f}% {pos} {m['sma_w']}d-MA")
    s = " | ".join(parts)

    c10 = m.get("chg_10d")
    down = m.get("down_n", 0)
    up = m.get("sess_n", 0) - down
    if c10 is not None and c10 < 0 and down >= 7:
        s += "  [SUSTAINED DECLINE]"
    elif c10 is not None and c10 > 0 and up >= 7:
        s += "  [SUSTAINED RALLY]"
    return s


# Macro instrument universe grouped by category.
# Each entry: (display_name, yfinance_symbol_or_None, (tv_symbol, tv_exchange)_or_None).
# yfinance (Yahoo) is the primary source; tvDatafeed (TradingView) is the fallback for
# symbols Yahoo does not carry — mainly newer NSE sector indices (Healthcare, Oil & Gas,
# Defence, Commodities, Consumer Durables, Microcap, JP10Y). Those tv symbols resolve only
# when TradingView auth (TV_SESSION_TOKEN / TV_USERNAME) is configured; otherwise the line
# shows "Data temporarily unavailable" rather than breaking.
_MACRO_UNIVERSE = {
    "Indian Indices (Breadth)": [
        ("Nifty 50",                "^NSEI",                ("NIFTY", "NSE")),
        ("Nifty Bank",              "^NSEBANK",             ("BANKNIFTY", "NSE")),
        ("BSE Sensex",              "^BSESN",               ("SENSEX", "BSE")),
        ("Nifty 500",               "^CRSLDX",              ("CNX500", "NSE")),
        ("Nifty Next 50",           "^NSMIDCP",             ("NIFTYJR", "NSE")),
        ("Nifty Midcap 100",        "NIFTY_MIDCAP_100.NS",  ("CNXMIDCAP", "NSE")),
        ("Nifty Smallcap 100",      "^CRSMID",              ("CNXSMALLCAP", "NSE")),
        ("Nifty Microcap 250",      None,                   ("NIFTY_MICROCAP250", "NSE")),
    ],
    "Indian Sector Indices": [
        ("Nifty Auto",              "^CNXAUTO",             ("CNXAUTO", "NSE")),
        ("Nifty IT",                "^CNXIT",               ("CNXIT", "NSE")),
        ("Nifty Pharma",            "^CNXPHARMA",           ("CNXPHARMA", "NSE")),
        ("Nifty FMCG",              "^CNXFMCG",             ("CNXFMCG", "NSE")),
        ("Nifty Metal",             "^CNXMETAL",            ("CNXMETAL", "NSE")),
        ("Nifty Realty",            "^CNXREALTY",           ("CNXREALTY", "NSE")),
        ("Nifty Energy",            "^CNXENERGY",           ("CNXENERGY", "NSE")),
        ("Nifty Infrastructure",    "^CNXINFRA",            ("CNXINFRA", "NSE")),
        ("Nifty Media",             "^CNXMEDIA",            ("CNXMEDIA", "NSE")),
        ("Nifty PSU Bank",          "^CNXPSUBANK",          ("CNXPSUBANK", "NSE")),
        ("Nifty Private Bank",      "NIFTY_PVT_BANK.NS",    ("NIFTYPVTBANK", "NSE")),
        ("Nifty Financial Services","NIFTY_FIN_SERVICE.NS", ("CNXFINANCE", "NSE")),
        ("Nifty PSE",               "^CNXPSE",              ("CNXPSE", "NSE")),
        ("Nifty Services",          "^CNXSERVICE",          ("CNXSERVICE", "NSE")),
        ("Nifty India Consumption", "^CNXCONSUM",           ("CNXCONSUMPTION", "NSE")),
        ("Nifty Healthcare",        None,                   ("NIFTY_HEALTHCARE", "NSE")),
        ("Nifty Oil & Gas",         None,                   ("NIFTY_OIL_AND_GAS", "NSE")),
        ("Nifty India Defence",     None,                   ("NIFTY_IND_DEFENCE", "NSE")),
        ("Nifty Commodities",       None,                   ("CNXCOMMODITIES", "NSE")),
        ("Nifty Consumer Durables", None,                   ("NIFTY_CONSR_DURBL", "NSE")),
    ],
    "Global Equity Indices": [
        ("S&P 500",                 "^GSPC",                None),
        ("Nasdaq Composite",        "^IXIC",                None),
        ("Nasdaq 100",              "^NDX",                 None),
        ("Dow Jones",               "^DJI",                 None),
        ("FTSE 100",                "^FTSE",                None),
        ("DAX 40",                  "^GDAXI",               None),
        ("CAC 40",                  "^FCHI",                None),
        ("Euro Stoxx 50",           "^STOXX50E",            None),
        ("Nikkei 225",              "^N225",                None),
        ("Hang Seng",               "^HSI",                 None),
        ("Shanghai Composite",      "000001.SS",            None),
        ("KOSPI",                   "^KS11",                None),
    ],
    "Commodities (Global)": [
        ("Gold (COMEX)",            "GC=F",                 None),
        ("Silver (COMEX)",          "SI=F",                 None),
        ("Crude Oil (WTI)",         "CL=F",                 None),
        ("Brent Crude",             "BZ=F",                 None),
        ("Copper",                  "HG=F",                 None),
    ],
    "Currencies": [
        ("USD/INR",                 "INR=X",                ("USDINR", "FX_IDC")),
        ("US Dollar Index (DXY)",   "DX-Y.NYB",             ("DXY", "TVC")),
        ("USD/JPY",                 "JPY=X",                ("USDJPY", "FX_IDC")),
    ],
    "Volatility": [
        ("India VIX",               "^INDIAVIX",            ("INDIAVIX", "NSE")),
        ("CBOE VIX",                "^VIX",                 None),
        ("Gold VIX (GVZ)",          "^GVZ",                 None),
        ("Crude VIX (OVX)",         "^OVX",                 None),
        ("Dow VIX (VXD)",           "^VXD",                 None),
    ],
    "Crypto": [
        ("Bitcoin (BTC)",           "BTC-USD",              None),
        ("Ethereum (ETH)",          "ETH-USD",              None),
    ],
    "Bond Yields": [
        ("US 10Y Yield",            "^TNX",                 ("US10Y", "TVC")),
        ("Japan 10Y Yield",         None,                   ("JP10Y", "TVC")),
    ],
}


# Indian indices Yahoo does not carry — fetched directly from NSE / niftyindices via
# nsepython. Keys = display names in _MACRO_UNIVERSE; values = exact niftyindices index
# names (verified against nsepython.nse_get_index_list()).
_NSE_DIRECT_NAMES = {
    "Nifty Healthcare":        "NIFTY HEALTHCARE",
    "Nifty Oil & Gas":         "NIFTY OIL AND GAS",
    "Nifty India Defence":     "NIFTY IND DEFENCE",
    "Nifty Commodities":       "NIFTY COMMODITIES",
    "Nifty Consumer Durables": "NIFTY CONSR DURBL",
    "Nifty Microcap 250":      "NIFTY MICROCAP250",
}


def _fetch_nse_index(nse_name):
    """
    Fetch ~1 month of daily closes for an NSE index directly from niftyindices via
    nsepython (for Indian indices Yahoo doesn't carry). niftyindices returns rows
    newest-first, so sort ascending before computing trend.
    Returns (price, change_pct, metrics) or None.
    """
    if nsepython is None:
        return None
    try:
        end = datetime.date.today()
        start = end - datetime.timedelta(days=45)
        df = nsepython.index_history(
            nse_name, start.strftime("%d-%b-%Y"), end.strftime("%d-%b-%Y")
        )
        if df is None or len(df) == 0 or "CLOSE" not in df.columns or "HistoricalDate" not in df.columns:
            return None
        df = df.copy()
        df["_d"] = pd.to_datetime(df["HistoricalDate"], format="%d %b %Y", errors="coerce")
        df = df.dropna(subset=["_d"]).sort_values("_d")
        closes = pd.to_numeric(df["CLOSE"], errors="coerce").dropna()
        if len(closes) >= 2:
            m = _trend_metrics(closes)
            return float(closes.iloc[-1]), m.get("chg_1d"), m
    except Exception:
        pass
    return None


def _make_tv():
    """
    Build a TvDatafeed session using configured credentials, else anonymous.
    Returns the session, or None if tvDatafeed is unavailable / construction fails.
    """
    if not TvDatafeed:
        return None
    try:
        tok = os.environ.get('TV_SESSION_TOKEN', '').strip()
        usr = os.environ.get('TV_USERNAME', '')
        pwd = os.environ.get('TV_PASSWORD', '')
        if tok:
            return TvDatafeed(token=tok)
        if usr and pwd:
            return TvDatafeed(username=usr, password=pwd)
        return TvDatafeed()
    except Exception:
        return None


def _fetch_quote(name, yf_symbol, tv_tuple, tv_holder):
    """
    Fetch ~1 month of daily closes for one instrument. Source order:
      1) yfinance (Yahoo) — primary for everything it carries
      2) nsepython (NSE / niftyindices) — for Indian indices Yahoo lacks (_NSE_DIRECT_NAMES)
      3) tvDatafeed (TradingView) — last-resort; the anonymous socket drops mid-run, so the
         tv path retries once with a FRESH connection (reused via tv_holder).
    Returns (price, change_pct, metrics); (None, None, {}) if all sources fail.
    """
    if yf_symbol:
        try:
            closes = yf.Ticker(yf_symbol).history(period="1mo")['Close'].dropna()
            if len(closes) >= 2:
                m = _trend_metrics(closes)
                return float(closes.iloc[-1]), m.get("chg_1d"), m
        except Exception:
            pass

    nse_name = _NSE_DIRECT_NAMES.get(name)
    if nse_name:
        res = _fetch_nse_index(nse_name)
        if res is not None:
            return res

    if tv_tuple:
        tv_sym, tv_exc = tv_tuple
        for attempt in range(2):
            if attempt == 1:  # shared connection failed — reconnect fresh and retry once
                tv_holder[0] = _make_tv()
            tv = tv_holder[0]
            if tv is None:
                continue
            try:
                data = tv.get_hist(symbol=tv_sym, exchange=tv_exc, interval=Interval.in_daily, n_bars=22)
                if data is not None and not data.empty:
                    closes = data['close'].dropna()
                    if len(closes) >= 2:
                        m = _trend_metrics(closes)
                        return float(closes.iloc[-1]), m.get("chg_1d"), m
            except Exception:
                pass
    return None, None, {}


# Flat lookup: index display name -> (yf_symbol, tv_tuple), built from _MACRO_UNIVERSE.
_INDEX_BY_NAME = {
    name: (yf_symbol, tv_tuple)
    for _instruments in _MACRO_UNIVERSE.values()
    for (name, yf_symbol, tv_tuple) in _instruments
}


def get_index_trend(name):
    """
    Fetch the multi-session price tape for a single instrument in _MACRO_UNIVERSE by its
    display name (e.g. "Nifty IT", "Nifty Bank", "Nifty 500"). Reuses the same
    yfinance -> nsepython -> tvDatafeed source chain as the macro feed (get_live_market_data).
    Returns "<name>: <price> | <trend>" (with any [SUSTAINED ...] tag) or None if the index
    can't be resolved / fetched.
    """
    entry = _INDEX_BY_NAME.get(name)
    if entry is None:
        return None
    yf_symbol, tv_tuple = entry
    price, _change, metrics = _fetch_quote(name, yf_symbol, tv_tuple, [None])
    if price is None:
        return None
    return f"{name}: {price:,.2f} | {_format_trend(metrics)}"


def get_live_market_data():
    """
    Fetches exact, live prices for critical macro indicators.
    Sources:
      - yfinance (primary) + tvDatafeed (fallback) for global indices/FX/bonds
      - Trading Economics for India 10Y yield
      - MCX India website for MCX commodity prices (Gold, Silver, Crude, Copper)
    """
    import os
    from dotenv import load_dotenv
    load_dotenv()

    tv = _make_tv()
    if tv is not None:
        try:
            probe = tv.get_hist(symbol="NIFTY", exchange="NSE", interval=Interval.in_daily, n_bars=2)
            if probe is None or probe.empty:
                tv = None
        except Exception:
            tv = None
    tv_holder = [tv]  # mutable so _fetch_quote can swap in a fresh connection on drop

    results = []
    results.append("## LIVE MARKET PRICES (Deterministic Ground Truth)\n")
    results.append(
        "Each line: latest level, then the multi-session trend — 1d/5d/10d/20d % change, "
        "count of down sessions in the last 10, and position vs the ~20-day moving average. "
        "A [SUSTAINED DECLINE] / [SUSTAINED RALLY] tag marks a sustained multi-session move. "
        "Per Stage 7.5, reconcile EVERY directional call (sector, index, metal/commodity, "
        "currency, bond yield, volatility, crypto) against this tape.\n"
    )

    global_prices = {}
    usd_inr = 84.0

    for category, instruments in _MACRO_UNIVERSE.items():
        results.append(f"\n### {category}")
        for name, yf_symbol, tv_tuple in instruments:
            price, change_pct, metrics = _fetch_quote(name, yf_symbol, tv_tuple, tv_holder)
            if price is None:
                results.append(f"- **{name}**: Data temporarily unavailable")
                continue
            global_prices[name] = {"price": price, "change": change_pct, "metrics": metrics}
            if name == "USD/INR":
                usd_inr = price
            if "Yield" in name:
                results.append(f"- **{name}**: {price:.3f}% | {_format_trend(metrics)}")
            else:
                results.append(f"- **{name}**: {price:,.2f} | {_format_trend(metrics)}")

    # India 10Y Yield — Trading Economics
    india_10y = _fetch_india_10y_yield_trading_economics()
    if india_10y is not None:
        results.append(f"- **India 10Y Yield**: {india_10y:.3f}% (Trading Economics)")
    else:
        india_10y_tv = None
        tv = tv_holder[0]
        if tv:
            try:
                data = tv.get_hist(symbol="IN10Y", exchange="TVC", interval=Interval.in_daily, n_bars=22)
                if data is not None and not data.empty:
                    closes = data['close'].dropna()
                    india_10y_tv = closes.iloc[-1]
                    results.append(f"- **India 10Y Yield**: {india_10y_tv:.3f}% | {_format_trend(_trend_metrics(closes))}")
            except Exception:
                pass
        if india_10y_tv is None:
            results.append("- **India 10Y Yield**: Data temporarily unavailable")

    # MCX India prices — scraped directly from mcxindia.com
    results.append("\n### Indian Commodities (MCX)")
    mcx_prices = _fetch_mcx_prices()

    # Math fallback config (only used if MCX scrape fails for a symbol)
    mcx_math_fallback = {
        "Gold (MCX)":      {"global_key": "Gold (COMEX)",    "unit": "per 10g",  "math": lambda g, r: (g / 31.103) * 10 * r * 1.06},
        "Silver (MCX)":    {"global_key": "Silver (COMEX)",  "unit": "per kg",   "math": lambda s, r: (s / 31.103) * 1000 * r * 1.06},
        "Crude Oil (MCX)": {"global_key": "Crude Oil (WTI)", "unit": "per bbl",  "math": lambda c, r: c * r},
        "Copper (MCX)":    {"global_key": "Copper",          "unit": "per kg",   "math": lambda cop, r: cop * 2.20462 * r * 1.05},
    }

    for mcx_name in mcx_math_fallback:
        conf = mcx_math_fallback[mcx_name]
        # MCX scrape is a snapshot with no history; attach the global instrument's
        # multi-session trend as a directional proxy so the model isn't trend-blind.
        g_data = global_prices.get(conf["global_key"])
        proxy = ""
        if g_data and g_data.get("metrics"):
            proxy = f" | global trend: {_format_trend(g_data['metrics'])}"

        if mcx_name in mcx_prices:
            d = mcx_prices[mcx_name]
            results.append(f"- **{mcx_name}**: Rs. {d['price']:,.2f} {d['unit']} (MCX, expiry {d['expiry']}){proxy}")
        elif g_data:
            # Fallback to math calculation from the global price
            mcx_price = conf["math"](g_data["price"], usd_inr)
            results.append(f"- **{mcx_name}**: Rs. {mcx_price:,.2f} {conf['unit']} (calc from global){proxy}")
        else:
            results.append(f"- **{mcx_name}**: Data temporarily unavailable")

    return "\n".join(results)


if __name__ == "__main__":
    print(get_live_market_data())
