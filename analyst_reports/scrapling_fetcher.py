"""
scrapling_fetcher.py — Trendlyne scraper using Scrapling's StealthyFetcher.

Uses Scrapling's built-in Cloudflare/WAF bypass to fetch analyst reports
from Trendlyne, replacing the blocked httpx/Playwright approach.
Also handles PDF downloads through the stealthy browser session.
"""

import asyncio
import os
import re
import sys
import json
import traceback

# ---------------------------------------------------------------------------
# Cookie helpers (shared with pdf_summarizer_cookies.py)
# ---------------------------------------------------------------------------

def _load_trendlyne_cookies() -> dict:
    """Return Trendlyne cookies from env var or local JSON file."""
    env_cookies = os.getenv("TRENDLYNE_COOKIES")
    if env_cookies:
        try:
            cookies = json.loads(env_cookies)
            print(f"SCRAPLING: Loaded {len(cookies)} cookies from env var", file=sys.stderr)
            return cookies
        except json.JSONDecodeError as e:
            print(f"SCRAPLING: Failed to parse TRENDLYNE_COOKIES env: {e}", file=sys.stderr)

    cookie_file = os.path.join(os.path.dirname(__file__), "trendlyne_cookies.json")
    if os.path.exists(cookie_file):
        with open(cookie_file, "r") as f:
            cookies = json.load(f)
        print(f"SCRAPLING: Loaded {len(cookies)} cookies from file", file=sys.stderr)
        return cookies

    print("SCRAPLING: No Trendlyne cookies found", file=sys.stderr)
    return {}


# ---------------------------------------------------------------------------
# Scrapling-based report page scraper
# ---------------------------------------------------------------------------

def _scrape_reports_page(url: str, ticker: str) -> list[dict]:
    """
    Synchronous function: fetch the Trendlyne research-reports page
    using Scrapling's StealthyFetcher and parse report data.

    Returns a list of report dicts (same schema as trendlyne_fetcher.py).
    """
    try:
        from scrapling.fetchers import StealthyFetcher
    except ImportError as e:
        print(f"SCRAPLING: Error importing scrapling: {e}", file=sys.stderr)
        return []

    cookies = _load_trendlyne_cookies()

    try:
        print(f"SCRAPLING: Fetching {url}", file=sys.stderr)
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            block_images=True,
            hide_canvas=True,
            disable_webgl=True,
            google_search=False,
            extra_headers={
                "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
                "Referer": "https://trendlyne.com/",
            },
            wait_selector=".panel-post",
            timeout=45000,
        )

        html_len = len(page.html_content) if hasattr(page, 'html_content') else 0
        print(f"SCRAPLING: Got page ({html_len} chars)", file=sys.stderr)

        reports = []
        seen_brokerages: set[str] = set()

        panels = page.css(".panel-post")
        if not panels:
            # Try body text length as sanity check
            body_text = page.get_all_text() if hasattr(page, 'get_all_text') else ''
            print(f"SCRAPLING: No .panel-post found. Body text length: {len(body_text)}", file=sys.stderr)
            return []

        for panel in panels:
            if len(reports) >= 8:
                break

            # Broker name
            broker_links = panel.css("a[href*='/broker-recommendation-posts/']")
            if not broker_links:
                continue
            brokerage = broker_links[0].text.strip() if broker_links[0].text else ""
            if not brokerage or brokerage.lower() in seen_brokerages:
                continue

            # Recommendation
            reco_elems = panel.css(".post-reco-type")
            reco_text = reco_elems[0].text.strip() if reco_elems else ""
            reco_upper = reco_text.upper()
            if any(w in reco_upper for w in ("BUY", "ACCUMULATE", "OUTPERFORM")):
                normalized_reco = "Buy"
            elif any(w in reco_upper for w in ("SELL", "REDUCE", "UNDERPERFORM")):
                normalized_reco = "Sell"
            else:
                normalized_reco = "Hold"

            # Target price & upside
            target_price = "N/A"
            upside = "N/A"
            for div in panel.css("div, span"):
                div_text = div.text or ""
                if "Target:" in div_text and "|" in div_text:
                    tm = re.search(r"Target[:\s]*[₹Rs.\s]*([0-9,]+(?:\.[0-9]+)?)", div_text, re.IGNORECASE)
                    if tm:
                        target_price = tm.group(1).replace(",", "")
                    if "|" in div_text:
                        after_pipe = div_text.split("|")[1] if len(div_text.split("|")) > 1 else ""
                        um = re.search(r"([+-]?[0-9]+(?:\.[0-9]+)?)\s*%", after_pipe)
                        if um:
                            upside = f"{um.group(1)}%"
                    break

            # Date
            report_date = "Recent"
            date_elems = panel.css(".post-head-subtext")
            if date_elems:
                date_text = date_elems[0].text or ""
                dm = re.search(
                    r"(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})",
                    date_text,
                    re.IGNORECASE,
                )
                if dm:
                    report_date = dm.group(1)

            # Summary from the right card
            summary = ""
            articles = panel.css("article")
            for article in articles:
                text = article.text or ""
                text = text.strip()
                if len(text) > 100:
                    summary = text
                    break
            if summary:
                summary = re.sub(r"\s*More\.\.\.\s*$", "", summary)
            if not summary:
                summary = f"{brokerage} rates {ticker} as {normalized_reco} with target ₹{target_price}."

            # PDF URL
            pdf_url = None
            pdf_links = panel.css("a[href*='/get-document/report/pdf/']")
            if pdf_links:
                href = pdf_links[0].attrib.get("href", "").strip()
                if href.startswith("/"):
                    pdf_url = f"https://trendlyne.com{href}"
                elif href.startswith("http"):
                    pdf_url = href

            if not pdf_url:
                # Try to construct from post ID
                post_links = panel.css("a[href*='/posts/']")
                if post_links:
                    href = post_links[0].attrib.get("href", "")
                    match = re.search(r"/posts/(\d+)/", href)
                    if match:
                        pdf_url = f"https://trendlyne.com/get-document/report/pdf/{match.group(1)}/"

            seen_brokerages.add(brokerage.lower())
            reports.append({
                "brokerage": brokerage,
                "date": report_date,
                "recommendation": normalized_reco,
                "target_price": target_price,
                "upside": upside,
                "summary": summary,
                "pdf_url": pdf_url,
            })

        print(f"SCRAPLING: Parsed {len(reports)} reports for {ticker}", file=sys.stderr)
        return reports

    except Exception as e:
        print(f"SCRAPLING: Error fetching reports for {ticker}: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return []


def _download_pdf_intercept(pdf_url: str) -> bytes | None:
    """
    Synchronous function: download a PDF from Trendlyne using raw Playwright
    (installed via 'scrapling install') to bypass WAF. Intercepts the AWS S3
    redirect URL and fetches the raw PDF bytes directly with httpx.
    """
    import httpx
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print(f"SCRAPLING: Playwright not available: {e}", file=sys.stderr)
        return None

    cookies = _load_trendlyne_cookies()
    pw_cookies = [{"name": k, "value": v, "domain": ".trendlyne.com", "path": "/"} for k, v in cookies.items()]

    final_pdf_url = None
    try:
        print(f"SCRAPLING: Capturing redirect for {pdf_url}", file=sys.stderr)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            )
            context.add_cookies(pw_cookies)
            page = context.new_page()

            def handle_response(response):
                nonlocal final_pdf_url
                url = response.url
                ct = response.headers.get("content-type", "")
                if response.status in (301, 302, 307) and "trendlyne.com/get-document" in url:
                    final_pdf_url = response.headers.get("location")
                elif "application/pdf" in ct:
                    final_pdf_url = url

            page.on("response", handle_response)

            try:
                page.goto(pdf_url, referer="https://trendlyne.com/", wait_until="commit", timeout=20000)
            except Exception:
                pass  # Navigation may abort on download trigger

            import time
            time.sleep(1.5)  # Wait for redirect chain to complete

            browser.close()

        if final_pdf_url:
            print(f"SCRAPLING: Fetching S3 URL: {final_pdf_url[:60]}...", file=sys.stderr)
            r = httpx.get(final_pdf_url, timeout=30)
            if r.content and r.content[:5] == b"%PDF-":
                print(f"SCRAPLING: PDF downloaded ({len(r.content)} bytes)", file=sys.stderr)
                return r.content

        print("SCRAPLING: Redirect interception failed, falling back to httpx", file=sys.stderr)
        return _download_pdf_httpx(pdf_url, cookies)

    except Exception as e:
        print(f"SCRAPLING: Playwright error: {e}, falling back to httpx", file=sys.stderr)
        return _download_pdf_httpx(pdf_url, cookies)


async def download_pdf_with_scrapling(pdf_url: str) -> bytes | None:
    """Async wrapper: download a PDF via Playwright redirect interception."""
    return await asyncio.to_thread(_download_pdf_intercept, pdf_url)

def _download_pdf_httpx(pdf_url: str, cookies: dict) -> bytes | None:
    """Fallback: download PDF with httpx using Trendlyne cookies."""
    import httpx

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://trendlyne.com/",
        "Accept": "application/pdf,*/*",
    }

    try:
        with httpx.Client(follow_redirects=True, timeout=60.0, cookies=cookies) as client:
            response = client.get(pdf_url, headers=headers)
            response.raise_for_status()

            ct = response.headers.get("content-type", "")
            if "pdf" in ct.lower() or response.content[:5] == b"%PDF-":
                print(f"SCRAPLING: httpx fallback PDF downloaded ({len(response.content)} bytes)", file=sys.stderr)
                return response.content
            else:
                # Might have been redirected to login
                if b"login" in response.content[:2000].lower():
                    print("SCRAPLING: httpx PDF download redirected to login – cookies may be expired", file=sys.stderr)
                else:
                    print(f"SCRAPLING: httpx response content-type: {ct}", file=sys.stderr)
                return None
    except Exception as e:
        print(f"SCRAPLING: httpx PDF fallback failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Async wrappers (used by the agent)
# ---------------------------------------------------------------------------

async def fetch_reports_with_scrapling(ticker: str, post_url: str) -> list[dict]:
    """
    Async wrapper: fetch Trendlyne analyst reports page using Scrapling.
    Runs the synchronous StealthyFetcher in a thread to avoid blocking the event loop.
    """
    return await asyncio.to_thread(_scrape_reports_page, post_url, ticker)


