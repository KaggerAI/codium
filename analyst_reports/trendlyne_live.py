"""
trendlyne_live.py - Live, paginated fetch of a stock's Trendlyne research-report feed.

Used by the Analyst Report Agent. Unlike trendlyne_fetcher.fetch_analyst_reports_async
(which serves the pre-fetched JSON cache first and stops at 8 brokers), this always goes
to the live page, walks up to `max_pages` pages (10 reports each, newest first) and
returns EVERY report in the look-back window - including several from the same broker,
so the caller can see how a broker's rating and target moved between reports.

Transport: trendlyne_auth.request() - curl_cffi with a Chrome TLS fingerprint, direct
first and through RESIDENTIAL_PROXY_URL when the WAF blocks the IP (typical on Azure).
The listing is public, but the session cookies are sent so every run also records
whether the Trendlyne session is still logged in (see trendlyne_auth.get_status()).
"""

import os
import re
import sys
import datetime

from bs4 import BeautifulSoup

from analyst_reports.trendlyne_fetcher import get_post_url


def _log(*parts):
    """stderr log that cannot raise on a non-ASCII Windows console (cp1252)."""
    try:
        print(*parts, file=sys.stderr)
    except UnicodeEncodeError:
        print(*(str(p).encode("ascii", "replace").decode() for p in parts), file=sys.stderr)

_DATE_RE = re.compile(
    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*,?\s+\d{4})",
    re.IGNORECASE,
)


def normalize_rating(text: str) -> str:
    """Collapse a broker's rating vocabulary to Buy / Hold / Sell."""
    t = (text or "").upper()
    if any(w in t for w in ("STRONG SELL", "SELL", "REDUCE", "UNDERPERFORM", "UNDERWEIGHT")):
        return "Sell"
    if any(w in t for w in ("BUY", "ACCUMULATE", "OUTPERFORM", "OVERWEIGHT", "ADD")):
        return "Buy"
    if t.strip():
        return "Hold"
    return ""


def parse_date(text: str) -> datetime.date | None:
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _get(url: str, cookies: dict):
    """Direct-then-proxy GET via trendlyne_auth (shared routing + session tracking)."""
    from analyst_reports import trendlyne_auth as ta
    resp, _via_proxy = ta.request("GET", url, cookies=cookies, context="reports page",
                                  headers={"Referer": "https://trendlyne.com/"}, timeout=30)
    return resp


def _parse_panel(panel) -> dict | None:
    broker_link = panel.find("a", href=lambda x: x and "/broker-recommendation-posts/" in x)
    if not broker_link:
        return None
    brokerage = broker_link.get_text(strip=True)
    if not brokerage:
        return None

    reco_el = panel.find(class_="post-reco-type")
    rating_raw = reco_el.get_text(strip=True).rstrip(":").strip() if reco_el else ""

    target_price = None
    target_note = ""
    for div in panel.find_all(["div", "span"]):
        txt = div.get_text(" ", strip=True)
        if txt.startswith("Target") and ("|" in txt or "Target:" in txt):
            m = re.search(r"Target[:\s]*(?:Rs\.?|INR)?\s*([0-9,]+(?:\.[0-9]+)?)", txt, re.IGNORECASE)
            if m:
                try:
                    target_price = float(m.group(1).replace(",", ""))
                except ValueError:
                    pass
            if "|" in txt:
                target_note = txt.split("|", 1)[1].strip()
            break

    date_el = panel.find(class_="post-head-subtext")
    report_date = parse_date(date_el.get_text(" ", strip=True) if date_el else "")

    title = ""
    title_el = panel.find(class_="card-title")
    if title_el:
        title = title_el.get_text(" ", strip=True)

    summary = ""
    bq = panel.find(class_="card-blockquote")
    if bq:
        art = bq.find("article")
        if art:
            summary = re.sub(r"\s*More\.\.\.\s*$", "", art.get_text(" ", strip=True))

    pdf_url = None
    pdf_link = panel.find("a", href=lambda x: x and "/get-document/report/pdf/" in x)
    if pdf_link:
        href = pdf_link.get("href", "").strip()
        pdf_url = f"https://trendlyne.com{href}" if href.startswith("/") else href

    # Trendlyne's own change badges, e.g. "Anand Rathi upgraded TCS to" / "downgraded share price target"
    changes = []
    for lbl in panel.find_all("label", class_=lambda c: c and "label" in c):
        tip = (lbl.get("title") or "").lower()
        if "target" in tip:
            changes.append("target_up" if "upgrad" in tip else "target_down" if "downgrad" in tip else "")
        elif "upgrad" in tip:
            changes.append("rating_up")
        elif "downgrad" in tip:
            changes.append("rating_down")

    price_el = panel.find(class_="post-current-price")
    price_at_post = None
    if price_el:
        try:
            price_at_post = float(price_el.get_text(strip=True).replace(",", ""))
        except ValueError:
            pass

    return {
        "brokerage": brokerage,
        "date": report_date.strftime("%d %b %Y") if report_date else "",
        "date_iso": report_date.isoformat() if report_date else "",
        "rating_raw": rating_raw,
        "recommendation": normalize_rating(rating_raw) or "Hold",
        "target_price": target_price,
        "target_note": target_note,        # e.g. "Upside: 12%" or "Target met"
        "title": title,
        "summary": summary,
        "pdf_url": pdf_url,
        "trendlyne_changes": [c for c in changes if c],
        "source": "Trendlyne",
    }


def fetch_trendlyne_reports_live(ticker: str, max_pages: int = 3, lookback_days: int = 365) -> list[dict]:
    """
    Every report on the ticker's Trendlyne feed newer than `lookback_days`, newest first.
    Raises on transport failure so the caller can fall back; returns [] for a
    ticker Trendlyne does not cover.
    """
    url = get_post_url(ticker)
    if not url:
        return []

    from analyst_reports import trendlyne_auth as ta

    cutoff = datetime.date.today() - datetime.timedelta(days=lookback_days)
    # The listing itself is public; cookies are sent so each run doubles as a free
    # session-health check (a logged-out page means PDFs will need a refreshed session).
    try:
        cookies = ta.get_cookies()
    except Exception:
        cookies = {}

    reports: list[dict] = []
    base_url = None
    for page in range(1, max_pages + 1):
        # The /post/<T>/<id>/ URL redirects to a slug URL and the redirect drops the
        # query string, so page 2+ must be requested on the resolved slug URL.
        page_url = url if page == 1 else f"{base_url}?page={page}"
        r = _get(page_url, cookies)
        if r.status_code != 200:
            if page == 1:
                raise RuntimeError(f"Trendlyne returned HTTP {r.status_code}")
            break
        if page == 1:
            base_url = str(r.url).split("?")[0]
            if cookies:
                logged_in = ta.page_is_logged_in(r.text)
                if logged_in is False:
                    ta.record_session_expired("reports page", "Report listing rendered logged-out")
                elif logged_in is True:
                    ta.record_session_ok("reports page")

        soup = BeautifulSoup(r.text, "html.parser")
        panels = soup.find_all("div", class_=lambda c: c and "panel-post" in c)
        if not panels:
            if page == 1:
                raise RuntimeError("Trendlyne page had no report panels (blocked or layout change)")
            break

        reached_cutoff = False
        for p in panels:
            rep = _parse_panel(p)
            if not rep:
                continue
            if rep["date_iso"] and datetime.date.fromisoformat(rep["date_iso"]) < cutoff:
                reached_cutoff = True
                continue
            reports.append(rep)
        if reached_cutoff or len(panels) < 10:
            break

    _log(f"TRENDLYNE_LIVE: {len(reports)} reports for {ticker}")
    return reports
