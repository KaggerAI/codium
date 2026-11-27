"""
eval_extraction_models.py - Does a cheaper model extract broker reports as well?

Runs the Analyst Report Agent's PDF extraction on the same real reports with two
model configurations and compares them field by field, plus token usage.

    venv/Scripts/python.exe -m analyst_reports.eval_extraction_models TCS HDFCBANK --n 4
    venv/Scripts/python.exe -m analyst_reports.eval_extraction_models TCS \
        --a gemini-3.8-flash:LOW --b gemini-3.5-flash-lite:MINIMAL

What it checks (numbers are what the agent's tables and computed P/E, PEG, CAGR rest on):
  rating, target price, valuation method / multiple / basis year, and EPS, revenue,
  PAT per fiscal year (equal within 1%). Qualitative fields (thesis, SWOT) are written
  side by side into a markdown report for a human read.

Agreement does not prove correctness, but disagreements point at exactly which
numbers to check in the PDF. Production extraction cache is never touched.
Needs GOOGLE_API_KEY and working Trendlyne cookies (or TRENDLYNE_USERNAME/PASSWORD).
"""

import os
import sys
import json
import time
import argparse
import datetime
import concurrent.futures

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _out(s=""):
    try:
        print(s)
    except UnicodeEncodeError:
        print(str(s).encode("ascii", "replace").decode())


def _close(a, b, tol=0.01):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return str(a).strip().lower() == str(b).strip().lower()
    if a == b:
        return True
    return abs(a - b) <= tol * max(abs(a), abs(b))


def _norm_fy(label):
    from agents.analyst_metrics import parse_fy
    p = parse_fy(label or "")
    return f"FY{p[1] % 100:02d}" if p and p[0] == "FY" else (label or "").upper()


def compare(a: dict, b: dict) -> list[tuple[str, object, object, bool]]:
    rows = []

    def add(name, va, vb, numeric=True):
        same = _close(va, vb) if numeric else (str(va or "").strip().lower() == str(vb or "").strip().lower())
        rows.append((name, va, vb, same))

    add("rating", a.get("rating"), b.get("rating"), numeric=False)
    add("target_price", a.get("target_price"), b.get("target_price"))
    va, vb = a.get("valuation") or {}, b.get("valuation") or {}
    add("valuation.method", va.get("method"), vb.get("method"), numeric=False)
    add("valuation.multiple", va.get("multiple"), vb.get("multiple"))
    add("valuation.basis_year", _norm_fy(va.get("basis_year")), _norm_fy(vb.get("basis_year")), numeric=False)

    ea = {_norm_fy(r.get("fiscal_year")): r for r in a.get("estimates") or []}
    eb = {_norm_fy(r.get("fiscal_year")): r for r in b.get("estimates") or []}
    for fy in sorted(set(ea) | set(eb)):
        for field in ("eps", "revenue_cr", "pat_cr"):
            x, y = (ea.get(fy) or {}).get(field), (eb.get(fy) or {}).get(field)
            if x is None and y is None:
                continue
            add(f"{fy}.{field}", x, y)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--n", type=int, default=3, help="reports per ticker (latest per broker)")
    ap.add_argument("--a", default="gemini-3.8-flash:LOW", help="baseline model:THINKING")
    ap.add_argument("--b", default="gemini-3.5-flash-lite:MINIMAL", help="candidate model:THINKING")
    ap.add_argument("--out", default=None, help="markdown report path")
    args = ap.parse_args()

    from analyst_reports.trendlyne_live import fetch_trendlyne_reports_live
    from analyst_reports.trendlyne_auth import download_pdf_sync
    from analyst_reports.trendlyne_fetcher import TRENDLYNE_DATA
    from agents.analyst_extract import extract_pdf_report
    from agents.analyst_agent import _latest_per_broker

    cfg_a = args.a.split(":")
    cfg_b = args.b.split(":")

    jobs = []
    for t in args.tickers:
        reports = fetch_trendlyne_reports_live(t.upper(), max_pages=2)
        latest, _ = _latest_per_broker(reports)
        name = (TRENDLYNE_DATA.get(t.upper()) or {}).get("stock_name", t)
        for r in [x for x in latest if x.get("pdf_url")][: args.n]:
            jobs.append((t.upper(), name, r))
    _out(f"Comparing {args.a}  vs  {args.b}  on {len(jobs)} reports\n")

    def run(job, cfg):
        t, name, r = job
        pdf = download_pdf_sync(r["pdf_url"])
        usage = {}
        t0 = time.time()
        data = extract_pdf_report(pdf, r["pdf_url"], r["brokerage"], t, name, r.get("date", ""),
                                  model=cfg[0], thinking_level=cfg[1] if len(cfg) > 1 else None,
                                  save_cache=False, usage_out=usage)
        usage["seconds"] = round(time.time() - t0, 1)
        return data, usage

    totals = {"a": {"prompt": 0, "output": 0, "thinking": 0, "seconds": 0},
              "b": {"prompt": 0, "output": 0, "thinking": 0, "seconds": 0}}
    agree = total = 0
    md = [f"# Extraction model comparison ({datetime.date.today()})\n",
          f"A = `{args.a}`, B = `{args.b}`\n"]

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(lambda j: (run(j, cfg_a), run(j, cfg_b)), j): j for j in jobs}
        for f in concurrent.futures.as_completed(futs):
            t, name, r = futs[f]
            try:
                (da, ua), (db, ub) = f.result()
            except Exception as e:
                _out(f"[{t} / {r['brokerage']}] FAILED: {e}")
                continue
            for key, u in (("a", ua), ("b", ub)):
                totals[key]["prompt"] += u.get("prompt_tokens", 0)
                totals[key]["output"] += u.get("output_tokens", 0)
                totals[key]["thinking"] += u.get("thinking_tokens", 0)
                totals[key]["seconds"] += u.get("seconds", 0)
            rows = compare(da, db)
            n_same = sum(1 for x in rows if x[3])
            agree += n_same
            total += len(rows)
            _out(f"[{t} / {r['brokerage']} / {r.get('date')}] {n_same}/{len(rows)} fields agree "
                 f"(B used {ub.get('model')} {ub.get('thinking_level')})")
            for name_, va, vb, same in rows:
                if not same:
                    _out(f"    DIFF {name_:22} A={va!r:>14}  B={vb!r:>14}")
            md.append(f"\n## {t} - {r['brokerage']} ({r.get('date')})\n")
            md.append(f"{n_same}/{len(rows)} numeric/categorical fields agree. PDF: {r['pdf_url']}\n")
            md.append("| Field | A | B | |\n|---|---|---|---|")
            md.extend(f"| {n} | {va} | {vb} | {'' if s else 'DIFF'} |" for n, va, vb, s in rows)
            md.append(f"\n**Thesis A:** {da.get('thesis')}\n\n**Thesis B:** {db.get('thesis')}\n")
            for q in ("strengths", "weaknesses", "opportunities", "threats"):
                md.append(f"- {q} A: {'; '.join((da.get('swot') or {}).get(q) or [])}")
                md.append(f"- {q} B: {'; '.join((db.get('swot') or {}).get(q) or [])}")

    _out("")
    _out(f"Overall agreement: {agree}/{total} fields ({(agree / total * 100 if total else 0):.0f}%)")
    for key, label in (("a", args.a), ("b", args.b)):
        tt = totals[key]
        _out(f"{label:34} prompt {tt['prompt']:>9,}  output {tt['output']:>8,}  thinking {tt['thinking']:>8,}  "
             f"time {tt['seconds']:.0f}s")
    md.append(f"\n## Totals\n\nAgreement {agree}/{total}.\n\n| Config | Prompt tokens | Output tokens | Thinking tokens | Seconds |\n|---|---|---|---|---|")
    for key, label in (("a", args.a), ("b", args.b)):
        tt = totals[key]
        md.append(f"| {label} | {tt['prompt']:,} | {tt['output']:,} | {tt['thinking']:,} | {tt['seconds']:.0f} |")

    out = args.out or os.path.join("temp_reports", f"extraction_eval_{datetime.date.today()}.md")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    _out(f"\nSide-by-side report: {out}")


if __name__ == "__main__":
    main()
