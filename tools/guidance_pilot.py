"""
guidance_pilot.py - run the Concall Guidance Tracker from the command line.

    python tools/guidance_pilot.py run                       # the pilot set (47 large-cap names)
    python tools/guidance_pilot.py run --tickers TCS,LT,HDFCBANK
    python tools/guidance_pilot.py run --top 500                # top 500 by market cap
    python tools/guidance_pilot.py run --quarter "Sep 2026" --force
    python tools/guidance_pilot.py stats                       # pilot accuracy so far
    python tools/guidance_pilot.py export --out guidance_review.csv

`run` fetches each company's latest transcript from Screener, extracts guidance
with Gemini Flash-Lite (GUIDANCE_EXTRACTION_MODEL, default gemini-3.5-flash-lite),
validates every row against the transcript and stores it in kagger.db. Calls
already extracted are skipped unless --force.

`export` writes every row, with its source quote, page link and validator
flags, plus empty-or-filled review columns, for offline evaluation in a
spreadsheet. Reviews entered in the web UI (Concall Agent > Guidance Tracker)
are included.

Needs GOOGLE_API_KEY in the environment (or the repo's .env).
ASCII-only output: the Windows console encodes with cp1252.
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, '.env'))
except Exception:
    pass


def cmd_run(args):
    from agents import guidance_tracker as g
    if not os.getenv('GOOGLE_API_KEY'):
        print('GOOGLE_API_KEY is not set (environment or .env). Nothing was run.')
        return 2
    tickers = [t for t in (args.tickers or '').replace(' ', ',').split(',') if t]
    if not tickers and args.top:
        tickers, skipped = g.top_companies_by_market_cap(args.top)
        if skipped:
            print(f"Skipped as bad data or duplicates: {', '.join(s['ticker'] for s in skipped[:20])}")
    tickers = tickers or list(g.PILOT_UNIVERSE)
    print(f"Extracting guidance for {len(tickers)} companies with {args.model or g.EXTRACTION_MODEL} "
          f"({args.workers} workers){' - FORCE re-extract' if args.force else ''}")
    results = g.run_batch(tickers, quarter=args.quarter, force=args.force,
                          workers=args.workers, model=args.model)
    print()
    print(f"{'TICKER':<12}{'QUARTER':<10}{'STATUS':<15}{'ROWS':>5}{'OK':>5}{'REVIEW':>7}{'REJ':>5}"
          f"{'NUM-RM':>8}{'NO-QUOTE':>9}{'ANALYST':>8}  NOTE")
    for r in sorted(results, key=lambda x: x['ticker']):
        print(f"{r['ticker']:<12}{r.get('quarter', '') or '':<10}{r['status']:<15}"
              f"{r.get('items', ''):>5}{r.get('verified', ''):>5}{r.get('needs_review', ''):>7}"
              f"{r.get('rejected', ''):>5}{r.get('numbers_removed', ''):>8}"
              f"{r.get('quotes_not_found', ''):>9}{r.get('analyst_rows_blocked', ''):>8}  "
              f"{(r.get('error') or r.get('note') or '')[:70]}")
    ok = sum(1 for r in results if r['status'] == 'ok')
    print(f"\n{ok} extracted, {sum(1 for r in results if r['status'] == 'skipped')} skipped, "
          f"{len(results) - ok - sum(1 for r in results if r['status'] == 'skipped')} not extracted.")
    return 0


def cmd_stats(_args):
    from agents import guidance_tracker as g
    e = g.evaluation_stats()

    def pct(v):
        return '-' if v is None else f"{v * 100:.1f}%"
    print(f"Transcripts: {e['transcripts']}")
    print(f"Validator status: {e['auto_status']}")
    print(f"Validator flags: {e['auto_flags']}")
    print(f"Reviewed rows: {e['reviewed_items']} across {e['reviewed_calls']} calls; verdicts {e['verdicts']}")
    print(f"Precision: {pct(e['precision'])}   Invented-figure rate: {pct(e['invented_rate_numeric'])}   "
          f"Not guidance/wrong speaker: {pct(e['not_guidance_rate'])}")
    print(f"Missed guidance reported: {e['missed_reported']}   Recall estimate: {pct(e['recall_estimate'])}")
    for b, s in sorted(e['by_sector'].items()):
        print(f"  {b:<10} precision {pct(s['precision'])} of {s['reviewed']}")
    print(f"Tokens: {e['tokens']}")
    return 0


def cmd_export(args):
    from agents import guidance_tracker as g
    with open(args.out, 'w', encoding='utf-8-sig', newline='') as fh:
        g.write_export_csv(fh)
    print(f"Wrote {args.out}")
    return 0


def main():
    p = argparse.ArgumentParser(description='Concall Guidance Tracker pilot runner')
    sub = p.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run', help='fetch, extract, validate and store')
    r.add_argument('--tickers', help='comma-separated tickers (default: pilot set)')
    r.add_argument('--top', type=int, help='the top N companies by market cap from the stock master')
    r.add_argument('--quarter', help="e.g. 'Sep 2026' (default: latest with a transcript)")
    r.add_argument('--force', action='store_true', help='re-extract calls already stored')
    r.add_argument('--workers', type=int, default=4)
    r.add_argument('--model', help='override GUIDANCE_EXTRACTION_MODEL')
    sub.add_parser('stats', help='pilot accuracy from reviews')
    e = sub.add_parser('export', help='CSV of every row for offline review')
    e.add_argument('--out', default='guidance_review.csv')
    args = p.parse_args()
    return {'run': cmd_run, 'stats': cmd_stats, 'export': cmd_export}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
