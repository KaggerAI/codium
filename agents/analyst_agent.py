"""
analyst_agent.py - Analyst Report Agent for the Kagger AI Agent Marketplace.

Pipeline (v2):
  1. Live Trendlyne feed (paginated, 12 months) + market data (price, Yahoo/LSEG consensus) in parallel.
     The pre-fetched JSON cache is only a fallback, and is flagged as stale when used.
  2. Every broker's latest report PDF -> structured extraction (rating, target, valuation
     method & multiple, multi-year estimates, SWOT, industry outlook, catalysts, risks).
     Runs concurrently with
  3. Targeted Perplexity searches per broker group (global houses + domestic institutional
     desks) -> structured news-sourced calls.
  4. Deterministic metrics in code (agents/analyst_metrics.py): upside, forward P/E / P/B,
     EPS CAGR, PEG, implied multiples, consensus stats, EPS dispersion, target momentum.
  5. One structured synthesis call: consolidated SWOT with broker attribution, debates,
     bull / bear case, valuation view, industry outlook, catalysts, risks.

The result carries both the structured data (rendered as tables/charts by agents.html)
and a markdown rendering of it in `analysis` (used by chat and the watchlist modal).
"""

import sys
import os
import re
import json
import time
import asyncio
import datetime
import threading
import traceback
import urllib.parse
import concurrent.futures

from flask import request, jsonify

from agents.base import (
    create_agent_job, update_agent_job, get_agent_job,
    store_latest_result, get_latest_result,
)
from agents.prompts.analyst_prompts import (
    ANALYST_CHAT_PROMPT, ANALYST_SYNTHESIS_STRUCTURED_PROMPT, ANALYST_SYNTHESIS_SCHEMA,
)
from agents import analyst_metrics as am


def _log(*parts):
    """stderr log that cannot raise on a non-ASCII Windows console (cp1252)."""
    try:
        print(*parts, file=sys.stderr)
    except UnicodeEncodeError:
        print(*(str(p).encode("ascii", "replace").decode() for p in parts), file=sys.stderr)


def _json_safe(obj):
    """Replace NaN/Infinity with None. Python's json emits them as bare NaN, which the
    browser's JSON.parse rejects ("Unexpected token 'N'"), failing the whole poll."""
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


RESULT_VERSION = 4          # 4: growth CAGR columns, current-standing tiles
MAX_PDF_REPORTS = 15        # latest report per broker, newest first
PDF_WORKERS = 5           # extraction parallelism; Trendlyne downloads are throttled separately in trendlyne_auth
LOOKBACK_DAYS = 365         # Trendlyne history window (for target momentum + history chart)
NEWS_MONTHS = 6
MAX_ENRICHED_CALLS = 14      # news-sourced calls whose full articles are read


# =====================================================================
# HELPERS
# =====================================================================

_BROKER_STOPWORDS = {
    "ltd", "limited", "pvt", "private", "securities", "security", "capital", "markets",
    "market", "institutional", "equities", "equity", "financial", "services", "broking",
    "stock", "stockbroking", "india", "research", "the", "co", "company", "group", "inc",
    "llc", "plc", "asia", "global", "investment", "investments", "bank", "and",
}


# Same firm, different names in the press vs on Trendlyne.
_BROKER_ALIASES = {
    "pl": "prabhudas lilladher", "prabhudas lilladhar": "prabhudas lilladher",
    "bank america": "bofa", "j p morgan": "jpmorgan", "jp morgan": "jpmorgan",
    "kr choksey": "deven choksey", "spark": "avendus spark",
}


def _norm_broker(name: str) -> str:
    toks = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    toks = [t for t in toks if t not in _BROKER_STOPWORDS]
    norm = " ".join(toks) or (name or "").lower().strip()
    return _BROKER_ALIASES.get(norm, norm)


def _same_broker(a: str, b: str) -> bool:
    na, nb = _norm_broker(a), _norm_broker(b)
    if not na or not nb:
        return False
    # Whole-word containment: "PL Capital (Prabhudas Lilladher)" vs "Prabhudas Lilladher".
    return na == nb or f" {nb} " in f" {na} " or f" {na} " in f" {nb} "


def _parse_any_date(text: str):
    if not text:
        return None
    try:
        from dateutil import parser as dp
        d = dp.parse(text, fuzzy=True, default=datetime.datetime(2000, 1, 1)).date()
        if d.year == 2000 or d > datetime.date.today() + datetime.timedelta(days=2):
            return None
        return d
    except Exception:
        return None


def _pdf_link(pdf_url: str | None) -> str | None:
    if not pdf_url:
        return None
    return "/api/analyst-pdf?url=" + urllib.parse.quote(pdf_url, safe="")


def _cache_reports_to_live_shape(cached: list[dict]) -> list[dict]:
    """Convert entries from analyst_reports_cache.json to the trendlyne_live shape."""
    out = []
    for r in cached or []:
        d = _parse_any_date(r.get("date", ""))
        try:
            tp = float(str(r.get("target_price", "")).replace(",", ""))
        except ValueError:
            tp = None
        out.append({
            "brokerage": r.get("brokerage", "Unknown"),
            "date": d.strftime("%d %b %Y") if d else r.get("date", ""),
            "date_iso": d.isoformat() if d else "",
            "rating_raw": r.get("recommendation", ""),
            "recommendation": r.get("recommendation", "Hold"),
            "target_price": tp,
            "title": "",
            "summary": r.get("summary", ""),
            "pdf_url": r.get("pdf_url"),
            "source": "Trendlyne (cached)",
        })
    return out


def _latest_per_broker(reports: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    reports: newest first, possibly several per broker.
    Returns (latest report per broker with prev_* fields attached, full target history).
    """
    by_broker: dict[str, list[dict]] = {}
    order = []
    for r in sorted(reports, key=lambda x: x.get("date_iso") or "", reverse=True):
        key = _norm_broker(r["brokerage"])
        if key not in by_broker:
            by_broker[key] = []
            order.append(key)
        by_broker[key].append(r)

    score = {"Buy": 1, "Hold": 0, "Sell": -1}
    latest = []
    for key in order:
        hist = by_broker[key]
        cur = dict(hist[0])
        # Previous call = the most recent older report with a target (skip same-day duplicates)
        prev = next((h for h in hist[1:] if h.get("target_price") and h.get("date_iso") != cur.get("date_iso")), None)
        if prev:
            cur["prev_target_price"] = prev.get("target_price")
            cur["prev_rating"] = prev.get("rating_raw") or prev.get("recommendation")
            cur["prev_date"] = prev.get("date")
            a, b = score.get(cur.get("recommendation"), 0), score.get(prev.get("recommendation"), 0)
            cur["rating_change"] = "up" if a > b else "down" if a < b else None
        latest.append(cur)

    history = [
        {"brokerage": r["brokerage"], "date_iso": r["date_iso"], "target_price": r.get("target_price"),
         "rating": r.get("recommendation")}
        for r in reports if r.get("date_iso") and r.get("target_price")
    ]
    return latest, history


# =====================================================================
# MARKDOWN RENDERING (for chat context + watchlist modal)
# =====================================================================

def _fmt_num(x, prefix="", suffix="", nd=0):
    if x is None:
        return "—"
    return f"{prefix}{x:,.{nd}f}{suffix}"


def _brokers(lst):
    return f" _({', '.join(lst)})_" if lst else ""


def _g(r, kind):
    g = (r.get("growth") or {}).get(kind) or {}
    if g.get("cagr") is None:
        return "—"
    note = ", stated" if g.get("basis") == "stated" else ""
    return f"{g['cagr']:.0f}% ({g.get('span') or ''}{note})"


def build_markdown(company_name, ticker, cons, rows, syn) -> str:
    syn = syn or {}
    fys = cons.get("fy_labels", [])
    fy1, fy2 = (fys + ["", ""])[:2]
    tp = cons.get("target") or {}
    rc = cons.get("rating_counts", {})
    md = []
    if syn.get("verdict_headline"):
        md.append(f"## Verdict\n\n**{syn['verdict_headline']}**\n")
    cur = cons.get("current") or {}
    if cur:
        q = f" ({cur.get('quarter')})" if cur.get("quarter") else ""
        md.append("## Where the stock stands today\n")
        md.append("| Metric | Value |\n|---|---|")
        md.append(f"| Current price | {_fmt_num(cur.get('cmp') or cons.get('cmp'), '₹', nd=2)} |")
        md.append(f"| P/E (trailing 12m) | {_fmt_num(cur.get('pe_ttm'), suffix='x', nd=1)} |")
        md.append(f"| EPS (trailing 12m) | {_fmt_num(cur.get('eps_ttm'), '₹', nd=2)} |")
        md.append(f"| Revenue YoY{q} | {_fmt_num(cur.get('revenue_yoy'), suffix='%', nd=1)} |")
        md.append(f"| EBITDA YoY{q} | {_fmt_num(cur.get('ebitda_yoy'), suffix='%', nd=1)} |")
        md.append(f"| PAT YoY{q} | {_fmt_num(cur.get('pat_yoy'), suffix='%', nd=1)} |")
        md.append("")
    md.append("## Consensus snapshot\n")
    md.append("| Metric | Value |\n|---|---|")
    md.append(f"| Consensus rating | {cons.get('consensus_rating')} ({rc.get('Buy',0)} Buy / {rc.get('Hold',0)} Hold / {rc.get('Sell',0)} Sell) |")
    md.append(f"| CMP | {_fmt_num(cons.get('cmp'), '₹', nd=2)} |")
    md.append(f"| Median target | {_fmt_num(tp.get('median'), '₹')} (upside {_fmt_num(cons.get('upside_median_pct'), suffix='%', nd=1)}) |")
    md.append(f"| Target range | {_fmt_num(tp.get('low'), '₹')} ({tp.get('low_broker','')}) – {_fmt_num(tp.get('high'), '₹')} ({tp.get('high_broker','')}) |")
    md.append(f"| Fwd P/E {fy2}E (at median EPS) | {_fmt_num(cons.get('fwd_pe_fy2'), suffix='x', nd=1)} |")
    md.append(f"| Median EPS CAGR | {_fmt_num(cons.get('eps_cagr_median_pct'), suffix='%', nd=1)} |")
    md.append(f"| Reports in consensus | {cons.get('n_in_consensus')} ({cons.get('n_full_reports')} full PDFs read) |")
    yf = cons.get("yf") or {}
    if yf.get("price_target"):
        md.append(f"| Yahoo/LSEG mean target | {_fmt_num(yf['price_target'].get('mean'), '₹')} |")
    if yf.get("eps_revision_90d_pct") is not None:
        md.append(f"| {yf.get('eps_revision_fy','')} EPS revision, 90d (LSEG) | {yf['eps_revision_90d_pct']}% |")
    md.append("")
    if syn.get("bottom_line"):
        md.append(f"## Bottom line\n\n{syn['bottom_line']}\n")

    md.append("## Broker comparison\n")
    md.append(f"| Broker | Date | Rating | Target | Upside | Method | EPS {fy1}E | EPS {fy2}E | P/E {fy2}E | Revenue CAGR | EBITDA CAGR | PAT CAGR | Source |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        v = r.get("valuation") or {}
        meth = v.get("method") or "—"
        if v.get("multiple"):
            meth += f" {v['multiple']}x {v.get('basis_year') or ''}".rstrip()
        md.append(
            f"| {r['brokerage']} | {r.get('date') or '—'}{' (stale)' if r.get('stale') else ''} | {r.get('rating_raw') or r['rating']} | "
            f"{_fmt_num(r.get('target_price'), '₹')} | {_fmt_num(r.get('upside_pct'), suffix='%', nd=1)} | {meth} | "
            f"{_fmt_num(r['eps'].get(fy1), nd=1)} | {_fmt_num(r['eps'].get(fy2), nd=1)} | {_fmt_num(r['fwd_pe'].get(fy2), suffix='x', nd=1)} | "
            f"{_g(r, 'revenue')} | {_g(r, 'ebitda')} | {_g(r, 'pat')} | {r.get('source')} |"
        )
    md.append("")
    if syn.get("valuation_view"):
        md.append(f"## How the street values it\n\n{syn['valuation_view']}\n")

    sw = syn.get("swot") or {}
    if any(sw.get(k) for k in ("strengths", "weaknesses", "opportunities", "threats")):
        md.append("## SWOT (consolidated)\n")
        for k in ("strengths", "weaknesses", "opportunities", "threats"):
            if sw.get(k):
                md.append(f"**{k.title()}**")
                md.extend(f"- {p['point']}{_brokers(p.get('brokers'))}" for p in sw[k])
                md.append("")

    if syn.get("bull_case") or syn.get("bear_case"):
        md.append(f"## Bull vs bear\n\n**Bull case:** {syn.get('bull_case','')}\n\n**Bear case:** {syn.get('bear_case','')}\n")
    for d in syn.get("debates") or []:
        md.append(f"### Debate: {d.get('topic')}\n- **Bull:** {d.get('bull_view')}{_brokers(d.get('bull_brokers'))}\n- **Bear:** {d.get('bear_view')}{_brokers(d.get('bear_brokers'))}\n")

    io = syn.get("industry_outlook") or {}
    if io.get("summary"):
        md.append(f"## Industry outlook\n\n{io['summary']}\n")
        md.extend(f"- {g['point']}{_brokers(g.get('brokers'))}" for g in io.get("growth_figures") or [])
        if io.get("tailwinds"):
            md.append("\n**Tailwinds:** " + "; ".join(io["tailwinds"]))
        if io.get("headwinds"):
            md.append("\n**Headwinds:** " + "; ".join(io["headwinds"]))
        md.append("")
    if syn.get("catalysts"):
        md.append("## Catalysts\n")
        md.extend(f"- {c['point']}{_brokers(c.get('brokers'))}" for c in syn["catalysts"])
        md.append("")
    if syn.get("risks"):
        md.append("## Key risks\n\n| Risk | Severity | Flagged by |\n|---|---|---|")
        md.extend(f"| {r['point']} | {r.get('severity','')} | {', '.join(r.get('brokers') or [])} |" for r in syn["risks"])
        md.append("")
    if syn.get("estimate_momentum"):
        md.append(f"## Estimate momentum\n\n{syn['estimate_momentum']}\n")
    return "\n".join(md)


def _rows_for_llm(rows: list[dict], include_summary: bool = False) -> list[dict]:
    keep = ["brokerage", "origin", "source", "date", "age_days", "rating_raw", "rating",
            "target_price", "upside_pct", "prev_target_price", "tp_change_pct", "rating_change",
            "bull_case_target", "bear_case_target", "valuation", "implied_multiple", "eps",
            "revenue_cr", "pat_cr", "ebitda_margin_pct", "roe_pct", "fwd_pe", "fwd_pb",
            "eps_cagr_pct", "eps_cagr_span", "growth", "ebitda_cr", "eps_derived", "growth_guidance", "estimate_revisions", "thesis",
            "key_drivers", "catalysts", "risks", "management_guidance", "swot",
            "industry_outlook", "key_quote"]
    if include_summary:
        keep.append("detailed_summary")
    out = []
    for r in rows:
        d = {k: r.get(k) for k in keep if r.get(k) not in (None, "", [], {})}
        if not r.get("has_full_report") and r.get("listing_summary"):
            d["listing_summary"] = r["listing_summary"]
        out.append(d)
    return out


# =====================================================================
# BACKGROUND PIPELINE
# =====================================================================

def _run_analyst_analysis(job_id, ticker, company_name, call_gemini_api_fn,
                          call_perplexity_search_api_fn, fetch_analyst_reports_async_fn):
    from agents.analyst_extract import (
        extract_pdf_report, load_cached_extraction, fetch_news_calls, gemini_json, step_config,
    )
    from google.genai import types

    start = time.time()
    today = datetime.date.today()
    warnings: list[str] = []
    source_notes: dict = {}

    try:
        # ── Step 1: Trendlyne feed + market data in parallel ───────────
        update_agent_job(job_id, {"progress": f"Fetching the live brokerage report feed and market data for {ticker}..."})
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        market_f = pool.submit(am.fetch_market_data, ticker)

        reports: list[dict] = []
        try:
            from analyst_reports.trendlyne_live import fetch_trendlyne_reports_live
            reports = fetch_trendlyne_reports_live(ticker, max_pages=3, lookback_days=LOOKBACK_DAYS)
            source_notes["trendlyne"] = "live"
        except Exception as e:
            _log(f"ANALYST_AGENT: live Trendlyne failed for {ticker}: {e}")
            source_notes["trendlyne"] = "failed"
            try:
                loop = asyncio.new_event_loop()
                cached = loop.run_until_complete(fetch_analyst_reports_async_fn(ticker))
                loop.close()
                reports = _cache_reports_to_live_shape(cached)
                if reports:
                    source_notes["trendlyne"] = "cache"
                    warnings.append("Live Trendlyne fetch failed, so domestic reports come from an older "
                                    "cached snapshot and may be months out of date.")
            except Exception as ce:
                _log(f"ANALYST_AGENT: cache fallback failed: {ce}")
        if source_notes["trendlyne"] == "failed":
            warnings.append("Could not reach Trendlyne, so no domestic brokerage reports were loaded.")

        latest, tp_history = _latest_per_broker(reports)
        _log(f"ANALYST_AGENT: {len(reports)} Trendlyne reports, {len(latest)} brokers")

        # ── Step 2 + 3: PDF extraction  ||  news-sourced calls ─────────
        domestic_names = [r["brokerage"] for r in latest]
        news_f = pool.submit(fetch_news_calls, ticker, company_name, call_perplexity_search_api_fn,
                             domestic_names, NEWS_MONTHS)
        from agents.analyst_extract import discover_coverage_roster
        roster_f = pool.submit(discover_coverage_roster, ticker, company_name, NEWS_MONTHS)

        to_read = [r for r in latest if r.get("pdf_url")][:MAX_PDF_REPORTS]
        extractions: dict[str, dict] = {}
        pending = []
        for r in to_read:
            c = load_cached_extraction(r["pdf_url"])
            if c:
                extractions[r["pdf_url"]] = c
            else:
                pending.append(r)

        done_count = [len(extractions)]
        lock = threading.Lock()
        total = len(to_read)

        def _progress():
            update_agent_job(job_id, {"progress": f"Reading full brokerage reports: {done_count[0]}/{total} done "
                                                  f"(also searching news for global brokerage calls)..."})

        download_errors: list[str] = []
        network_failed: list[dict] = []

        def _read_one(r):
            # trendlyne_auth: cookies -> re-login on expiry (if credentials are set),
            # direct -> residential proxy on block, and records session health.
            from analyst_reports.trendlyne_auth import download_pdf_sync
            try:
                try:
                    pdf = download_pdf_sync(r["pdf_url"])
                except Exception as de:
                    with lock:
                        download_errors.append(str(de))
                        if "Could not reach Trendlyne" in str(de):
                            network_failed.append(r)
                    _log(f"ANALYST_AGENT: PDF download failed for {r['brokerage']}: {str(de)[:200]}")
                    return r["pdf_url"], None
                if not pdf or len(pdf) < 1000:
                    _log(f"ANALYST_AGENT: no PDF bytes for {r['brokerage']}")
                    return r["pdf_url"], None
                ext = extract_pdf_report(pdf, r["pdf_url"], r["brokerage"], ticker, company_name, r.get("date", ""))
                return r["pdf_url"], ext
            except Exception as e:
                _log(f"ANALYST_AGENT: extraction failed for {r['brokerage']}: {str(e)[:200]}")
                return r["pdf_url"], None
            finally:
                with lock:
                    done_count[0] += 1
                    _progress()

        if total:
            _progress()
        if pending:
            with concurrent.futures.ThreadPoolExecutor(max_workers=PDF_WORKERS) as ex:
                for url, ext in ex.map(_read_one, pending):
                    if ext:
                        extractions[url] = ext
        # Trendlyne sometimes resets connections in bursts: one more, unhurried pass for the
        # reports that failed on the network (not off-site broker hosts, not login/quota).
        retry = [r for r in network_failed if r["pdf_url"] not in extractions]
        if retry:
            time.sleep(5)
            download_errors[:] = [e for e in download_errors if "Could not reach Trendlyne" not in e]
            with lock:
                done_count[0] -= len(retry)
            for r in retry:
                url, ext = _read_one(r)
                if ext:
                    extractions[url] = ext
        n_failed = sum(1 for r in to_read if r["pdf_url"] not in extractions)
        if n_failed:
            pool_down = any("No Trendlyne account" in e for e in download_errors)
            if pool_down:
                warnings.append(f"No Trendlyne account could download reports right now (monthly limits reached "
                                f"or sessions expired), so {n_failed} of {total} brokers show Trendlyne's short "
                                "summary only. Details are on the Admin page (Trendlyne accounts).")
            else:
                warnings.append(f"{n_failed} of {total} report PDFs could not be read; those brokers show "
                                "Trendlyne's short summary only.")

        update_agent_job(job_id, {"progress": "Collecting news-sourced brokerage calls (global houses)..."})
        try:
            news_calls, n_news_sources, news_results = news_f.result(timeout=240)
        except Exception as e:
            _log(f"ANALYST_AGENT: news calls failed: {e}")
            news_calls, n_news_sources, news_results = [], 0, []
        source_notes["news_sources"] = n_news_sources

        try:
            roster = roster_f.result(timeout=120)
        except Exception as e:
            _log(f"ANALYST_AGENT: coverage roster failed: {e}")
            roster = []
        market = market_f.result(timeout=120)
        pool.shutdown(wait=False)
        cmp = market.get("cmp")
        if not cmp:
            warnings.append("Live price unavailable, so upside and forward multiples are not computed.")

        # ── Step 4: rows + deterministic metrics ──────────────────────
        rows = []
        for r in latest:
            base = {**r, "source_kind": "Full report" if r.get("pdf_url") in extractions else "Trendlyne summary",
                    "origin": "Domestic", "link": _pdf_link(r.get("pdf_url"))}
            rows.append(am.build_row(base, extractions.get(r.get("pdf_url")), cmp, today))

        kept_calls = []
        for c in news_calls:
            if any(_same_broker(c.get("brokerage", ""), n) for n in domestic_names):
                continue
            if any(_same_broker(c.get("brokerage", ""), k["brokerage"]) for k, _ in kept_calls):
                continue
            d = _parse_any_date(c.get("date", ""))
            if not d or (today - d).days > NEWS_MONTHS * 31:
                continue
            kept_calls.append((c, d))

        # Brokers that the roster names, or that appear by name next to the company in any
        # search result, but that the news extraction missed, become LEADS. A lead is kept only
        # if enrichment then finds real articles naming the broker and the company.
        from agents.analyst_extract import scan_broker_mentions
        mentions = scan_broker_mentions(news_results, company_name, ticker)
        leads = [{**m, "lead_source": "mention"} for m in mentions] + \
                [{**r, "lead_source": r.get("roster_source", "roster")} for r in roster]
        known = domestic_names + [c.get("brokerage", "") for c, _ in kept_calls]
        n_leads = 0
        for lead in leads:
            name = (lead.get("brokerage") or "").strip()
            if not name or any(_same_broker(name, k) for k in known):
                continue
            d = _parse_any_date(lead.get("date") or "")
            if d and (today - d).days > NEWS_MONTHS * 31:
                continue
            known.append(name)
            n_leads += 1
            kept_calls.append(({"brokerage": name, "date": lead.get("date"), "lead": True,
                                "source_url": lead.get("url"), "origin": "Domestic"}, d))
        source_notes["coverage_leads"] = n_leads

        # Read the full articles behind each news call (plus alternate outlets covering the
        # same note) so news-sourced brokers get thesis / valuation / growth / risks too.
        enrichments: dict[int, tuple] = {}
        if kept_calls:
            update_agent_job(job_id, {"progress": f"Reading full articles for {len(kept_calls)} news-sourced brokerage calls..."})
            from agents.analyst_extract import enrich_news_call
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                futs = {ex.submit(enrich_news_call, c, company_name, ticker, call_perplexity_search_api_fn): i
                        for i, (c, _) in enumerate(kept_calls[:MAX_ENRICHED_CALLS])}
                for f in concurrent.futures.as_completed(futs):
                    try:
                        enrichments[futs[f]] = f.result()
                    except Exception as e:
                        _log(f"ANALYST_AGENT: enrichment failed: {str(e)[:160]}")

        global_rows = []
        for i, (c, d) in enumerate(kept_calls):
            ext, sources = enrichments.get(i, (None, []))
            no_view = bool(ext) and ext.get("broker_view_found") is False
            if c.get("lead"):
                # Lead: kept only when the articles read show this broker's own view, and at
                # least one of them is established financial press (not a blog / aggregator).
                if not ext or no_view or not any(s.get("trusted") for s in sources):
                    continue
                ext_rating = ext.get("rating") if ext.get("rating") not in (None, "Not rated") else None
                if not (ext_rating or ext.get("target_price") or (ext.get("thesis") or "").strip()):
                    continue        # articles mention the broker but carry no view of its own
                # A lead's rating / target come only from the articles actually read.
                c = {**c, "rating": ext_rating or "Not rated", "rating_raw": ext.get("rating_raw"),
                     "target_price": ext.get("target_price")}
            elif no_view:
                ext, sources = None, []     # keep the call itself; the articles found weren't about it
            base = {
                "brokerage": c.get("brokerage"), "origin": c.get("origin") or "Global",
                "source_kind": "News",
                "date": d.strftime("%d %b %Y") if d else "", "date_iso": d.isoformat() if d else "",
                "undated": d is None,
                "recommendation": c.get("rating") if c.get("rating") not in (None, "Not rated") else None,
                "rating_raw": c.get("rating_raw") or (None if c.get("rating") in (None, "Not rated") else c.get("rating")),
                "target_price": c.get("target_price"),
                "prev_target_price": c.get("previous_target_price"),
                "valuation_method": c.get("valuation_method"),
                "valuation_multiple": c.get("valuation_multiple"),
                "valuation_basis_year": c.get("valuation_basis_year"),
                "eps_estimates": c.get("eps_estimates") or [],
                "thesis": c.get("thesis"), "risks": c.get("risks") or [],
                "link": c.get("source_url") or (sources[0]["url"] if sources else None),
                "sources": sources,
            }
            global_rows.append(am.build_row(base, ext, cmp, today))

        all_rows = rows + global_rows
        all_rows.sort(key=lambda r: (not r["in_consensus"], -(datetime.date.fromisoformat(r["date_iso"]).toordinal() if r.get("date_iso") else 0)))

        if not all_rows:
            update_agent_job(job_id, {
                "status": "error",
                "error": f"No analyst reports found for {ticker}. This stock may not have recent brokerage coverage."
                         + (" " + " ".join(warnings) if warnings else ""),
            })
            return

        consensus = am.compute_consensus(all_rows, market, tp_history)

        # ── Step 5: structured synthesis ──────────────────────────────
        update_agent_job(job_id, {"progress": f"Synthesising {len(all_rows)} brokerage views..."})
        cons_for_llm = {k: v for k, v in consensus.items() if k not in ("price_history", "tp_history", "peg_fy2")}
        payload = json.dumps({"computed_metrics": cons_for_llm, "reports": _rows_for_llm(all_rows)},
                             ensure_ascii=False, default=str)
        prompt = ANALYST_SYNTHESIS_STRUCTURED_PROMPT.format(
            company_name=company_name, ticker=ticker, today=today.strftime("%d %b %Y"),
            cmp=f"Rs {cmp:,.2f}" if cmp else "not available", payload=payload[:300000],
        )
        synthesis = {}
        try:
            synthesis = gemini_json([types.Part.from_text(text=prompt)], ANALYST_SYNTHESIS_SCHEMA,
                                    usage_tag="analyst.synthesis", temperature=0.4,
                                    **step_config("synthesis"))
        except Exception as e:
            _log(f"ANALYST_AGENT: synthesis failed: {e}")
            warnings.append("The cross-broker synthesis step failed; tables and per-broker details are still shown.")

        analysis_md = build_markdown(company_name, ticker, consensus, all_rows, synthesis)
        elapsed = int(time.time() - start)

        result = {
            "version": RESULT_VERSION,
            "ticker": ticker,
            "company_name": company_name,
            "cmp": cmp,
            "consensus": consensus,
            "rows": all_rows,
            "synthesis": synthesis,
            "analysis": analysis_md,
            "warnings": warnings,
            "source_notes": source_notes,
            # Back-compat fields (watchlist modal, chat payloads)
            "domestic_reports": [
                {"brokerage": r["brokerage"], "recommendation": r["rating"], "target_price": r["target_price"],
                 "date": r["date"], "summary": r["thesis"] or r["listing_summary"], "pdf_url": r["link"]}
                for r in rows
            ],
            "global_reports": [
                {"brokerage": r["brokerage"], "recommendation": r["rating"], "target_price": r["target_price"],
                 "date": r["date"], "summary": r["thesis"], "source_url": r["link"]}
                for r in global_rows
            ],
            "domestic_count": len(rows),
            "global_count": len(global_rows),
            "pdfs_analyzed": len(extractions),
            "analyzed_at": time.time(),
            "analysis_time_seconds": elapsed,
        }
        result = _json_safe(result)
        store_latest_result("analyst", ticker, result)
        update_agent_job(job_id, {
            "status": "complete", "progress": "Analysis complete!", "result": result,
            "completed_at": time.time(), "total_time": elapsed,
        })
        _log(f"ANALYST_AGENT: done for {ticker} in {elapsed}s ({len(rows)} domestic, "
             f"{len(global_rows)} news, {len(extractions)} PDFs)")

    except Exception as e:
        _log(f"ANALYST_AGENT ERROR: {e}")
        traceback.print_exc(file=sys.stderr)
        update_agent_job(job_id, {"status": "error", "error": f"Analysis failed: {str(e)}"})


# =====================================================================
# FLASK ROUTE REGISTRATION
# =====================================================================

def register_analyst_routes(app, call_gemini_api_fn, call_perplexity_search_api_fn,
                            fetch_analyst_reports_async_fn):
    """Register all Analyst Report Agent API routes."""

    def _get_company_name(ticker: str) -> str:
        try:
            from analyst_reports.trendlyne_fetcher import TRENDLYNE_DATA
            data = TRENDLYNE_DATA.get(ticker.upper())
            if data:
                return data.get("stock_name", ticker)
        except Exception:
            pass
        return ticker

    @app.route("/agent/analyst/analyze", methods=["POST"])
    def agent_analyst_analyze():
        try:
            data = request.get_json(force=True)
            ticker = data.get("ticker", "").strip().upper()
            if not ticker:
                return jsonify({"error": "Ticker is required"}), 400

            company_name = _get_company_name(ticker)
            _log(f"ANALYST_AGENT: Starting analysis for {ticker} ({company_name})")

            existing = get_latest_result("analyst", ticker)
            if (existing and data.get("force_refresh") is not True
                    and (existing.get("result") or {}).get("version") == RESULT_VERSION):
                age_minutes = (time.time() - existing["stored_at"]) / 60
                if age_minutes < 60:
                    return jsonify({"status": "complete", "result": _json_safe(existing["result"]),
                                    "cached": True, "age_minutes": round(age_minutes)})

            job_id = create_agent_job("analyst", ticker)
            thread = threading.Thread(
                target=_run_analyst_analysis,
                args=(job_id, ticker, company_name, call_gemini_api_fn,
                      call_perplexity_search_api_fn, fetch_analyst_reports_async_fn),
                daemon=True,
            )
            thread.start()
            return jsonify({"job_id": job_id, "status": "processing",
                            "message": f"Analyst report analysis started for {ticker}."})
        except Exception as e:
            _log(f"ANALYST_AGENT ERROR: {e}")
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500

    @app.route("/agent/analyst/<job_id>/status", methods=["GET"])
    def agent_analyst_status(job_id):
        job = get_agent_job(job_id)
        if not job:
            return jsonify({"error": "Job not found or expired"}), 404
        if job["status"] == "processing":
            return jsonify({"status": "processing", "progress": job["progress"],
                            "elapsed_seconds": int(time.time() - job["started_at"])})
        if job["status"] == "complete":
            return jsonify({"status": "complete", "result": _json_safe(job["result"])})
        if job["status"] == "error":
            return jsonify({"status": "error", "error": job["error"]})
        return jsonify({"error": "Unknown job status"}), 500

    @app.route("/agent/analyst/chat", methods=["POST"])
    def agent_analyst_chat():
        try:
            data = request.get_json(force=True)
            question = data.get("question", "").strip()
            ticker = data.get("ticker", "").strip().upper()
            analysis_context = data.get("analysis_context", "").strip()
            company_name = data.get("company_name", _get_company_name(ticker))

            if not question:
                return jsonify({"error": "Question is required"}), 400
            if not analysis_context:
                return jsonify({"error": "Analysis context is required. Run the analysis first."}), 400

            # Prefer the full structured result held server-side; fall back to the client's text.
            domestic_text = data.get("domestic_reports_text", "Not available")
            global_text = data.get("global_reports_text", "Not available")
            stored = get_latest_result("analyst", ticker) if ticker else None
            res = (stored or {}).get("result") or {}
            if res.get("version") == RESULT_VERSION and res.get("rows"):
                dom = [r for r in res["rows"] if r.get("source") != "News"]
                glob = [r for r in res["rows"] if r.get("source") == "News"]
                domestic_text = json.dumps(_rows_for_llm(dom, include_summary=True), ensure_ascii=False, default=str)
                global_text = json.dumps(_rows_for_llm(glob), ensure_ascii=False, default=str)
                analysis_context = res.get("analysis") or analysis_context

            chat_prompt = ANALYST_CHAT_PROMPT.format(
                company_name=company_name, ticker=ticker,
                analysis=analysis_context[:40000],
                domestic_reports=domestic_text[:120000],
                global_reports=global_text[:30000],
            )
            messages = [{"role": "system", "content": chat_prompt},
                        {"role": "user", "content": question}]
            answer = call_gemini_api_fn(messages, model="gemini-3.5-flash-lite",
                                        temperature=1, thinking_level="MEDIUM",
                                        usage_tag="analyst.chat")
            return jsonify({"answer": answer, "status": "success"})
        except Exception as e:
            _log(f"ANALYST_AGENT_CHAT ERROR: {e}")
            traceback.print_exc(file=sys.stderr)
            return jsonify({"error": str(e)}), 500

    # =================================================================
    # Admin: Trendlyne session health (Admin page card)
    # =================================================================
    def _require_admin():
        from flask import session as flask_session
        if "user_id" not in flask_session:
            return jsonify({"error": "Authentication required"}), 401
        from auth.database import User
        user = User.get_by_id(flask_session["user_id"])
        if not user or not user.is_admin:
            return jsonify({"error": "Admin access required"}), 403
        return None

    @app.route("/api/admin/trendlyne/status", methods=["GET"])
    def admin_trendlyne_status():
        denied = _require_admin()
        if denied:
            return denied
        from analyst_reports import trendlyne_auth as ta
        return jsonify({"status": ta.get_status()})

    @app.route("/api/admin/trendlyne/check", methods=["POST"])
    def admin_trendlyne_check():
        denied = _require_admin()
        if denied:
            return denied
        from analyst_reports import trendlyne_auth as ta
        aid = str((request.get_json(silent=True) or {}).get("account_id") or "") or None
        return jsonify(ta.check_session(aid))

    @app.route("/api/admin/trendlyne/cookies", methods=["POST"])
    def admin_trendlyne_cookies():
        denied = _require_admin()
        if denied:
            return denied
        from analyst_reports import trendlyne_auth as ta
        body = request.get_json(force=True) or {}
        aid = str(body.get("account_id") or "1")
        try:
            cookies = ta.parse_cookie_input(body.get("cookie", ""))
            ta.set_cookies_manually(cookies, aid)
        except (ValueError, json.JSONDecodeError) as e:
            return jsonify({"error": str(e)}), 400
        check = ta.check_session(aid)
        return jsonify({"saved": True, "cookie_names": sorted(cookies.keys()), **check})

    @app.route("/api/admin/trendlyne/relogin", methods=["POST"])
    def admin_trendlyne_relogin():
        denied = _require_admin()
        if denied:
            return denied
        from analyst_reports import trendlyne_auth as ta
        aid = str((request.get_json(silent=True) or {}).get("account_id") or "1")
        try:
            ta.force_login(aid)
        except Exception as e:
            return jsonify({"error": str(e), "status": ta.get_status()}), 400
        return jsonify(ta.check_session(aid))

    @app.route("/api/admin/trendlyne/reset", methods=["POST"])
    def admin_trendlyne_reset():
        """Clear an account's cool-downs (limit / error / expired) so it is tried again now."""
        denied = _require_admin()
        if denied:
            return denied
        from analyst_reports import trendlyne_auth as ta
        aid = str((request.get_json(silent=True) or {}).get("account_id") or "1")
        ta.reset_cooldowns(aid)
        return jsonify({"status": ta.get_status()})

    @app.route("/agent/analyst/latest", methods=["GET"])
    def agent_analyst_latest():
        ticker = request.args.get("ticker", "").strip().upper()
        if not ticker:
            return jsonify({"error": "Ticker is required"}), 400
        result = get_latest_result("analyst", ticker)
        if result:
            return jsonify({"status": "complete", "result": _json_safe(result["result"]),
                            "age_minutes": round((time.time() - result["stored_at"]) / 60)})
        return jsonify({"status": "none", "message": "No cached result found"})
