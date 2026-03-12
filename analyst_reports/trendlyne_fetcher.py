"""
trendlyne_fetcher.py - Module for fetching analyst reports from Trendlyne.com

This module scrapes analyst/brokerage reports from Trendlyne's "Research Reports" page
and returns structured data for display in the analysis UI.
"""

import httpx
import asyncio
import pandas as pd
from bs4 import BeautifulSoup
from progress_logger import log_progress
import os
import re

# Load CSV once at module initialization
CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'trendlyne_all_stocks_master.csv')
TRENDLYNE_DATA = {}  # Maps ticker -> {id, stock_name, url}


def load_url_mapping():
    """
    Load ticker → Trendlyne data mapping from CSV.
    CSV columns: Stock Name, Ticker, Trendlyne ID, Research Report URL
    """
    global TRENDLYNE_DATA
    try:
        df = pd.read_csv(CSV_PATH)
        for _, row in df.iterrows():
            ticker = str(row['Ticker']).strip().upper()
            TRENDLYNE_DATA[ticker] = {
                'id': str(row['Trendlyne ID']),
                'stock_name': str(row['Stock Name']).strip(),
                'stock_url': str(row['Research Report URL']).strip()
            }
        print(f"INFO: Loaded {len(TRENDLYNE_DATA)} tickers from trendlyne_all_stocks_master.csv")
    except Exception as e:
        print(f"WARNING: Failed to load URL mapping: {e}")


# Load on module import
load_url_mapping()


def get_post_url(ticker: str) -> str | None:
    """
    Construct the 'post' format URL which has text summaries.
    
    CSV URL format: https://trendlyne.com/research-reports/stock/27/ADANIPORTS/
    Post URL format: https://trendlyne.com/research-reports/post/ADANIPORTS/27/
    """
    data = TRENDLYNE_DATA.get(ticker.upper())
    if not data:
        return None
    
    # Construct post URL: /research-reports/post/{TICKER}/{ID}/
    return f"https://trendlyne.com/research-reports/post/{ticker.upper()}/{data['id']}/"


async def fetch_analyst_reports_async(ticker: str) -> list[dict]:
    """
    Scrapes analyst reports from Trendlyne's 'post' page format.
    Returns up to 5 latest reports from DIFFERENT brokerages.
    
    HTML Structure (from browser inspection):
    - Container: div.panel-post.Ltop.m-b-2.bdr
    - Recommendation: .post-reco-type (e.g., "Buy:", "Hold:")
    - Brokerage: a[href*="broker-recommendation-posts"]
    - Target & Upside: .card-text.tbold.trending-body-text -> "Target: 1770 | Upside : 19.5%"
    - Summary: .card-blockquote article
    - Date: .post-head-subtext span
    """
    url = get_post_url(ticker)
    if not url:
        log_progress(f"No analyst report URL found for {ticker}")
        return []
    
    try:
        log_progress(f"Fetching analyst reports for {ticker}...")
        
        # Realistic Chrome browser headers to avoid 403 blocks from cloud IPs (Azure etc.)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://trendlyne.com/",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
            "Connection": "keep-alive",
        }
        
        MAX_RETRIES = 2
        response = None
        html_content = ""
        
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            # First, hit the homepage to establish cookies (session-based anti-bot check)
            try:
                homepage_resp = await client.get("https://trendlyne.com/", headers=headers, timeout=15.0)
                # Small delay to appear more human-like
                await asyncio.sleep(0.5)
            except Exception:
                pass  # Non-critical, proceed even if homepage fails
            
            # Now fetch the actual reports page with retry logic
            for attempt in range(MAX_RETRIES + 1):
                try:
                    response = await client.get(url, headers=headers)
                    if response.status_code == 403 or response.status_code == 429:
                        print(f"WARN: Got {response.status_code} fetching analyst reports for {ticker} via httpx. Falling back to Playwright...")
                        # If we get blocked, break loop and handle with Playwright
                        response = None
                        break
                    response.raise_for_status()
                    html_content = response.text
                    break
                except httpx.HTTPStatusError as e:
                    if attempt == MAX_RETRIES:
                        # Attempt Playwright fallback on HTTP errors
                        response = None
                        break
                        
        # --- PLAYWRIGHT FALLBACK (ENHANCED with stealth + wait_for_selector) ---
        if not html_content:
            log_progress(f"Using browser fallback for Trendlyne Analyst Reports: {ticker}...")
            from playwright.async_api import async_playwright
            try:
                from playwright_stealth import stealth_async
            except ImportError:
                stealth_async = None
            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    context = await browser.new_context(
                        user_agent=headers["User-Agent"],
                        viewport={'width': 1920, 'height': 1080},
                        java_script_enabled=True,
                    )
                    page = await context.new_page()
                    
                    # Apply stealth to bypass fingerprint-based bot detection
                    if stealth_async:
                        await stealth_async(page)
                    
                    # Hit homepage first to establish cookies/session
                    await page.goto("https://trendlyne.com/", wait_until='domcontentloaded', timeout=30000)
                    await asyncio.sleep(1)
                    
                    # Navigate to the actual reports page
                    await page.goto(url, wait_until='networkidle', timeout=45000)
                    
                    # Wait for dynamically-loaded report panels (loaded via JS/AJAX after page load)
                    try:
                        await page.wait_for_selector('.panel-post', timeout=10000)
                        print(f"DEBUG: Playwright found .panel-post elements for {ticker}")
                    except Exception:
                        # If no panels found after 10s, wait a bit more then grab whatever we have
                        print(f"DEBUG: No .panel-post found after 10s for {ticker}, waiting 3s more...")
                        await asyncio.sleep(3)
                    
                    html_content = await page.content()
                    print(f"DEBUG: Playwright fetched {len(html_content)} chars for {ticker}")
                    await browser.close()
            except Exception as pw_err:
                print(f"ERROR: Playwright fallback failed for {ticker}: {pw_err}")
                return []
        
        soup = BeautifulSoup(html_content, 'html.parser')
        
        reports = []
        seen_brokerages = set()
        
        # Find all report panels using the correct container class
        report_panels = soup.find_all('div', class_=lambda c: c and 'panel-post' in c)
        
        for panel in report_panels:
            if len(reports) >= 8:
                break
            
            # Try to find broker link
            broker_link = panel.find('a', href=lambda x: x and '/broker-recommendation-posts/' in x)
            if not broker_link:
                continue
            
            brokerage = broker_link.get_text(strip=True)
            
            # Skip if we already have this brokerage
            if brokerage.lower() in seen_brokerages:
                continue
            
            # Extract recommendation from .post-reco-type
            reco_elem = panel.find(class_='post-reco-type')
            recommendation_text = reco_elem.get_text(strip=True) if reco_elem else ''
            
            # Normalize recommendation
            reco_upper = recommendation_text.upper()
            if 'BUY' in reco_upper or 'ACCUMULATE' in reco_upper or 'OUTPERFORM' in reco_upper:
                normalized_reco = 'Buy'
            elif 'SELL' in reco_upper or 'REDUCE' in reco_upper or 'UNDERPERFORM' in reco_upper:
                normalized_reco = 'Sell'
            else:
                normalized_reco = 'Hold'
            
            # Extract Target and Upside from .card-text.tbold.trending-body-text
            # Pattern: "Target: 1770 | Upside : 19.5%"
            target_price = 'N/A'
            upside = 'N/A'
            
            target_upside_elem = panel.find(class_=lambda c: c and 'trending-body-text' in c and 'tbold' in str(panel.get('class', [])))
            if not target_upside_elem:
                # Try alternative: find div containing "Target:" and "|"
                for div in panel.find_all(['div', 'span']):
                    div_text = div.get_text()
                    if 'Target:' in div_text and '|' in div_text:
                        target_upside_elem = div
                        break
            
            if target_upside_elem:
                tu_text = target_upside_elem.get_text(strip=True)
                
                # Extract target price: "Target: 1770" or "Target: 1,770"
                target_match = re.search(r'Target[:\s]*[₹Rs.\s]*([0-9,]+(?:\.[0-9]+)?)', tu_text, re.IGNORECASE)
                if target_match:
                    target_price = target_match.group(1).replace(',', '')
                
                # Extract upside: after "|" look for pattern like "Upside : 19.5%" or just percentage
                if '|' in tu_text:
                    after_pipe = tu_text.split('|')[1] if len(tu_text.split('|')) > 1 else ''
                    upside_match = re.search(r'([+-]?[0-9]+(?:\.[0-9]+)?)\s*%', after_pipe)
                    if upside_match:
                        upside = f"{upside_match.group(1)}%"
            
            # Extract date from .post-head-subtext
            date_elem = panel.find(class_='post-head-subtext')
            report_date = 'Recent'
            if date_elem:
                date_text = date_elem.get_text(strip=True)
                date_match = re.search(r'(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4})', date_text, re.IGNORECASE)
                if date_match:
                    report_date = date_match.group(1)
            
            # Extract summary from the RIGHT card
            # Structure: panel has .card-group with two cards:
            #   - Left card: .newstitle-left-broker (metadata)
            #   - Right card: .card.nobdr (summary)
            summary = ''
            
            # Find the card group and get the RIGHT card (second card)
            card_group = panel.find(class_='card-group')
            if card_group:
                cards = card_group.find_all('div', class_='card', recursive=False)
                # The right card is typically the second one
                if len(cards) >= 2:
                    right_card = cards[1]
                    # Find .card-blockquote article in the right card
                    blockquote = right_card.find(class_='card-blockquote')
                    if blockquote:
                        summary_elem = blockquote.find('article')
                        if summary_elem:
                            summary = summary_elem.get_text(separator=' ', strip=True)
            
            # Fallback: try to find any article element with substantial text
            if not summary:
                articles = panel.find_all('article')
                for article in articles:
                    text = article.get_text(separator=' ', strip=True)
                    # Look for the one with the actual summary (longer text)
                    if len(text) > 100 and not text.startswith('BUY') and not text.startswith('SELL'):
                        summary = text
                        break
            
            if summary:
                # Remove "More..." from the end if present
                summary = re.sub(r'\s*More\.\.\.\s*$', '', summary)
            
            if not summary:
                summary = f"{brokerage} rates {ticker} as {normalized_reco} with target ₹{target_price}."
            
            # No character limit - extract full summary from DOM (typically ~600 chars)
            
            # Extract PDF URL
            pdf_url = None
            pdf_link = panel.find('a', href=lambda x: x and '/get-document/report/pdf/' in x)
            if pdf_link:
                href = pdf_link.get('href', '').strip()  # Strip whitespace!
                # Ensure it's an absolute URL
                if href.startswith('/'):
                    pdf_url = f"https://trendlyne.com{href}"
                elif href.startswith('http'):
                    pdf_url = href
            
            
            seen_brokerages.add(brokerage.lower())
            
            # Generate PDF URL as fallback (Trendlyne loads these via JavaScript)
            # The pattern is: https://trendlyne.com/get-document/report/pdf/{id}/
            # We can construct this from the tile's data attributes or links
            if not pdf_url:
                # Try to find document ID from any links in the panel
                doc_link = panel.find('a', href=lambda x: x and '/posts/' in x)
                if doc_link:
                    # Extract post ID from URL like /posts/5397522/...
                    href = doc_link.get('href', '')
                    match = re.search(r'/posts/(\d+)/', href)
                    if match:
                        doc_id = match.group(1)
                        pdf_url = f"https://trendlyne.com/get-document/report/pdf/{doc_id}/"
            
            reports.append({
                'brokerage': brokerage,
                'date': report_date,
                'recommendation': normalized_reco,
                'target_price': target_price,
                'upside': upside,
                'summary': summary,
                'pdf_url': pdf_url
            })
        
        log_progress(f"Found {len(reports)} analyst reports for {ticker}")
        return reports
        
    except httpx.HTTPStatusError as e:
        print(f"ERROR: HTTP error fetching analyst reports for {ticker}: {e}")
        return []
    except Exception as e:
        print(f"ERROR: Failed to fetch analyst reports for {ticker}: {e}")
        import traceback
        traceback.print_exc()
        return []


# Synchronous wrapper for testing
def fetch_analyst_reports(ticker: str) -> list[dict]:
    """Synchronous wrapper for fetch_analyst_reports_async"""
    return asyncio.run(fetch_analyst_reports_async(ticker))
