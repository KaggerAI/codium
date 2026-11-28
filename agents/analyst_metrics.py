"""
analyst_metrics.py - Deterministic numbers for the Analyst Report Agent.

Everything an investor compares across brokers - upside from today's price, forward
P/E and P/B, EPS CAGR, PEG, the multiple implied by each target, consensus target
statistics, EPS dispersion by year, target-price momentum - is computed HERE from the
extracted figures, never by the LLM. That keeps the numbers consistent across brokers
and auditable (each one traces back to a table in a report).
"""

import re
import sys
import math
import datetime
import statistics

STALE_DAYS = 90          # flagged in the UI
CONSENSUS_MAX_AGE = 180  # older calls are shown but excluded from consensus stats


def _log(*parts):
    """stderr log that cannot raise on a non-ASCII Windows console (cp1252)."""
    try:
        print(*parts, file=sys.stderr)
    except UnicodeEncodeError:
        print(*(str(p).encode("ascii", "replace").decode() for p in parts), file=sys.stderr)


# =====================================================================
# Fiscal-year helpers (Indian April-March fiscal years)
# =====================================================================

_FY_RE = re.compile(r"(FY|CY)?\s*'?(\d{2,4})\s*([AEPF])?", re.IGNORECASE)


def parse_fy(label: str):
    """'FY27E' -> ('FY', 2027, False); 'FY2026A' -> ('FY', 2026, True). None if unparseable."""
    if not label:
        return None
    m = _FY_RE.search(str(label).strip())
    if not m:
        return None
    kind = (m.group(1) or "FY").upper()
    yr = int(m.group(2))
    if yr < 100:
        yr += 2000
    if not 2000 <= yr <= 2100:
        return None
    suffix = (m.group(3) or "").upper()
    # Rolling bases such as "Sep 2028" / "Sept-28E": September 2028 falls in FY29
    # (Apr 2028 - Mar 2029), so a month from April onwards belongs to the next fiscal year.
    if not m.group(1):
        mon = _MONTH_RE.search(str(label))
        if mon:
            month = _MONTHS.index(mon.group(1)[:3].lower()) + 1
            return "FY", yr + 1 if month >= 4 else yr, suffix == "A"
    return kind, yr, suffix == "A"


_METHOD_ALIASES = {
    "PE": "P/E", "P/E": "P/E", "PER": "P/E",
    "PB": "P/B", "P/B": "P/B", "P/BV": "P/B", "PBV": "P/B",
    "PABV": "P/ABV", "P/ABV": "P/ABV",
    "EV/EBITDA": "EV/EBITDA", "EVEBITDA": "EV/EBITDA",
    "EV/SALES": "EV/Sales", "DCF": "DCF", "SOTP": "SOTP",
}


def normalize_method(method: str) -> str:
    key = re.sub(r"\s+", "", str(method or "")).upper()
    return _METHOD_ALIASES.get(key, method)


def valuation_basis(val: dict) -> str | None:
    """'book' | 'earnings' | None - what the broker's multiple is applied to."""
    method = normalize_method(val.get("method") or "")
    metric = re.sub(r"\s+", "", str(val.get("metric") or "")).lower()
    if method in ("P/B", "P/ABV") or any(k in metric for k in ("abv", "bv", "book", "p/b", "networth")):
        return "book"
    if method == "P/E" or any(k in metric for k in ("eps", "earning", "p/e", "pat")):
        return "earnings"
    return None


_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?", re.IGNORECASE)


def fy_label(year: int) -> str:
    return f"FY{year % 100:02d}"


def current_fy(today: datetime.date | None = None) -> int:
    """Fiscal year in progress (Nov 2026 -> 2027, i.e. FY27 = Apr 2026 - Mar 2027)."""
    today = today or datetime.date.today()
    return today.year + 1 if today.month >= 4 else today.year


def _f(x):
    try:
        if x is None:
            return None
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def _quantile(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def _r(x, n=1):
    return None if x is None else round(x, n)


# =====================================================================
# Market data (price, price history, Yahoo/LSEG consensus)
# =====================================================================

def fetch_market_data(ticker: str) -> dict:
    """CMP, 1y weekly closes and the Yahoo Finance (LSEG-sourced) consensus block."""
    out = {"cmp": None, "yf_symbol": None, "price_history": [], "consensus": None}
    try:
        import yfinance as yf
    except ImportError:
        return out

    symbols = [ticker] if ticker.endswith((".NS", ".BO")) else [f"{ticker}.NS", f"{ticker}.BO"]
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="1y", interval="1wk")
            if hist is None or hist.empty:
                continue
            out["yf_symbol"] = sym
            # Yahoo's newest bar is often a partial one with a NaN close (seen on Azure), so take
            # the latest *finite* close; NaN would also serialise as invalid JSON for the browser.
            last = t.history(period="5d")
            closes = [_f(c) for c in (last if not last.empty else hist)["Close"].tolist()]
            closes = [c for c in closes if c] or [_f(c) for c in hist["Close"].tolist() if _f(c)]
            out["cmp"] = closes[-1] if closes else None
            out["price_history"] = [
                {"date": idx.strftime("%Y-%m-%d"), "close": round(float(c), 2)}
                for idx, c in hist["Close"].items() if c == c
            ]
            out["consensus"] = _yf_consensus(t)
            out["current"] = _current_standing(t, out["cmp"])
            break
        except Exception as e:
            _log(f"ANALYST_METRICS: yfinance failed for {sym}: {e}")

    # Current price stays from yfinance (live). P/E, trailing EPS and the latest quarter's YoY
    # growth come from Screener, whose quarterly table is the reported Indian numbers (Yahoo's
    # statement history lags for some companies and its bank "revenue" is not comparable).
    # yfinance's figures remain only as the fallback when Screener is unavailable.
    scr = _screener_standing(ticker.replace(".NS", "").replace(".BO", ""))
    if scr:
        cur = out.get("current") or {}
        cur.update({k: v for k, v in scr.items() if v is not None or k == "ebitda_yoy"})
        cur["cmp"] = _r(out["cmp"], 2) if out["cmp"] else None
        out["current"] = cur
    elif out.get("current"):
        out["current"]["source"] = "Yahoo Finance (Screener unavailable)"
    return out


def _num(s) -> float | None:
    try:
        v = float(str(s).replace(",", "").replace("%", "").strip())
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _screener_standing(ticker: str) -> dict | None:
    """
    P/E (Screener's 'Stock P/E'), trailing EPS (sum of the last four quarters' 'EPS in Rs')
    and latest-quarter YoY growth in Sales / Operating Profit (EBITDA) / Net Profit, from the
    Screener quarterly results table (consolidated when available). None if unavailable.
    """
    try:
        import asyncio
        from fetchers.screener_fetcher import fetch_consolidated_async
        tables, _desc, ratios, consolidated = asyncio.run(fetch_consolidated_async(ticker))
    except Exception as e:
        _log(f"ANALYST_METRICS: Screener fetch failed for {ticker}: {str(e)[:160]}")
        return None
    q = (tables or {}).get("Quarterly Results")
    if q is None or q.empty or len(q.columns) < 2:
        return None

    label_col = q.columns[0]
    rows = {}
    for _, row in q.iterrows():
        key = re.sub(r"[^a-z %/]", "", str(row[label_col]).lower()).strip()
        if key and not key.startswith("childrow") and key not in rows:
            rows[key] = row

    periods = [c for c in q.columns[1:] if re.match(r"^[A-Z][a-z]{2} \d{4}$", str(c))]
    if not periods:
        return None
    latest = periods[-1]
    mon, yr = latest.split()
    year_ago = f"{mon} {int(yr) - 1}"

    def val(name, col):
        row = rows.get(name)
        return _num(row[col]) if row is not None and col in row.index else None

    def yoy(name):
        a, b = val(name, latest), (val(name, year_ago) if year_ago in periods else None)
        return _r((a / b - 1) * 100, 1) if a is not None and b and b > 0 else None

    is_financial = "financing profit" in rows
    revenue_key = "sales" if "sales" in rows else "revenue"
    eps_vals = [val("eps in rs", c) for c in periods[-4:]]
    d = datetime.datetime.strptime(latest, "%b %Y").date()
    return {
        "source": "Screener" + (" (consolidated)" if consolidated else " (standalone)"),
        "pe_ttm": _r(_num(ratios.get("Stock P/E")), 1) if ratios else None,
        "eps_ttm": _r(sum(eps_vals), 2) if len(eps_vals) == 4 and all(v is not None for v in eps_vals) else None,
        "quarter": _quarter_label(d),
        "quarter_end": d.isoformat(),
        "revenue_yoy": yoy(revenue_key),
        "ebitda_yoy": None if is_financial else yoy("operating profit"),
        "pat_yoy": yoy("net profit"),
        "is_financial": is_financial,
    }


def _quarter_label(d: datetime.date) -> str:
    """Indian fiscal quarter: Sep 2026 -> 'Q2 FY27'."""
    fy = d.year + 1 if d.month >= 4 else d.year
    q = {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}[d.month]
    return f"Q{q} {fy_label(fy)}"


def _current_standing(t, cmp: float | None) -> dict:
    """Where the company stands today: trailing P/E and EPS, and the latest quarter's YoY
    growth in revenue, EBITDA and net profit - the yardstick for the brokers' projections."""
    cur = {"cmp": _r(cmp, 2)}
    info = {}
    try:
        info = t.info or {}
    except Exception:
        pass
    eps_ttm = _f(info.get("trailingEps"))
    cur["eps_ttm"] = _r(eps_ttm, 2)
    pe = _f(info.get("trailingPE"))
    if cmp and eps_ttm and eps_ttm > 0:
        pe = cmp / eps_ttm                 # re-base on today's price
    cur["pe_ttm"] = _r(pe, 1) if pe and pe > 0 else None

    def yoy(q, names):
        for n in names:
            if n in q.index:
                row = q.loc[n]
                latest = row.index[0]
                year_ago = [c for c in row.index if abs((latest - c).days - 365) <= 20]
                a = _f(row[latest])
                b = _f(row[year_ago[0]]) if year_ago else None
                if a is not None and b and b > 0:
                    return _r((a / b - 1) * 100, 1)
        return None

    # Banks / NBFCs / insurers: Yahoo's "revenue" mixes interest income and other items (HDFC
    # Bank showed +81%), and EBITDA does not apply - show profit growth only.
    cur["is_financial"] = str(info.get("sector") or "").lower().startswith("financial")
    try:
        q = t.quarterly_income_stmt
        if q is not None and not q.empty:
            latest = q.columns[0]
            # Yahoo's statement history can lag by a year for some companies: only trust it
            # when its latest column is a recent quarter.
            if (datetime.date.today() - latest.date()).days <= 200:
                cur["quarter"] = _quarter_label(latest.date())
                cur["quarter_end"] = latest.date().isoformat()
                cur["revenue_yoy"] = yoy(q, ("Total Revenue", "Operating Revenue"))
                cur["ebitda_yoy"] = yoy(q, ("EBITDA", "Normalized EBITDA"))
                cur["pat_yoy"] = yoy(q, ("Net Income", "Net Income Common Stockholders"))
    except Exception as e:
        _log(f"ANALYST_METRICS: quarterly statement unavailable: {e}")
    # Yahoo's own quarterly YoY fields cover gaps in the statement history.
    if cur.get("revenue_yoy") is None and _f(info.get("revenueGrowth")) is not None:
        cur["revenue_yoy"] = _r(_f(info["revenueGrowth"]) * 100, 1)
    if cur.get("pat_yoy") is None and _f(info.get("earningsQuarterlyGrowth")) is not None:
        cur["pat_yoy"] = _r(_f(info["earningsQuarterlyGrowth"]) * 100, 1)
    if not cur.get("quarter") and info.get("mostRecentQuarter"):
        try:
            d = datetime.datetime.fromtimestamp(int(info["mostRecentQuarter"]), datetime.timezone.utc).date()
            cur["quarter"], cur["quarter_end"] = _quarter_label(d), d.isoformat()
        except Exception:
            pass
    if cur["is_financial"]:
        cur["revenue_yoy"] = cur["ebitda_yoy"] = None
    return cur


def _yf_consensus(t) -> dict | None:
    cfy = current_fy()
    period_fy = {"0y": cfy, "+1y": cfy + 1}
    res = {"source": "Yahoo Finance (LSEG consensus)", "eps": [], "revenue": [],
           "price_target": None, "eps_revision_90d_pct": None, "eps_revision_30d_pct": None,
           "recommendations": None}
    got = False
    try:
        pt = t.analyst_price_targets
        if pt and pt.get("mean"):
            res["price_target"] = {k: _r(_f(pt.get(k)), 0) for k in ("mean", "median", "high", "low")}
            got = True
    except Exception:
        pass
    try:
        ee = t.earnings_estimate
        if ee is not None and not ee.empty:
            for p, fy in period_fy.items():
                if p in ee.index:
                    row = ee.loc[p]
                    res["eps"].append({
                        "fy": fy_label(fy), "mean": _r(_f(row.get("avg")), 2),
                        "low": _r(_f(row.get("low")), 2), "high": _r(_f(row.get("high")), 2),
                        "n": int(row.get("numberOfAnalysts") or 0),
                        "growth_pct": _r((_f(row.get("growth")) or 0) * 100, 1) if _f(row.get("growth")) is not None else None,
                    })
                    got = True
    except Exception:
        pass
    try:
        re_ = t.revenue_estimate
        if re_ is not None and not re_.empty:
            for p, fy in period_fy.items():
                if p in re_.index:
                    row = re_.loc[p]
                    avg = _f(row.get("avg"))
                    res["revenue"].append({
                        "fy": fy_label(fy), "mean_cr": _r(avg / 1e7, 0) if avg else None,
                        "n": int(row.get("numberOfAnalysts") or 0),
                    })
    except Exception:
        pass
    try:
        tr = t.eps_trend
        if tr is not None and not tr.empty and "+1y" in tr.index:
            row = tr.loc["+1y"]
            cur = _f(row.get("current"))
            for col, key in (("90daysAgo", "eps_revision_90d_pct"), ("30daysAgo", "eps_revision_30d_pct")):
                old = _f(row.get(col))
                if cur and old:
                    res[key] = _r((cur / old - 1) * 100, 1)
            res["eps_revision_fy"] = fy_label(cfy + 1)
    except Exception:
        pass
    try:
        rs = t.recommendations
        if rs is not None and not rs.empty:
            row = rs.iloc[0]
            res["recommendations"] = {k: int(row.get(k) or 0) for k in
                                      ("strongBuy", "buy", "hold", "sell", "strongSell")}
    except Exception:
        pass
    return res if got else None


# =====================================================================
# Per-broker rows
# =====================================================================

def _estimates_by_fy(estimates: list) -> dict:
    """{2027: {...row, 'is_actual': bool}} keeping FY rows only."""
    out = {}
    for row in estimates or []:
        p = parse_fy(row.get("fiscal_year"))
        if not p or p[0] != "FY":
            continue
        _, yr, is_actual = p
        out[yr] = {**row, "is_actual": is_actual}
    return out


# Company-level metrics only: "API revenue CAGR" or "Biologics revenue CAGR" are segments.
_GROWTH_PATTERNS = {
    "revenue": re.compile(r"^(consolidated |total |net |overall |group )?(revenue|revenues|sales|net sales|top ?line|income from operations)$"),
    "ebitda": re.compile(r"^(consolidated |total |adj(usted)?\.? |reported )?ebitda$"),
    "pat": re.compile(r"^(consolidated |adj(usted)?\.? |reported )?(pat|net profit|profit after tax|net income|earnings|eps|bottom ?line)$"),
}


def _stated_growth(guidance: list, kind: str) -> dict | None:
    pat = _GROWTH_PATTERNS[kind]
    for g in guidance or []:
        metric = re.sub(r"\s+", " ", str(g.get("metric") or "").strip().lower())
        if _f(g.get("cagr_pct")) is not None and pat.match(metric):
            return {"cagr": _r(_f(g.get("cagr_pct")), 1), "span": g.get("period") or "", "basis": "stated"}
    return None


def _series_cagr(series: dict, by_fy: dict, cfy: int) -> dict | None:
    """CAGR of a per-year series: last actual year (< current FY) -> furthest estimate (<= FY+2)."""
    actual = [y for y in series if (by_fy.get(y, {}).get("is_actual") or y < cfy)]
    est = [y for y in series if cfy <= y <= cfy + 2]
    if not est:
        return None
    base_y = max(actual) if actual else (min(est) if len(est) >= 2 else None)
    end_y = max(est)
    if not base_y or end_y <= base_y or series[base_y] <= 0 or series[end_y] <= 0:
        return None
    val = ((series[end_y] / series[base_y]) ** (1 / (end_y - base_y)) - 1) * 100
    return {"cagr": _r(val, 1), "span": f"{fy_label(base_y)}-{fy_label(end_y)}E", "basis": "estimates"}


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _parse_month_year(text: str) -> tuple[int, int] | None:
    """'Sep-27', "Dec'27", 'September 2027', 'Mar 28E' -> (year, month)."""
    m = re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*['\-/ ]?\s*(\d{4}|\d{2})\b",
                  (text or "").lower())
    if not m:
        return None
    yr = int(m.group(2))
    return (yr + 2000 if yr < 100 else yr), _MONTHS[m.group(1)]


def _target_by(ext: dict, date_iso: str, technical: bool) -> dict | None:
    """When the target price is meant to be reached. A dated target the broker states wins;
    else report date + the stated horizon; else Indian brokers' 12-month convention (flagged
    as assumed). Chart-based trading calls run weeks, so nothing is assumed for them."""
    stated = _parse_month_year(ext.get("target_date") or "")
    if stated:
        yr, mo = stated
        return {"label": f"{_MONTH_NAMES[mo - 1]} {yr}", "iso": f"{yr}-{mo:02d}",
                "basis": "stated", "note": f"Target set for {ext.get('target_date')}"}
    try:
        d = datetime.date.fromisoformat(date_iso)
    except (TypeError, ValueError):
        return None
    months = _f(ext.get("target_horizon_months"))
    assumed = not (months and 0 < months <= 60)
    if assumed:
        if technical:
            return None
        months = 12
    total = d.month - 1 + int(round(months))
    yr, mo = d.year + total // 12, total % 12 + 1
    return {"label": f"{_MONTH_NAMES[mo - 1]} {yr}", "iso": f"{yr}-{mo:02d}",
            "basis": "assumed" if assumed else "horizon",
            "note": (f"Not stated: assumes the usual 12-month target from the {date_iso} report"
                     if assumed else f"{int(round(months))}-month target from the {date_iso} report")}


def build_row(base: dict, ext: dict | None, cmp: float | None, today: datetime.date) -> dict:
    """Merge a report's listing data (Trendlyne / news) with its PDF extraction and compute metrics."""
    ext = ext or {}
    cfy = current_fy(today)

    # The listing (Trendlyne's database / the news call) is authoritative for rating and
    # target; the PDF extraction only fills gaps.
    ext_rating = ext.get("rating") if ext.get("rating") not in (None, "Not rated") else None
    rating = base.get("recommendation") or ext_rating
    tp = _f(base.get("target_price")) or _f(ext.get("target_price"))
    date_iso = base.get("date_iso") or ""
    age = None
    if date_iso:
        try:
            age = (today - datetime.date.fromisoformat(date_iso)).days
        except ValueError:
            pass

    by_fy = _estimates_by_fy(ext.get("estimates") or base.get("eps_estimates") or [])
    eps = {yr: _f(r.get("eps")) for yr, r in by_fy.items() if _f(r.get("eps"))}
    bvps = {yr: _f(r.get("bvps")) for yr, r in by_fy.items() if _f(r.get("bvps"))}

    val = dict(ext.get("valuation") or {})
    if not val and base.get("valuation_method"):
        val = {"method": base.get("valuation_method"), "multiple": base.get("valuation_multiple"),
               "basis_year": base.get("valuation_basis_year")}
    if val.get("method"):
        val["method"] = normalize_method(val["method"])
    bp = parse_fy(val.get("basis_year") or "")
    basis = valuation_basis(val)
    multiple = _f(val.get("multiple"))

    # Press coverage rarely prints an EPS table, but "Rs 1,200 = 40x Sep-28E EPS" pins
    # that year's EPS exactly: target / multiple. Marked as derived in the row.
    eps_derived = []
    if tp and multiple and multiple > 0 and bp and basis == "earnings" and bp[1] not in eps \
            and cfy <= bp[1] <= cfy + 2:
        eps[bp[1]] = tp / multiple
        eps_derived.append(fy_label(bp[1]))

    fwd_pe, fwd_pb = {}, {}
    if cmp:
        for yr in (cfy, cfy + 1, cfy + 2):
            if eps.get(yr) and eps[yr] > 0:
                fwd_pe[fy_label(yr)] = _r(cmp / eps[yr], 1)
            if bvps.get(yr) and bvps[yr] > 0:
                fwd_pb[fy_label(yr)] = _r(cmp / bvps[yr], 2)

    # EPS CAGR: last actual year (<= cfy-1) -> furthest estimate year (<= cfy+2)
    cagr = span = None
    actual_years = [y for y, r in by_fy.items() if (r["is_actual"] or y < cfy) and eps.get(y)]
    est_years = [y for y in eps if y >= cfy and y <= cfy + 2]
    base_y = max(actual_years) if actual_years else (min(est_years) if len(est_years) >= 2 else None)
    end_y = max(est_years) if est_years else None
    if base_y and end_y and end_y > base_y and eps[base_y] > 0 and eps[end_y] > 0 \
            and not (fy_label(base_y) in eps_derived or fy_label(end_y) in eps_derived):
        cagr = ((eps[end_y] / eps[base_y]) ** (1 / (end_y - base_y)) - 1) * 100
        span = f"{fy_label(base_y)}-{fy_label(end_y)}"
    if cagr is None:
        # No estimate table: use a growth rate the broker states for earnings
        # ("39% PAT CAGR over FY26-29E"). PAT growth ~ EPS growth absent dilution.
        for g in ext.get("growth_guidance") or []:
            m = str(g.get("metric") or "").lower()
            if _f(g.get("cagr_pct")) is not None and any(k in m for k in ("eps", "pat", "profit", "earning")):
                cagr = _f(g.get("cagr_pct"))
                span = f"{g.get('period') or ''} (stated)".strip()
                break

    pe2 = fwd_pe.get(fy_label(cfy + 1))
    peg = _r(pe2 / cagr, 2) if pe2 and cagr and cagr > 0 else None

    # Company-level growth the broker expects: a rate it states wins; otherwise computed from
    # its own estimate table (last actual year -> furthest estimate). PAT falls back to EPS.
    guidance = ext.get("growth_guidance") or []
    growth = {}
    for kind, field in (("revenue", "revenue_cr"), ("ebitda", "ebitda_cr"), ("pat", "pat_cr")):
        stated = _stated_growth(guidance, kind)
        if stated:
            growth[kind] = stated
            continue
        series = {y: _f(r.get(field)) for y, r in by_fy.items() if _f(r.get(field))}
        g = _series_cagr(series, by_fy, cfy)
        if g:
            growth[kind] = g
        elif kind == "pat" and cagr is not None:
            growth[kind] = {"cagr": _r(cagr, 1), "span": span, "basis": "EPS"}

    # Implied multiple = target / the per-share figure the broker's multiple is applied to.
    # Book-based (P/B, P/ABV, or an SOTP whose core is valued on ABV) -> BVPS; earnings-based
    # -> EPS; an SOTP / DCF without a stated metric has no single comparable figure.
    implied = None
    if tp and bp and basis and not eps_derived:
        denom_map = bvps if basis == "book" else eps
        if denom_map.get(bp[1]):
            implied = _r(tp / denom_map[bp[1]], 2 if basis == "book" else 1)

    prev_tp = _f(base.get("prev_target_price")) or _f(ext.get("previous_target_price"))
    tp_change = _r((tp / prev_tp - 1) * 100, 1) if tp and prev_tp else None

    # Consensus membership: needs a target, a known date, and to be inside the window.
    if not tp:
        excluded_reason = "no target price"
    elif ext.get("report_type") == "Technical / trading call":
        # A chart-based trading target answers a different question (weeks, not 12 months).
        excluded_reason = "technical call"
    elif base.get("undated"):
        excluded_reason = "date unknown"
    elif age is not None and age > CONSENSUS_MAX_AGE:
        excluded_reason = "older than 6 months"
    else:
        excluded_reason = None
    in_consensus = excluded_reason is None

    def per_fy(field, nd=0):
        return {fy_label(y): _r(_f(r.get(field)), nd) for y, r in sorted(by_fy.items()) if _f(r.get(field)) is not None}

    return {
        "brokerage": base.get("brokerage"),
        "origin": base.get("origin", "Domestic"),
        "source": base.get("source_kind"),
        "title": base.get("title"),
        "report_type": ext.get("report_type"),
        "date": base.get("date"),
        "date_iso": date_iso,
        "age_days": age,
        "stale": age is not None and age > STALE_DAYS,
        "in_consensus": in_consensus,
        "excluded_reason": excluded_reason,
        "rating": rating or "Not rated",
        "rating_raw": base.get("rating_raw") or ext.get("rating_raw") or rating or "Not rated",
        "target_price": _r(tp, 0),
        "target_by": _target_by(ext, date_iso, ext.get("report_type") == "Technical / trading call") if tp else None,
        "upside_pct": _r((tp / cmp - 1) * 100, 1) if tp and cmp else None,
        "bull_case_target": _r(_f(ext.get("bull_case_target")), 0),
        "bear_case_target": _r(_f(ext.get("bear_case_target")), 0),
        "prev_target_price": _r(prev_tp, 0),
        "prev_rating": base.get("prev_rating") or ext.get("previous_rating"),
        "prev_date": base.get("prev_date"),
        "tp_change_pct": tp_change,
        "rating_change": base.get("rating_change"),
        "valuation": {
            "method": val.get("method"),
            "multiple": _f(val.get("multiple")),
            "metric": val.get("metric"),
            "basis_year": val.get("basis_year"),
            "details": val.get("details"),
            "rationale": val.get("rationale"),
        },
        "implied_multiple": implied,
        "eps": {fy_label(y): _r(v, 2) for y, v in sorted(eps.items())},
        "eps_derived": eps_derived,
        "growth_guidance": ext.get("growth_guidance") or [],
        "revenue_cr": per_fy("revenue_cr"),
        "pat_cr": per_fy("pat_cr"),
        "ebitda_cr": per_fy("ebitda_cr"),
        "growth": growth,
        "ebitda_margin_pct": per_fy("ebitda_margin_pct", 1),
        "roe_pct": per_fy("roe_pct", 1),
        "bvps": {fy_label(y): _r(v, 1) for y, v in sorted(bvps.items())},
        "fwd_pe": fwd_pe,
        "fwd_pb": fwd_pb,
        "eps_cagr_pct": _r(cagr, 1),
        "eps_cagr_span": span,
        "peg": peg,
        "estimate_revisions": ext.get("estimate_revisions") or [],
        "thesis": ext.get("thesis") or base.get("thesis") or base.get("summary") or "",
        "key_drivers": ext.get("key_drivers") or [],
        "headwinds": ext.get("headwinds") or [],
        "catalysts": ext.get("catalysts") or [],
        "risks": ext.get("risks") or base.get("risks") or [],
        "management_guidance": ext.get("management_guidance") or [],
        "swot": ext.get("swot") or None,
        "industry_outlook": ext.get("industry_outlook") or None,
        "key_quote": ext.get("key_quote"),
        "detailed_summary": ext.get("detailed_summary") or "",
        "listing_summary": base.get("summary") or "",
        "link": base.get("link"),
        "sources": base.get("sources") or [],
        "pdf_url": base.get("pdf_url"),
        # A broker PDF that was read, vs press coverage of a note that was read in full.
        "has_full_report": bool(ext) and base.get("source_kind") == "Full report",
        "has_detail": bool(ext),
    }


# =====================================================================
# Consensus
# =====================================================================

_RATING_SCORE = {"Buy": 1, "Hold": 0, "Sell": -1}


def compute_consensus(rows: list[dict], market: dict, tp_history: list[dict]) -> dict:
    cmp = market.get("cmp")
    cfy = current_fy()
    fys = [fy_label(cfy), fy_label(cfy + 1), fy_label(cfy + 2)]
    live = [r for r in rows if r["in_consensus"]]

    counts = {"Buy": 0, "Hold": 0, "Sell": 0}
    for r in live:
        counts[r["rating"] if r["rating"] in counts else "Hold"] += 1
    score = (sum(_RATING_SCORE.get(r["rating"], 0) for r in live) / len(live)) if live else None
    if score is None:
        label = "No coverage"
    elif score >= 0.75:
        label = "Strong Buy"
    elif score >= 0.25:
        label = "Buy"
    elif score > -0.25:
        label = "Hold"
    elif score > -0.75:
        label = "Sell"
    else:
        label = "Strong Sell"

    tps = [(r["target_price"], r["brokerage"]) for r in live if r["target_price"]]
    tp_vals = [t for t, _ in tps]
    tp_block = None
    if tp_vals:
        hi = max(tps)
        lo = min(tps)
        med = _median(tp_vals)
        tp_block = {
            "median": _r(med, 0), "mean": _r(statistics.mean(tp_vals), 0),
            "high": _r(hi[0], 0), "high_broker": hi[1],
            "low": _r(lo[0], 0), "low_broker": lo[1],
            "q1": _r(_quantile(tp_vals, 0.25), 0), "q3": _r(_quantile(tp_vals, 0.75), 0),
            "dispersion_pct": _r((hi[0] - lo[0]) / med * 100, 0) if med else None,
            "n": len(tp_vals),
        }

    # EPS dispersion by fiscal year (+ Yahoo/LSEG mean where the year matches)
    yf_eps = {e["fy"]: e for e in ((market.get("consensus") or {}).get("eps") or [])}
    eps_disp = []
    for fy in fys:
        vals = [r["eps"].get(fy) for r in live if r["eps"].get(fy)]
        if not vals and fy not in yf_eps:
            continue
        med = _median(vals)
        eps_disp.append({
            "fy": fy, "n": len(vals),
            "min": _r(min(vals), 2) if vals else None,
            "median": _r(med, 2),
            "max": _r(max(vals), 2) if vals else None,
            "spread_pct": _r((max(vals) - min(vals)) / med * 100, 1) if vals and med else None,
            "fwd_pe_at_median": _r(cmp / med, 1) if cmp and med and med > 0 else None,
            "yf_mean": yf_eps.get(fy, {}).get("mean"),
            "yf_n": yf_eps.get(fy, {}).get("n"),
        })

    # Valuation methods
    methods = {}
    for r in live:
        m = (r["valuation"] or {}).get("method")
        if not m or m == "Not stated":
            continue
        d = methods.setdefault(m, {"method": m, "count": 0, "brokers": [], "multiples": [], "basis_years": set()})
        d["count"] += 1
        d["brokers"].append(r["brokerage"])
        if r["valuation"].get("multiple"):
            d["multiples"].append(r["valuation"]["multiple"])
        if r["valuation"].get("basis_year"):
            d["basis_years"].add(r["valuation"]["basis_year"])
    method_list = []
    for d in sorted(methods.values(), key=lambda x: -x["count"]):
        mults = d.pop("multiples")
        d["basis_years"] = sorted(d["basis_years"])
        d["multiple_low"] = _r(min(mults), 1) if mults else None
        d["multiple_high"] = _r(max(mults), 1) if mults else None
        d["multiple_median"] = _r(_median(mults), 1) if mults else None
        method_list.append(d)

    # Headline valuation metric: P/B when most brokers value it on book (banks / NBFCs)
    # Only rows that state what their multiple is applied to get a vote.
    bases = [valuation_basis(r["valuation"] or {}) for r in live]
    headline_metric = "pb" if bases.count("book") > bases.count("earnings") else "pe"

    fy2 = fys[1]
    cagr_med = _median([r["eps_cagr_pct"] for r in live])
    cagr_source = "brokers" if cagr_med is not None else None
    pe_fy2 = next((e["fwd_pe_at_median"] for e in eps_disp if e["fy"] == fy2), None)
    pe_source = "brokers" if pe_fy2 is not None else None
    bvps_fy2 = _median([r["bvps"].get(fy2) for r in live])

    # Small / mid caps: the brokers in the consensus often come from press coverage with no
    # estimate table. Fall back to the LSEG consensus (Yahoo) so the valuation tiles still
    # answer "how expensive is it", and say where the number came from.
    yf_c = market.get("consensus") or {}
    yf_eps_by_fy = {e["fy"]: e for e in yf_c.get("eps") or []}
    if pe_fy2 is None and cmp and (yf_eps_by_fy.get(fy2) or {}).get("mean"):
        pe_fy2 = _r(cmp / yf_eps_by_fy[fy2]["mean"], 1)
        pe_source = f"LSEG consensus EPS ({yf_eps_by_fy[fy2].get('n') or '?'} analysts)"
    if cagr_med is None:
        g1 = (yf_eps_by_fy.get(fys[0]) or {}).get("growth_pct")
        g2 = (yf_eps_by_fy.get(fy2) or {}).get("growth_pct")
        if g1 is not None and g2 is not None and g1 > -100 and g2 > -100:
            cagr_med = ((1 + g1 / 100) * (1 + g2 / 100)) ** 0.5 * 100 - 100
            cagr_source = f"LSEG consensus, {fy_label(cfy - 1)}-{fy2}"
        elif g2 is not None:
            cagr_med, cagr_source = g2, f"LSEG consensus, {fy2} growth"

    # Target / rating momentum (each broker's latest vs its previous call)
    raised = [r["brokerage"] for r in live if r["tp_change_pct"] is not None and r["tp_change_pct"] > 0.5]
    cut = [r["brokerage"] for r in live if r["tp_change_pct"] is not None and r["tp_change_pct"] < -0.5]
    upgrades = [r["brokerage"] for r in live if r["rating_change"] == "up"]
    downgrades = [r["brokerage"] for r in live if r["rating_change"] == "down"]

    ages = [r["age_days"] for r in live if r["age_days"] is not None]

    return {
        "cmp": _r(cmp, 2),
        "as_of": datetime.date.today().isoformat(),
        "fy_labels": fys,
        "n_reports": len(rows),
        "n_in_consensus": len(live),
        "n_full_reports": sum(1 for r in live if r["has_full_report"]),
        "n_detailed": sum(1 for r in live if r.get("has_detail")),
        "n_full_reports_all": sum(1 for r in rows if r["has_full_report"]),
        "rating_counts": counts,
        "consensus_rating": label,
        "rating_score": _r(score, 2),
        "target": tp_block,
        "upside_median_pct": _r((tp_block["median"] / cmp - 1) * 100, 1) if tp_block and cmp else None,
        "eps_dispersion": eps_disp,
        "headline_metric": headline_metric,
        "fwd_pe_fy2": pe_fy2,
        "fwd_pe_source": pe_source,
        "eps_cagr_source": cagr_source,
        "fwd_pb_fy2": _r(cmp / bvps_fy2, 2) if cmp and bvps_fy2 else None,
        "eps_cagr_median_pct": _r(cagr_med, 1),
        "peg_fy2": _r(pe_fy2 / cagr_med, 2) if pe_fy2 and cagr_med and cagr_med > 0 else None,
        "valuation_methods": method_list,
        "momentum": {
            "tp_raised": raised, "tp_cut": cut,
            "upgrades": upgrades, "downgrades": downgrades,
            "median_tp_change_pct": _r(_median([r["tp_change_pct"] for r in live]), 1),
        },
        "freshness": {
            "median_age_days": _r(_median(ages), 0),
            "n_stale": sum(1 for r in live if r["stale"]),
            "n_excluded_old": sum(1 for r in rows if r["target_price"] and not r["in_consensus"]),
        },
        "yf": market.get("consensus"),
        "current": market.get("current") or {},
        "price_history": market.get("price_history") or [],
        "tp_history": tp_history,
    }
