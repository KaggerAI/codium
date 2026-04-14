"""
screener_fetcher.py - Module for fetching data from Screener.in

This module encapsulates all the functions required to scrape and parse
company information, financial tables, and documents from the Screener.in website.
"""

import requests
import pandas as pd
from bs4 import BeautifulSoup
import pdfplumber
from io import BytesIO
import traceback
import re
import os
import mimetypes
from google import genai
from google.genai import types
from progress_logger import log_progress
import httpx
import asyncio
import pandas_market_calendars as mcal
from playwright.async_api import async_playwright
from difflib import SequenceMatcher  # For fuzzy name matching in peer comparison
import urllib.parse

# import os
# import openai

# --- Gemini API Configuration (New v1.0 SDK) ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
genai_client = None
if GOOGLE_API_KEY:
    try:
        genai_client = genai.Client(api_key=GOOGLE_API_KEY)
        print("INFO: (screener_fetcher) Google GenAI client initialized.")
    except Exception as e:
        print(f"ERROR: (screener_fetcher) Failed to initialize Google GenAI client: {e}")
else:
    print("WARN: GOOGLE_API_KEY environment variable not set. AI-powered PDF presentation analysis will be disabled.")

# --- Constants related to Screener.in ---
BASE_URL = "https://www.screener.in/company/{ticker}/consolidated/"

HEADERS = {"User-Agent": "Mozilla/5.0"}
LABELS = {
    1: "Quarterly Results",
    2: "Annual Results",
    3: "Sales Growth Pattern",
    4: "Profit Growth Pattern",
    5: "Stock Price Growth Pattern",
    6: "ROE Pattern",
    7: "Balance Sheet",
    8: "Cash Flow",
    9: "Financial Ratios",
    10: "Quarterly Shareholding Pattern",
    11: "Annual Shareholding Pattern",
}
PATTERNS = [
    "Sales Growth Pattern",
    "Profit Growth Pattern",
    "Stock Price Growth Pattern",
    "ROE Pattern",
]

# --- Helper and Fetching Functions ---

def scrape_top_ratios(soup: BeautifulSoup) -> dict:
    """
    Scrapes the top ratios section (ul#top-ratios) from a Screener.in page.
    Returns a dict mapping metric labels to their values.
    """
    top_ratios = {}
    try:
        ratios_list = soup.select("#top-ratios li")
        for li in ratios_list:
            name_span = li.select_one(".name")
            value_span = li.select_one(".value .number")
            if not value_span:
                 value_span = li.select_one(".value")
            
            if name_span and value_span:
                name = name_span.get_text(strip=True)
                # Extract only the text, avoiding nested elements if possible
                value = value_span.get_text(strip=True)
                top_ratios[name] = value
    except Exception as e:
        log_progress(f"WARN: Error scraping top ratios: {e}")
    return top_ratios


def clean_df(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if df.columns.nlevels > 1:
        df.columns = ['_'.join(str(c) for c in col).strip() for col in df.columns.values]
    else:
        # Ensure columns are strings before using .str accessor
        # (pd.read_html can return integer column names for some tables)
        df.columns = df.columns.astype(str).str.strip().str.replace("Unnamed: 0", "", regex=False)
    for col in df.select_dtypes(include="object"):
        df[col] = df[col].str.strip().str.replace("+", "", regex=False)
    if df.index.dtype == object:
        df.index = df.index.astype(str).str.strip().str.replace("+", "", regex=False)
    return df

def _extract_latest_quarter_from_soup(soup: BeautifulSoup) -> str:
    """Extract the latest quarterly results column header (e.g. 'Mar 2026') from parsed HTML.
    Returns empty string if not found."""
    try:
        quarters_section = soup.select_one("#quarters")
        if quarters_section:
            table_html = quarters_section.select_one(".data-table")
            if table_html:
                headers = table_html.select("th")
                if headers:
                    import re as _re
                    qp = _re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}$', _re.IGNORECASE)
                    valid = [h.get_text(strip=True) for h in headers if qp.match(h.get_text(strip=True))]
                    if valid:
                        return valid[-1]
    except Exception:
        pass
    return ""


def _compare_quarter_strings(q1: str, q2: str) -> int:
    """Compare two quarter strings like 'Mar 2026' and 'Sep 2025'.
    Returns: 1 if q1 is newer, -1 if q2 is newer, 0 if same or unparseable."""
    try:
        from dateutil import parser as dateparser
        d1 = dateparser.parse(q1)
        d2 = dateparser.parse(q2)
        if d1 and d2:
            if d1 > d2: return 1
            elif d1 < d2: return -1
            else: return 0
    except Exception:
        pass
    return 0


def _inject_schedules_sync(tables: dict, soup: BeautifulSoup, cid: str):
    """Fetches hidden schedule rows from Screener API and injects them into the parsed DataFrames."""
    SECTION_MAP = {
        "quarters": "Quarterly Results",
        "profit-loss": "Annual Results",
        "balance-sheet": "Balance Sheet",
        "cash-flow": "Cash Flow",
        "ratios": "Financial Ratios"
    }
    
    buttons = soup.select("button[onclick*='showSchedule']")
    for btn in buttons:
        try:
            onclick = btn.get('onclick', '')
            m = re.search(r"showSchedule\('([^']+)',\s*'([^']+)'", onclick)
            if not m: continue
            
            parent_name = m.group(1)
            section_id = m.group(2)
            
            table_label = SECTION_MAP.get(section_id)
            if not table_label or table_label not in tables:
                continue
                
            df = tables[table_label]
            if df.empty: continue
            first_col_name = df.columns[0]
            
            api_url = f"https://www.screener.in/api/company/{cid}/schedules/?parent={urllib.parse.quote(parent_name)}&section={section_id}"
            
            resp = requests.get(api_url, headers=HEADERS, timeout=10)
            if resp.status_code != 200: continue
            
            schedule_data = resp.json()
            if not schedule_data: continue
            
            parent_idx_list = df.index[df[first_col_name].astype(str).str.contains(parent_name, regex=False, na=False)].tolist()
            if not parent_idx_list: continue
            
            insert_idx = parent_idx_list[0]
            row_pos = df.index.get_loc(insert_idx)
            
            child_df = pd.DataFrame.from_dict(schedule_data, orient='index')
            if child_df.empty: continue
            child_df.reset_index(inplace=True)
            child_df.rename(columns={'index': first_col_name}, inplace=True)
            child_df[first_col_name] = "CHILD_ROW:" + child_df[first_col_name].astype(str)
            
            for c in df.columns:
                if c not in child_df.columns:
                    child_df[c] = ""
            child_df = child_df[df.columns]
            
            df_top = df.iloc[:row_pos+1]
            df_bottom = df.iloc[row_pos+1:]
            
            tables[table_label] = pd.concat([df_top, child_df, df_bottom], ignore_index=True)
            
        except Exception as e:
            # Silent fail to ensure no scraping breakage
            pass

async def _inject_schedules_async(tables: dict, soup: BeautifulSoup, cid: str):
    """Async version of schedule injector to ensure lightning-fast background jobs."""
    SECTION_MAP = {
        "quarters": "Quarterly Results",
        "profit-loss": "Annual Results",
        "balance-sheet": "Balance Sheet",
        "cash-flow": "Cash Flow",
        "ratios": "Financial Ratios"
    }
    
    tasks = []
    buttons = soup.select("button[onclick*='showSchedule']")
    for btn in buttons:
        onclick = btn.get('onclick', '')
        m = re.search(r"showSchedule\('([^']+)',\s*'([^']+)'", onclick)
        if not m: continue
        
        parent_name = m.group(1)
        section_id = m.group(2)
        
        table_label = SECTION_MAP.get(section_id)
        if not table_label or table_label not in tables:
            continue
            
        tasks.append((parent_name, section_id, table_label))
        
    if not tasks: return

    # Fetch concurrently
    async def fetch_one(parent_name, section_id, table_label):
        try:
            api_url = f"https://www.screener.in/api/company/{cid}/schedules/?parent={urllib.parse.quote(parent_name)}&section={section_id}"
            async with httpx.AsyncClient() as client:
                resp = await client.get(api_url, headers=HEADERS, timeout=10.0)
                if resp.status_code != 200: return None
                return (parent_name, table_label, resp.json())
        except:
            return None
            
    results = await asyncio.gather(*[fetch_one(p, s, t) for p, s, t in tasks], return_exceptions=True)
    
    for res in results:
        if not res or isinstance(res, Exception): continue
        parent_name, table_label, schedule_data = res
        if not schedule_data: continue
        
        try:
            df = tables[table_label]
            if df.empty: continue
            first_col_name = df.columns[0]
            
            parent_idx_list = df.index[df[first_col_name].astype(str).str.contains(parent_name, regex=False, na=False)].tolist()
            if not parent_idx_list: continue
            
            insert_idx = parent_idx_list[0]
            row_pos = df.index.get_loc(insert_idx)
            
            child_df = pd.DataFrame.from_dict(schedule_data, orient='index')
            if child_df.empty: continue
            child_df.reset_index(inplace=True)
            child_df.rename(columns={'index': first_col_name}, inplace=True)
            child_df[first_col_name] = "CHILD_ROW:" + child_df[first_col_name].astype(str)
            
            for c in df.columns:
                if c not in child_df.columns:
                    child_df[c] = ""
            child_df = child_df[df.columns]
            
            df_top = df.iloc[:row_pos+1]
            df_bottom = df.iloc[row_pos+1:]
            
            tables[table_label] = pd.concat([df_top, child_df, df_bottom], ignore_index=True)
        except:
            pass


async def fetch_consolidated_async(ticker: str) -> tuple[dict[str, pd.DataFrame], str, dict, bool]:
    """
    Fetches financial tables using a robust, two-stage hybrid approach.
    
    STAGE 1: FAST FETCH
    - Attempts a fast fetch of the '/consolidated/' page using httpx. This works for
      most companies where data is rendered on the server.

    STAGE 2: ROBUST FALLBACK (if needed)
    - If the consolidated page is empty (like for BDL), it escalates to using Playwright.
    - Playwright loads the main standalone page and executes the JavaScript to render
      the dynamic financial tables.

    PARSING:
    - A new, robust parsing method is used. Instead of relying on table order,
      it finds each table by its specific HTML ID ('#quarters', '#profit-loss'),
      ensuring accuracy.
    """
    is_consolidated = False
    standalone_url = f"https://www.screener.in/company/{ticker}/"
    consolidated_url = standalone_url + "consolidated/"
    
    # --- STAGE 1: FAST FETCH ---
    log_progress(f"Attempting fast fetch for {ticker} (Checking Consolidated vs Standalone)...")
    # Use follow_redirects=True to handle cases like E2E where /consolidated/ might redirect
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(consolidated_url, headers=HEADERS, timeout=30.0)
            response.raise_for_status()
            text = response.text
            final_url = str(response.url)
            
            # 1. URL-based detection: If we wanted /consolidated/ but ended up somewhere else
            if "/consolidated/" in final_url:
                is_consolidated = True
            else:
                is_consolidated = False
                log_progress(f"Redirected from consolidated to standalone for {ticker}")

        except httpx.HTTPStatusError:
            log_progress(f"Consolidated URL not found (404) for {ticker}. Fetching standalone...")
            # Fallback to standalone if consolidated 404s
            try:
                response = await client.get(standalone_url, headers=HEADERS, timeout=30.0)
                response.raise_for_status()
                text = response.text
                is_consolidated = False
            except Exception as e:
                log_progress(f"Error fetching standalone for {ticker}: {e}")
                text = ""
        except Exception as e:
            log_progress(f"Error during fast fetch for {ticker}: {e}")
            text = ""

    soup = BeautifulSoup(text, 'html.parser')

    # 2. Toggle button detection (Most reliable indicator)
    # Toggles confirm if the current page is Consolidated or Standalone.
    # - "View Standalone" button present -> Current view is CONSOLIDATED.
    # - "View Consolidated" button present -> Current view is STANDALONE.
    # - NEITHER present -> Company is Standalone-only.
    links = soup.find_all("a", href=True)
    has_view_standalone = any("View Standalone" in l.get_text() for l in links)
    has_view_consolidated = any("View Consolidated" in l.get_text() for l in links)

    if has_view_standalone:
        is_consolidated = True
    elif has_view_consolidated:
        is_consolidated = False
    else:
        # Neither toggle found. This usually means it's a standalone-only company.
        # We also check the URL for a final sanity check, but toggles take precedence.
        if is_consolidated: # Was set to True if URL matched /consolidated/
             log_progress(f"No toggles found for {ticker} at {final_url}. Defaulting to is_consolidated=False.")
        is_consolidated = False

    # --- FRESHNESS CHECK ---
    # For companies like ICICIAMC (spun off), consolidated exists but has STALE data.
    # We compare the latest quarter header from consolidated vs standalone.
    # If standalone is newer, switch to it. Fully wrapped so failures never break main flow.
    if is_consolidated:
        try:
            consol_latest_q = _extract_latest_quarter_from_soup(soup)
            if consol_latest_q:
                # Quick-fetch standalone page to check its latest quarter
                async with httpx.AsyncClient(follow_redirects=True) as client2:
                    sa_resp = await client2.get(standalone_url, headers=HEADERS, timeout=15.0)
                    if sa_resp.status_code == 200:
                        sa_soup = BeautifulSoup(sa_resp.text, 'html.parser')
                        sa_latest_q = _extract_latest_quarter_from_soup(sa_soup)
                        if sa_latest_q and _compare_quarter_strings(sa_latest_q, consol_latest_q) > 0:
                            log_progress(f"FRESHNESS: Standalone ({sa_latest_q}) is newer than Consolidated ({consol_latest_q}) for {ticker}. Switching to standalone.")
                            text = sa_resp.text
                            soup = sa_soup
                            is_consolidated = False
                            final_url = str(sa_resp.url)
                        else:
                            log_progress(f"FRESHNESS: Consolidated ({consol_latest_q}) is current for {ticker}. Keeping consolidated.")
        except Exception as freshness_err:
            log_progress(f"FRESHNESS: Check failed for {ticker} (non-fatal): {freshness_err}")

    # --- CRITICAL FIX ---
    # If we determined this is a Standalone company (is_consolidated=False), but we are currently
    # holding the HTML from the 'consolidated_url', we MUST re-fetch the 'standalone_url'.
    # Why? Because the consolidated page for standalone-only companies (like E2E) might exist (200 OK)
    # but contain a different table structure (or fewer tables) than the main standalone page.
    # To guarantee we get the correct data tables (Quarterly Results, etc.), we switch to the source.
    if not is_consolidated and "/consolidated/" in final_url:
        log_progress(f"Correcting source: Detected standalone for {ticker} but on consolidated URL. Fetching standalone URL for data...")
        try:
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.get(standalone_url, headers=HEADERS, timeout=30.0)
                response.raise_for_status()
                text = response.text
                final_url = str(response.url)
                soup = BeautifulSoup(text, 'html.parser') # Re-parse with new content
        except Exception as e:
             log_progress(f"Error re-fetching standalone for {ticker}: {e}")
             # Proceed with what we have, better than nothing

    # --- STAGE 2: ROBUST FALLBACK (Browser Fetch) ---
    # If we still don't have the critical quarterly results table, try Playwright as a last resort.
    if not soup.select_one("#quarters .data-table"):
        log_progress(f"Tables not found for {ticker} via fast method. Escalating to browser fetch...")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            try:
                # Use standalone_url as the safest root
                await page.goto(standalone_url, wait_until='networkidle', timeout=45000)
                text = await page.content()
            finally:
                await browser.close()
        # Re-parse the soup object
        soup = BeautifulSoup(text, 'html.parser')
        # Re-check toggle buttons on the final rendered page
        links = soup.find_all("a", href=True)
        is_consolidated = any("/company/" in l.get('href', '') and "/consolidated/" not in l.get('href', '') for l in links if "View Standalone" in l.get_text())

    # --- NEW ROBUST PARSING LOGIC ---
    tables = {}
    description = ""
    top_ratios = scrape_top_ratios(soup)
    
    try:
        about_p = soup.select_one(".company-info .about p")
        if about_p:
            description = about_p.get_text(strip=True)
    except Exception as e:
        log_progress(f"WARN: Could not parse company description. Reason: {e}")

    # This mapping is the key. We find each section by its unique ID.
    SECTIONS_TO_FIND = {
        "Quarterly Results": "#quarters",
        "Annual Results": "#profit-loss",
        "Balance Sheet": "#balance-sheet",
        "Cash Flow": "#cash-flow",
        "Financial Ratios": "#ratios",
    }

    import io

    for label, selector in SECTIONS_TO_FIND.items():
        section_div = soup.select_one(selector)
        if section_div:
            # Find the actual data table within the section
            table_html = section_div.select_one(".data-table")
            if table_html:
                try:
                    df_list = await asyncio.to_thread(pd.read_html, io.StringIO(str(table_html)))
                    if df_list:
                        tables[label] = clean_df(df_list[0])
                except Exception as e:
                    log_progress(f"Could not parse table for '{label}' for {ticker}. Error: {e}")
            else:
                log_progress(f"Found section for '{label}' but no data-table inside for {ticker}.")
        else:
             log_progress(f"Could not find section with selector '{selector}' for {ticker}.")

    # Parse Shareholding Pattern explicitly (can contain Quarterly and Annual)
    shareholding_div = soup.select_one("#shareholding")
    if shareholding_div:
        sh_tables = shareholding_div.select(".data-table")
        if len(sh_tables) > 0:
            try:
                df_list = await asyncio.to_thread(pd.read_html, io.StringIO(str(sh_tables[0])))
                if df_list:
                    tables["Quarterly Shareholding Pattern"] = clean_df(df_list[0])
            except Exception as e:
                log_progress(f"Could not parse Quarterly Shareholding for {ticker}: {e}")
        if len(sh_tables) > 1:
            try:
                df_list = await asyncio.to_thread(pd.read_html, io.StringIO(str(sh_tables[1])))
                if df_list:
                    tables["Annual Shareholding Pattern"] = clean_df(df_list[0])
            except Exception as e:
                log_progress(f"Could not parse Annual Shareholding for {ticker}: {e}")

    # Parse Growth Patterns explicitly from ranges-tables
    ranges_tables = soup.select("table.ranges-table")
    series_list = []
    for table_html in ranges_tables:
        try:
            df_list = await asyncio.to_thread(pd.read_html, io.StringIO(str(table_html)))
            if df_list and len(df_list) > 0:
                df = clean_df(df_list[0])
                if not df.empty and len(df.columns) >= 2:
                    p = df.columns[0]
                    idx, val = df.columns[:2]
                    s = df.set_index(idx)[val]
                    s.name = p
                    series_list.append(s)
        except Exception as e:
            log_progress(f"Could not parse ranges-table for {ticker}: {e}")

    if series_list:
        merged = pd.concat(series_list, axis=1).T
        
        # Reorder columns to be logical: 10Y, 5Y, 3Y, 1Y, Last Year, TTM
        preferred_order = ["10 Years:", "5 Years:", "3 Years:", "1 Year:", "Last Year:", "TTM:"]
        existing_cols = [c for c in preferred_order if c in merged.columns]
        other_cols = [c for c in merged.columns if c not in preferred_order]
        merged = merged[existing_cols + other_cols]
        
        merged.index.name = "Particulars"
        merged = merged.reset_index() # Puts metric names into a column
        merged = merged.fillna("")
        tables["Growth Patterns"] = merged

    # Inject hidden child rows using Screener API
    cid_elem = soup.select_one('[data-company-id]')
    if cid_elem:
        cid = cid_elem.get('data-company-id')
        await _inject_schedules_async(tables, soup, cid)

    return tables, description, top_ratios, is_consolidated

async def fetch_latest_quarter_header_async(ticker: str, consolidated: bool = False) -> str:
    """
    Fast fetch of the latest quarterly result column header (e.g., 'Dec 2023').
    Used for cache validity checking.
    """
    # Consolidated aware URL
    if consolidated:
        target_url = f"https://www.screener.in/company/{ticker}/consolidated/"
    else:
        target_url = f"https://www.screener.in/company/{ticker}/"
        
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            response = await client.get(target_url, headers=HEADERS, timeout=15.0)
            if response.status_code != 200:
                print(f"DEBUG: fetch_latest_quarter failed for {ticker} (Consolidated: {consolidated}) - Status: {response.status_code}")
                return ""
            text = response.text
        except Exception as e:
            print(f"DEBUG: fetch_latest_quarter exception for {ticker}: {e}")
            return ""

    soup = BeautifulSoup(text, 'html.parser')
    quarters_section = soup.select_one("#quarters")
    if quarters_section:
        table_html = quarters_section.select_one(".data-table")
        if table_html:
            try:
                # Use a fast way to get just the headers
                # pd.read_html can be slow, but for a single small table it's fine
                # Alternatively, we can just use BeautifulSoup to find the last th
                headers = table_html.select("th")
                if headers:
                    # Filter out empty headers and get the last one
                    valid_headers = [h.get_text(strip=True) for h in headers if h.get_text(strip=True)]
                    if valid_headers:
                        return valid_headers[-1]
            except Exception as e:
                print(f"DEBUG: Error parsing quarter header for {ticker}: {e}")
    return ""

# async def fetch_consolidated_async(ticker: str) -> tuple[dict[str, pd.DataFrame], str]:
#     """
#     Fetches financial tables and company description from Screener.in.
    
#     This function includes a fallback mechanism. It first tries to fetch the
#     'consolidated' report. If it detects that key financial data (like the
#     quarterly results table) is missing, it automatically fetches the main
#     'standalone' page, ensuring data is retrieved even for companies without
#     a separate consolidated view (e.g., BDL).
#     """
#     standalone_url = f"https://www.screener.in/company/{ticker}/"
#     consolidated_url = standalone_url + "consolidated/"
    
#     async with httpx.AsyncClient() as client:
#         # First, attempt to get the consolidated report
#         log_progress(f"Fetching consolidated report for {ticker}...")
#         response = await client.get(consolidated_url, headers=HEADERS, timeout=30.0)
#         response.raise_for_status()
#         text = response.text

#     soup = BeautifulSoup(text, 'html.parser')

#     # Check if the main "quarters" table section is missing. This is our indicator
#     # that the consolidated page is empty and we need the standalone page.
#     if not soup.select_one("#quarters"):
#         log_progress(f"Consolidated view for {ticker} is incomplete. Fetching standalone data as a fallback.")
#         async with httpx.AsyncClient() as client:
#             # If missing, make a second request to the standalone URL
#             response = await client.get(standalone_url, headers=HEADERS, timeout=30.0)
#             response.raise_for_status()
#             text = response.text
#             # We must re-parse the soup object with the new page content
#             soup = BeautifulSoup(text, 'html.parser')

#     # The rest of the function proceeds with the 'text' that is guaranteed
#     # to have the financial tables.
#     description = ""
#     try:
#         about_p = soup.select_one(".company-info .about p")
#         if about_p:
#             description = about_p.get_text(strip=True)
#     except Exception as e:
#         log_progress(f"WARN: Could not parse company description for {ticker}. Reason: {e}")

#     # pd.read_html is a blocking (non-async) function, so we run it in a separate thread.
#     raw_tables = await asyncio.to_thread(pd.read_html, text)
    
#     tables = {LABELS.get(i): clean_df(df) for i, df in enumerate(raw_tables, start=1) if LABELS.get(i)}
#     series_list = []
#     for p in PATTERNS:
#         df = tables.pop(p, None)
#         if df is not None and len(df.columns) >= 2:
#             idx, val = df.columns[:2]
#             s = df.set_index(idx)[val]
#             s.name = p
#             series_list.append(s)
#     if series_list:
#         merged = pd.concat(series_list, axis=1).T
#         merged.index.name = ""
#         tables["Growth Patterns"] = merged
#     return tables, description

def fetch_consolidated(ticker: str) -> tuple[dict[str, pd.DataFrame], str, dict, bool, dict]:
    is_consolidated = False
    standalone_url = f"https://www.screener.in/company/{ticker}/"
    consolidated_url = standalone_url + "consolidated/"
    
    log_progress(f"Attempting sync fetch for {ticker} (Checking Consolidated vs Standalone)...")
    try:
        response = requests.get(consolidated_url, headers=HEADERS, allow_redirects=True, timeout=30.0)
        response.raise_for_status()
        text = response.text
        final_url = response.url
        
        if "/consolidated/" in final_url:
            is_consolidated = True
        else:
            is_consolidated = False
    except Exception as e:
        log_progress(f"Error fetching consolidated URL for {ticker}: {e}. Trying standalone...")
        try:
            response = requests.get(standalone_url, headers=HEADERS, timeout=30.0)
            response.raise_for_status()
            text = response.text
            is_consolidated = False
        except Exception as e2:
            log_progress(f"Error fetching standalone for {ticker}: {e2}")
            raise e2

    soup = BeautifulSoup(text, 'html.parser')
    
    # Toggle button detection
    links = soup.find_all("a", href=True)
    has_view_standalone = any("View Standalone" in l.get_text() for l in links)
    has_view_consolidated = any("View Consolidated" in l.get_text() for l in links)

    if has_view_standalone:
        is_consolidated = True
    elif has_view_consolidated:
        is_consolidated = False
    else:
        # Standalone-only company (like E2E)
        is_consolidated = False

    # --- FRESHNESS CHECK (Sync) ---
    if is_consolidated:
        try:
            consol_latest_q = _extract_latest_quarter_from_soup(soup)
            if consol_latest_q:
                sa_resp = requests.get(standalone_url, headers=HEADERS, timeout=15.0)
                if sa_resp.status_code == 200:
                    sa_soup = BeautifulSoup(sa_resp.text, 'html.parser')
                    sa_latest_q = _extract_latest_quarter_from_soup(sa_soup)
                    if sa_latest_q and _compare_quarter_strings(sa_latest_q, consol_latest_q) > 0:
                        log_progress(f"FRESHNESS: Standalone ({sa_latest_q}) is newer than Consolidated ({consol_latest_q}) for {ticker}. Switching to standalone.")
                        text = sa_resp.text
                        soup = sa_soup
                        is_consolidated = False
                        final_url = str(sa_resp.url)
                    else:
                        log_progress(f"FRESHNESS: Consolidated ({consol_latest_q}) is current for {ticker}. Keeping consolidated.")
        except Exception as freshness_err:
            log_progress(f"FRESHNESS: Check failed for {ticker} (non-fatal): {freshness_err}")

    # --- CRITICAL FIX (Sync) ---
    if not is_consolidated and "/consolidated/" in final_url:
        log_progress(f"Correcting source (sync): Detected standalone for {ticker} but on consolidated URL. Fetching standalone URL...")
        try:
            # Re-fetch using standalone URL to ensure we get the full tables
            response = requests.get(standalone_url, headers=HEADERS, timeout=30.0)
            response.raise_for_status()
            text = response.text
            # Re-parse soup
            soup = BeautifulSoup(text, 'html.parser')
        except Exception as e:
             log_progress(f"Error re-fetching standalone for {ticker}: {e}")
             # Proceed with what we have

    # --- Description scraping ---
    description = ""
    try:
        soup = BeautifulSoup(text, 'html.parser')
        # This is a more specific and reliable selector for the description paragraph.
        about_p = soup.select_one(".company-info .about p")
        if about_p:
            description = about_p.get_text(strip=True)
            log_progress(f"Successfully fetched company description for {ticker}.")
        else:
            log_progress(f"WARN: Could not find the description paragraph for {ticker}.")
    except Exception as e:
        log_progress(f"WARN: Could not parse company description for {ticker}. Reason: {e}")

    # --- Top Ratios Scraping ---
    top_ratios = scrape_top_ratios(soup)

    # --- Main financial tables ---
    import io
    raw = pd.read_html(io.StringIO(text))
    tables = {LABELS.get(i): clean_df(df) for i, df in enumerate(raw, start=1) if LABELS.get(i)}

    shareholding_div = soup.select_one("#shareholding")
    if shareholding_div:
        sh_tables = shareholding_div.select(".data-table")
        if len(sh_tables) > 0:
            try:
                df_list = pd.read_html(io.StringIO(str(sh_tables[0])))
                if df_list:
                    tables["Quarterly Shareholding Pattern"] = clean_df(df_list[0])
            except Exception as e:
                log_progress(f"Could not parse Quarterly Shareholding for {ticker}: {e}")
        if len(sh_tables) > 1:
            try:
                df_list = pd.read_html(io.StringIO(str(sh_tables[1])))
                if df_list:
                    tables["Annual Shareholding Pattern"] = clean_df(df_list[0])
            except Exception as e:
                log_progress(f"Could not parse Annual Shareholding for {ticker}: {e}")

    # Parse Growth Patterns explicitly from ranges-tables
    ranges_tables = soup.select("table.ranges-table")
    series_list = []
    for table_html in ranges_tables:
        try:
            df_list = pd.read_html(io.StringIO(str(table_html)))
            if df_list and len(df_list) > 0:
                df = clean_df(df_list[0])
                if not df.empty and len(df.columns) >= 2:
                    p = df.columns[0]
                    idx, val = df.columns[:2]
                    s = df.set_index(idx)[val]
                    s.name = p
                    series_list.append(s)
        except Exception as e:
            log_progress(f"Could not parse ranges-table for {ticker}: {e}")

    if series_list:
        merged = pd.concat(series_list, axis=1).T
        
        # Reorder columns to be logical: 10Y, 5Y, 3Y, 1Y, Last Year, TTM
        preferred_order = ["10 Years:", "5 Years:", "3 Years:", "1 Year:", "Last Year:", "TTM:"]
        existing_cols = [c for c in preferred_order if c in merged.columns]
        other_cols = [c for c in merged.columns if c not in preferred_order]
        merged = merged[existing_cols + other_cols]
        
        merged.index.name = "Particulars"
        merged = merged.reset_index() # Puts metric names into a column
        merged = merged.fillna("")
        tables["Growth Patterns"] = merged

    # Inject hidden child rows using Screener API
    cid_elem = soup.select_one('[data-company-id]')
    if cid_elem:
        cid = cid_elem.get('data-company-id')
        _inject_schedules_sync(tables, soup, cid)

    # --- Peer comparison extraction (free — reuses the already-downloaded soup) ---
    peer_data = {'company': {}, 'peers': []}
    try:
        peer_data = _parse_peer_table(soup, ticker)
        if peer_data and peer_data.get('peers'):
            log_progress(f"Extracted {len(peer_data['peers'])} peers for {ticker} from page (no extra request).")
    except Exception as e:
        log_progress(f"WARN: Peer extraction failed for {ticker}: {e}")

    return tables, description, top_ratios, is_consolidated, peer_data

async def get_company_id_async(ticker: str) -> int:
    url = "https://www.screener.in/api/company/search/"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={"q": ticker}, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
        
        if not data and '-' in ticker:
            resp = await client.get(url, params={"q": ticker.replace('-', ' ')}, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                resp = await client.get(url, params={"q": ticker.replace('-', '')}, timeout=30.0)
                resp.raise_for_status()
                data = resp.json()
                
    if not data: raise ValueError(f"No company found for ticker '{ticker}'")
    return data[0]["id"]

def get_company_id(ticker: str) -> int:
    url = "https://www.screener.in/api/company/search/"
    resp = requests.get(url, params={"q": ticker})
    resp.raise_for_status()
    data = resp.json()
    
    if not data and '-' in ticker:
        resp = requests.get(url, params={"q": ticker.replace('-', ' ')})
        resp.raise_for_status()
        data = resp.json()
        if not data:
            resp = requests.get(url, params={"q": ticker.replace('-', '')})
            resp.raise_for_status()
            data = resp.json()
            
    if not data:
        raise ValueError(f"No company found for ticker '{ticker}'")
    return data[0]["id"]

async def fetch_chart_data_async(client: httpx.AsyncClient, company_id: int, query: str, days: int = 10000, consolidated: bool = False) -> dict:
    url = f"https://www.screener.in/api/company/{company_id}/chart/"
    params = {"q": query, "days": days}
    if consolidated:
        params["consolidated"] = 1
    resp = await client.get(url, params=params, timeout=30.0)
    resp.raise_for_status()
    return resp.json()

def fetch_chart_data(company_id: int, query: str, days: int = 10000, consolidated: bool = False) -> dict:
    url = f"https://www.screener.in/api/company/{company_id}/chart/"
    params = {"q": query, "days": days}
    if consolidated:
        params["consolidated"] = 1
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()

# def parse_chart_json(chart_json: dict) -> pd.DataFrame:
#     datasets = chart_json.get("datasets", [])
#     dfs = []
#     for ds in datasets:
#         label = ds.get("label") or ds.get("metric")
#         values = ds.get("values", [])
#         df = pd.DataFrame(values, columns=["date", label])
#         df["date"] = pd.to_datetime(df["date"])
#         df.set_index("date", inplace=True)
#         df[label] = pd.to_numeric(df[label], errors="coerce")
#         dfs.append(df)
#     if not dfs:
#         return pd.DataFrame()
#     return pd.concat(dfs, axis=1)

def parse_chart_json(chart_json: dict) -> pd.DataFrame:
    datasets = chart_json.get("datasets", [])
    dfs = []
    for ds in datasets:
        label = ds.get("label") or ds.get("metric")
        values = ds.get("values", [])
        df = pd.DataFrame(values, columns=["date", label])
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
        df[label] = pd.to_numeric(df[label], errors="coerce")
        dfs.append(df)

    if not dfs:
        return pd.DataFrame()

    combined_df = pd.concat(dfs, axis=1)

    if combined_df.empty:
        return pd.DataFrame()

    # --- START: FINAL CORRECTED LOGIC ---

    # 1. Normalize the data's index to remove time part
    combined_df.index = combined_df.index.normalize()

    # 2. Get the BSE market calendar
    bse = mcal.get_calendar('BSE')
    start_date = combined_df.index.min()
    end_date = combined_df.index.max()

    if pd.isna(start_date) or pd.isna(end_date):
        # This handles cases where a stock might have no valuation data at all
        return pd.DataFrame()

    # 3. Get the valid trading days from the calendar
    valid_trading_days = bse.valid_days(start_date=start_date, end_date=end_date)

    # 4. THE CRITICAL FIX: Remove the timezone information from the calendar's index
    #    to make it compatible with our data's timezone-naive index.
    valid_trading_days = valid_trading_days.tz_localize(None)

    # 5. Re-index the DataFrame. This will now work correctly.
    business_days_df = combined_df.reindex(valid_trading_days)

    # 6. Interpolate to fill the gaps.
    interpolated_df = business_days_df.interpolate(method='linear', limit_direction='both')

    return interpolated_df

async def get_text_from_pdf_url_async(pdf_url: str, max_pages_to_process=75, max_chars_to_return=20000) -> str:
    """
    Downloads a PDF and extracts text. Uses pdfplumber first, then falls back to
    Gemini Vision for scanned PDFs (images instead of selectable text).
    """
    MIN_TEXT_THRESHOLD = 500  # If less than this, assume scanned PDF
    
    try:
        # NOTE: verify=False for corporate IR sites with SSL cert issues
        async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
            response = await client.get(pdf_url, headers=HEADERS, timeout=45.0)
            response.raise_for_status()
        
        pdf_content = response.content
        
        # --- FIX: Check if it's actually HTML (CRISIL often returns HTML pages for ratings) ---
        # 1. Check Content-Type header
        content_type = response.headers.get('content-type', '').lower()
        # 2. Check Magic Bytes (%PDF)
        is_pdf_signature = pdf_content.strip().startswith(b'%PDF')
        
        if 'html' in content_type or not is_pdf_signature:
            # It's likely an HTML page, not a PDF
            print(f"INFO: URL {pdf_url} appears to be HTML (Type: {content_type}). Parsing with BeautifulSoup...")
            try:
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # --- SPECIAL CASE: ICRA Wrapper Page ---
                # ICRA often embeds the PDF in an iframe with id="iframeRationaleReport"
                # or has a script for DownloadRatingReport
                icra_iframe = soup.find('iframe', id='iframeRationaleReport')
                if icra_iframe:
                    # src example: /web/viewer.html?file=/Rating/ShowRationalReportFilePdf/139291
                    # We want to extract the ID: 139291
                    src = icra_iframe.get('src', '')
                    import re
                    match = re.search(r'ShowRationalReportFilePdf/(\d+)', src)
                    if match:
                        report_id = match.group(1)
                        # Construct the direct download URL which is usually more reliable
                        # https://www.icra.in/Rating/GetRationalReportFilePdf?Id=139291
                        direct_pdf_url = f"https://www.icra.in/Rating/GetRationalReportFilePdf?Id={report_id}"
                        print(f"INFO: Detected ICRA Wrapper. Redirecting to real PDF: {direct_pdf_url}")
                        # Recursively fetch the real PDF
                        return await get_text_from_pdf_url_async(direct_pdf_url)

                # Extract text using space separator
                text = soup.get_text(separator=' ', strip=True)
                
                # --- CLEANING: Remove excessive whitespace ---
                # HTML often results in many multiple spaces/newlines
                import re
                text = re.sub(r'\s+', ' ', text).strip()
                
                return text[:max_chars_to_return]
            except Exception as e:
                print(f"WARN: HTML parsing failed for {pdf_url}: {e}")
                # Fallthrough to try PDF parsing just in case, or return error
        
        pdf_file = BytesIO(pdf_content)

        def blocking_pdf_extraction():
            all_text = []
            with pdfplumber.open(pdf_file) as pdf:
                search_limit = min(len(pdf.pages), 10)
                start_page = 2 # Default
                for i in range(search_limit):
                    page_text = pdf.pages[i].extract_text() or ""
                    if "moderator" in page_text.lower():
                        start_page = i
                        break
                end_page = min(len(pdf.pages), start_page + max_pages_to_process)
                for i in range(start_page, end_page):
                    page = pdf.pages[i]
                    text = page.extract_text()
                    if text: all_text.append(text)
            full_summary = "\n".join(all_text)
            return full_summary[:max_chars_to_return]

        extracted_text = await asyncio.to_thread(blocking_pdf_extraction)
        
        # Check if we got enough text - if not, it's likely a scanned PDF
        if extracted_text and len(extracted_text.strip()) >= MIN_TEXT_THRESHOLD:
            return extracted_text
        
        # =====================================================================
        # FALLBACK: Use Gemini Vision for OCR on scanned PDFs
        # =====================================================================
        print(f"INFO: pdfplumber extracted only {len(extracted_text.strip()) if extracted_text else 0} chars. Falling back to Gemini OCR for {pdf_url}...")
        
        if not GOOGLE_API_KEY:
            print("WARN: Gemini OCR fallback unavailable - no Google API Key configured")
            return extracted_text  # Return whatever we got
        
        gemini_text = await _extract_concall_with_gemini_async(pdf_content, pdf_url)
        if gemini_text and not gemini_text.startswith("Error:"):
            return gemini_text[:max_chars_to_return]
        
        # If Gemini also failed, return the original pdfplumber result
        return extracted_text
        
    except Exception as e:
        print(f"ERROR (async): Failed to get text from PDF URL {pdf_url}. Reason: {e}")
        return None


async def _extract_concall_with_gemini_async(pdf_content: bytes, pdf_url: str) -> str:
    """
    Uses Gemini Vision to OCR a scanned PDF concall transcript.
    This is a fallback when pdfplumber can't extract text (image-based PDF).
    """
    try:
        def blocking_gemini_ocr(content):
            global genai_client
            if not genai_client:
                raise ValueError("GenAI client not initialized")
                
            log_progress("Using Gemini Vision to OCR scanned concall transcript...")
            
            # Use the new SDK file upload
            pdf_file = genai_client.files.upload(
                file=BytesIO(content),
                config=types.UploadFileConfig(
                    display_name=pdf_url.split('/')[-1],
                    mime_type='application/pdf'
                )
            )
            print(f"PDF uploaded for OCR as '{pdf_file.name}'.")

            prompt = """You are an OCR and transcription specialist. This PDF contains a scanned earnings call transcript that cannot be read by standard text extraction tools because it's image-based.

Your task:
1. OCR all the text visible in this document
2. Extract the full conversation between the moderator, management, and analysts
3. Focus on the Question & Answer session if present
4. Preserve the speaker attributions (who said what)
5. Output the transcript as plain text, maintaining the natural conversation flow

DO NOT summarize or analyze - just extract the raw text/transcript content as accurately as possible.
If there are multiple pages, extract text from all of them.
Return ONLY the extracted transcript text, nothing else."""

            # New SDK generation syntax
            response = genai_client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=[
                    types.Part.from_text(text=prompt),
                    pdf_file
                ]
            )
            
            # Clean up uploaded file
            genai_client.files.delete(name=pdf_file.name)
            print(f"Cleaned up OCR file {pdf_file.name}.")
            
            return response.text

        return await asyncio.to_thread(blocking_gemini_ocr, pdf_content)
        
    except Exception as e:
        print(f"ERROR: Gemini OCR fallback failed for {pdf_url}: {e}")
        traceback.print_exc()
        return f"Error: Gemini OCR failed - {e}"



def get_text_from_pdf_url(pdf_url: str, max_pages_to_process=75, max_chars_to_return=20000) -> str:
    """
    Downloads a PDF, finds the start of the 'Question and Answer' session,
    and extracts text from that point onwards for a few pages.
    """
    try:
        response = requests.get(pdf_url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        
        pdf_file = BytesIO(response.content)
        
        all_text = []
        start_page = 0
        found_start_keyword = False

        with pdfplumber.open(pdf_file) as pdf:
            search_limit = min(len(pdf.pages), 10)
            for i in range(search_limit):
                page_text = pdf.pages[i].extract_text() or ""
                if "moderator" in page_text.lower():
                    log_progress(f"Analyzing Concall Transcript ...")
                    start_page = i
                    found_start_keyword = True
                    break
            
            if not found_start_keyword:
                print("WARN: 'Question and Answer' keyword not found. Defaulting to start extraction from page 3.")
                start_page = 2

            end_page = min(len(pdf.pages), start_page + max_pages_to_process)
            for i in range(start_page, end_page):
                page = pdf.pages[i]
                text = page.extract_text()
                if text:
                    all_text.append(text)
        
        full_summary = "\n".join(all_text)
        return full_summary[:max_chars_to_return]

    except Exception as e:
        print(f"ERROR: Failed to get text from PDF URL {pdf_url}. Reason: {e}")
        return None

async def summarize_presentation_with_gemini_async(pdf_url: str) -> str:
    """
    CORRECTED: This version isolates the entire Gemini SDK interaction into a 
    separate thread to prevent event loop conflicts with its underlying gRPC library.
    """
    if not GOOGLE_API_KEY:
        return "Error: Google API Key is not configured."

    try:
        # Step 1: Download the PDF content asynchronously (this part is fast and safe)
        # NOTE: verify=False bypasses SSL cert verification for corporate IR websites
        # (e.g., pfcindia.co.in) that may have cert chain issues on Windows
        async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
            print(f"Downloading presentation from {pdf_url} for Gemini analysis...")
            response = await client.get(pdf_url, headers=HEADERS, timeout=60.0)
            response.raise_for_status()
            pdf_content = response.content

        # Step 2: Define a synchronous function that handles ALL Gemini operations.
        def blocking_gemini_tasks(content):
            global genai_client
            if not genai_client:
                raise ValueError("GenAI client not initialized")
                
            log_progress("Uploading PDF to Google AI File Service...")
            mime_type = mimetypes.guess_type(pdf_url)[0] or 'application/pdf'

            # New SDK file upload
            pdf_file = genai_client.files.upload(
                file=BytesIO(content),
                config=types.UploadFileConfig(
                    display_name=pdf_url.split('/')[-1],
                    mime_type=mime_type
                )
            )

            print(f"PDF uploaded successfully as '{pdf_file.name}'.")

            prompt = """You are an expert financial data extractor AI. Your task is to perform a forensic-level analysis of the entire provided investor presentation PDF. Leave no stone unturned. Analyze every page of the document.

            Your output must be an extremely detailed and well-structured summary.

            **Instructions:**
            
            1.  **Extract All Quantitative Data:**
                -   Go through every chart, table, and text block. Extract all key financial metrics (Revenue, EBITDA, PAT, Margins, etc.) and operational KPIs (volumes, utilization, order book, etc.).
                -   Present this data in **Markdown tables** for clarity.
                -   Always include the period (e.g., Q3 FY24, FY24) and any YoY or QoQ growth figures mentioned.

            2.  **Detailed Revenue & Profit Breakdowns:**
                -   Create separate sections for revenue and profit breakdowns.
                -   Detail the contribution from every business segment, geographical region, or product line mentioned. Include absolute values and percentages if available.
                -   If this information is not present, you must explicitly state: "A detailed revenue/profit breakdown was not provided."

            3.  **Management Commentary & Outlook:**
                -   Extract **verbatim quotes** or detailed summaries of management's commentary on performance, strategy, and future outlook.
                -   List all stated future guidance, capex plans, new projects, and strategic initiatives.
                -   Summarize any discussion of industry trends, risks, headwinds, or competitive landscape.

            4.  **Source Everything:**
                -   If possible, reference the page number from the PDF where the information was found (e.g., "Source: Page 5").

            Do not summarize aggressively. The goal is a comprehensive, data-rich extraction of all relevant information from the document."""

            log_progress("Calling Gemini 3 Flash to extract insights from Investor Presentation...")
            
            # New SDK generation syntax
            response = genai_client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=[
                    types.Part.from_text(text=prompt),
                    pdf_file
                ]
            )

            # Clean up uploaded file
            genai_client.files.delete(name=pdf_file.name)
            print(f"Cleaned up uploaded file {pdf_file.name}.")
            
            return response.text

        # Step 3: Run the entire synchronous Gemini block in a separate thread.
        return await asyncio.to_thread(blocking_gemini_tasks, pdf_content)

    except Exception as e:
        # This will catch errors from both the httpx download and the Gemini processing
        print(f"ERROR (async): Failed to generate summary with Gemini for PDF {pdf_url}. Reason: {e}")
        traceback.print_exc() # Print full traceback for better debugging
        return f"Error: AI model failed to analyze presentation. Reason: {e}"

def summarize_presentation_with_gemini(pdf_url: str) -> str:
    """
    Downloads a PDF presentation, sends it to the Gemini 1.5 Flash model for
    analysis, and returns a detailed, structured textual summary of its contents.

    Args:
        pdf_url (str): The URL of the PDF to analyze.

    Returns:
        str: A detailed summary generated by the AI, or an error message.
    """
    if not GOOGLE_API_KEY:
        return "Error: Google API Key is not configured. Cannot summarize presentation."

    try:
        print(f"Downloading presentation from {pdf_url} for Gemini analysis...")
        response = requests.get(pdf_url, headers=HEADERS, timeout=30)
        response.raise_for_status()
        pdf_content = response.content
        
        log_progress("Uploading PDF to Google AI File Service...")
        mime_type = mimetypes.guess_type(pdf_url)[0] or 'application/pdf'

        # --- FIX: Using the new google-genai SDK syntax (synchronous) ---
        global genai_client
        if not genai_client:
             genai_client = genai.Client(api_key=GOOGLE_API_KEY)

        pdf_file = genai_client.files.upload(
            file=BytesIO(pdf_content),
            config=types.UploadFileConfig(
                display_name=pdf_url.split('/')[-1],
                mime_type=mime_type
            )
        )

        print(f"PDF uploaded successfully as '{pdf_file.name}'.")

        # Prompt remains the same
        prompt = """
        You are an expert financial data extractor AI. Your task is to perform a forensic-level analysis of the entire provided investor presentation PDF. Leave no stone unturned. Analyze every page of the document.

        Your output must be an extremely detailed and well-structured summary.

        **Instructions:**

        1.  **Extract All Quantitative Data:**
            -   Go through every chart, table, and text block. Extract all key financial metrics (Revenue, EBITDA, PAT, Margins, etc.) and operational KPIs (volumes, utilization, order book, etc.).
            -   Present this data in **Markdown tables** for clarity.
            -   Always include the period (e.g., Q3 FY24, FY24) and any YoY or QoQ growth figures mentioned.

        2.  **Detailed Revenue & Profit Breakdowns:**
            -   Create separate sections for revenue and profit breakdowns.
            -   Detail the contribution from every business segment, geographical region, or product line mentioned. Include absolute values and percentages if available.
            -   If this information is not present, you must explicitly state: "A detailed revenue/profit breakdown was not provided."

        3.  **Management Commentary & Outlook:**
            -   Extract **verbatim quotes** or detailed summaries of management's commentary on performance, strategy, and future outlook.
            -   List all stated future guidance, capex plans, new projects, and strategic initiatives.
            -   Summarize any discussion of industry trends, risks, headwinds, or competitive landscape.

        4.  **Source Everything:**
            -   If possible, reference the page number from the PDF where the information was found (e.g., "Source: Page 5").

        Do not summarize aggressively. The goal is a comprehensive, data-rich extraction of all relevant information from the document.
        """

        log_progress("Calling Gemini to extract insights from Investor Presentation...")
        
        # New SDK generation syntax
        response = genai_client.models.generate_content(
            model='gemini-3-flash-preview',
            contents=[
                types.Part.from_text(text=prompt),
                pdf_file
            ]
        )

        genai_client.files.delete(name=pdf_file.name)
        print(f"Cleaned up uploaded file {pdf_file.name}.")
        return response.text

        return response.text

    except Exception as e:
        print(f"ERROR: Failed to generate summary with Gemini for PDF URL {pdf_url}. Reason: {e}")
        traceback.print_exc()
        return f"Error: The AI model failed to analyze the presentation. Reason: {e}"

# In screener_fetcher.py

async def fetch_latest_document_dates_async(ticker: str) -> dict:
    """
    Lightweight fetch of ONLY the dates of the latest concall & presentation
    from Screener.in. Does NOT download or process any PDFs.
    Returns: {"concall_date": "Nov 14, 2025", "presentation_date": "Nov 14, 2025"}
    """
    result = {"concall_date": "", "presentation_date": ""}
    try:
        url = BASE_URL.format(ticker=ticker)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, headers=HEADERS, timeout=15.0)
            if response.status_code != 200:
                return result

        soup = BeautifulSoup(response.text, 'html.parser')
        concalls_section = soup.find('div', class_='concalls')
        if not concalls_section:
            return result

        for item in concalls_section.find_all('li', limit=4):
            if result["concall_date"] and result["presentation_date"]:
                break

            date_element = item.find('div', class_='nowrap')
            date_text = date_element.text.strip() if date_element else ""

            if not result["concall_date"]:
                transcript_link = item.find('a', string='Transcript', href=True)
                if transcript_link:
                    result["concall_date"] = date_text

            if not result["presentation_date"]:
                ppt_link = item.find('a', string='PPT', href=True)
                if ppt_link:
                    result["presentation_date"] = date_text

        return result
    except Exception as e:
        print(f"DEBUG: fetch_latest_document_dates failed for {ticker}: {e}")
        return result


async def fetch_latest_documents_async(ticker: str) -> list[dict]:
    try:
        url = BASE_URL.format(ticker=ticker)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, headers=HEADERS, timeout=45.0)
            response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        concalls_section = soup.find('div', class_='concalls')
        if not concalls_section: return []

        tasks_to_run = []
        doc_infos = []
        found_types = set()

        for item in concalls_section.find_all('li', limit=4): # Limit search to recent items
            if len(found_types) == 2: break
            date_element = item.find('div', class_='nowrap')
            raw_date = date_element.text.strip() if date_element else ""
            date_text = f"({raw_date})" if raw_date else ""

            if 'Concall' not in found_types:
                transcript_link = item.find('a', string='Transcript', href=True)
                rec_link = item.find('a', string='REC', href=True)
                if transcript_link or rec_link:
                    link = transcript_link['href'] if transcript_link else ""
                    doc_info = {"type": "Concall", "text": f"Concall Transcript {date_text}", "link": link, "date": raw_date}
                    if rec_link:
                        doc_info["rec_link"] = rec_link['href']
                    
                    if link:
                        tasks_to_run.append(get_text_from_pdf_url_async(doc_info['link']))
                    else:
                        # Dummy task if only REC link exists so it still gets returned
                        async def dummy_task(*args, **kwargs): return ""
                        tasks_to_run.append(dummy_task())
                        
                    doc_infos.append(doc_info)
                    found_types.add('Concall')

            if 'Presentation' not in found_types:
                ppt_link = item.find('a', string='PPT', href=True)
                if ppt_link:
                    doc_info = {"type": "Presentation", "text": f"Results Presentation {date_text}", "link": ppt_link['href'], "date": raw_date}
                    tasks_to_run.append(summarize_presentation_with_gemini_async(doc_info['link']))
                    doc_infos.append(doc_info)
                    found_types.add('Presentation')
        
        if not tasks_to_run: return []
        
        log_progress(f"Fetching and analyzing {len(tasks_to_run)} documents in parallel...")
        summaries = await asyncio.gather(*tasks_to_run, return_exceptions=True)
        
        final_docs = []
        for i, summary in enumerate(summaries):
            doc_info = doc_infos[i]
            if isinstance(summary, Exception):
                print(f"Failed to process document {doc_info['link']}: {summary}")
                doc_info['content_summary'] = f"Error processing document: {summary}"
            else:
                doc_info['content_summary'] = summary
            final_docs.append(doc_info)
        
        # --- Also find the latest Annual Report URL (link-only, no download at this stage) ---
        try:
            annual_links = soup.select('a[class*="Annual+Report"]')
            if annual_links:
                ar_link = annual_links[0]
                ar_href = ar_link.get('href', '').strip().replace(' ', '%20')
                if ar_href:
                    ar_date_div = ar_link.find('div')
                    ar_date = ar_date_div.text.strip() if ar_date_div else ''
                    ar_date_text = f"({ar_date})" if ar_date else ''
                    final_docs.append({
                        "type": "Annual Report",
                        "text": f"Annual Report {ar_date_text}",
                        "link": ar_href,
                        "date": ar_date,
                        # No content_summary — this is link-only at initial load
                    })
                    print(f"INFO: Found Annual Report URL for {ticker}: {ar_href}")
        except Exception as ar_err:
            print(f"WARN: Could not find Annual Report link for {ticker}: {ar_err}")

        return final_docs
    except Exception as e:
        print(f"Error fetching latest documents for {ticker}: {e}")
        return []

async def fetch_concall_rec_url_async(ticker: str) -> dict:
    """
    Fetch the latest concall recording URL from Screener.in.
    Returns dict with keys: url, date (or empty dict if none found).
    Looks for REC links in the concalls section.
    """
    try:
        url = BASE_URL.format(ticker=ticker)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, headers=HEADERS, timeout=45.0)
            response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        concalls_section = soup.find('div', class_='concalls')
        if not concalls_section: return {}

        for item in concalls_section.find_all('li', limit=4):
            rec_link = item.find('a', string='REC', href=True)
            if rec_link:
                date_element = item.find('div', class_='nowrap')
                raw_date = date_element.text.strip() if date_element else ""
                return {
                    "url": rec_link['href'],
                    "date": raw_date
                }
        return {}
    except Exception as e:
        print(f"Error fetching concall REC url for {ticker}: {e}")
        return {}



# =====================================================================
# Segment Revenue Extraction (On-Demand, AI-Powered)
# =====================================================================

def _find_segment_pages(pdf_bytes: bytes, max_pages: int = 60) -> list[int]:
    """
    Quick scan of an Annual Report PDF to find pages containing segment,
    geography, or export data. Returns a list of page indices (0-based).
    
    Uses a two-tier keyword system:
    - HIGH-confidence keywords (segment tables in Notes to Financial Statements)
    - LOW-confidence keywords (generic mentions that may appear anywhere)
    
    Prioritises dense clusters of keyword hits over scattered mentions.
    """
    # Tier 1: High-confidence — these almost always appear ON the actual segment data pages
    high_keywords = [
        'segment information', 'segment reporting', 'segmental information',
        'reportable segment', 'operating segment', 'business segment',
        'segment result', 'segment revenue', 'segment asset',
        'geographical segment', 'geographic segment',
        'revenue from external customer',
        'inter-segment', 'inter segment',
        'segment wise', 'segment-wise', 'segmentwise',
    ]
    
    # Tier 2: Lower-confidence — useful as supporting evidence but prone to false positives
    low_keywords = [
        'management discussion', 'md&a', 'management analysis',
        'financial highlights',
        'revenue breakdown', 'revenue by',
        'ebitda by', 'profit by segment',
        'domestic port', 'international port',
        'product wise', 'product-wise', 'vertical wise', 'vertical-wise',
        'sector wise', 'sector-wise',
        'geography wise', 'geography-wise', 'region wise', 'region-wise',
    ]
    
    # Tier 3: Very generic — only count if near high-confidence pages
    generic_keywords = ['domestic', 'export', 'overseas']
    
    high_pages = set()
    low_pages = set()
    generic_pages = set()
    
    try:
        from pypdf import PdfReader
        from io import BytesIO
        
        reader = PdfReader(BytesIO(pdf_bytes))
        total_pages = len(reader.pages)
        
        for i, page in enumerate(reader.pages):
            try:
                text = (page.extract_text() or '').lower()
            except Exception:
                text = ''
                
            if not text or len(text.strip()) < 50:
                continue
                
            if any(kw in text for kw in high_keywords):
                high_pages.add(i)
            elif any(kw in text for kw in low_keywords):
                low_pages.add(i)
            elif any(kw in text for kw in generic_keywords):
                generic_pages.add(i)
        
        print(f"INFO: _find_segment_pages (pypdf) scan: {len(high_pages)} high, {len(low_pages)} low, {len(generic_pages)} generic hits out of {total_pages} pages")
        
        # Build the final page set with context windows
        target_pages = set()
        
        # High-confidence pages get wide context (±3 pages for multi-page tables)
        for p in high_pages:
            for j in range(max(0, p - 3), min(total_pages, p + 6)):
                target_pages.add(j)
        
        # Low-confidence pages get medium context (±2 pages)
        for p in low_pages:
            for j in range(max(0, p - 2), min(total_pages, p + 4)):
                target_pages.add(j)
        
        # Generic pages only included if they're within 10 pages of a high/low hit
        high_low_combined = high_pages | low_pages
        for p in generic_pages:
            if any(abs(p - hp) <= 10 for hp in high_low_combined):
                for j in range(max(0, p - 1), min(total_pages, p + 3)):
                    target_pages.add(j)
            
            # If we have high-confidence hits, prioritize those clusters
            if high_pages and len(target_pages) > max_pages:
                # Too many pages — trim to prioritize high-confidence clusters
                # Score each page by proximity to high-confidence hits
                page_scores = {}
                for p in target_pages:
                    min_dist = min(abs(p - hp) for hp in high_pages)
                    page_scores[p] = min_dist
                # Sort by proximity to high-confidence hits, take top max_pages
                sorted_pages = sorted(target_pages, key=lambda p: page_scores[p])
                target_pages = set(sorted_pages[:max_pages])
            
            # If no keyword pages found at all, fallback to latter half of document
            if not target_pages:
                if total_pages > 20:
                    # Segment notes are typically in the last 30% of Annual Reports
                    start = int(total_pages * 0.6)
                    for j in range(start, min(total_pages, start + 40)):
                        target_pages.add(j)
                    print(f"INFO: No segment keywords found. Using pages {start}-{min(total_pages, start + 40)} as fallback")
                    
    except Exception as e:
        print(f"WARN: _find_segment_pages failed: {e}")
        return []
    
    result = sorted(target_pages)[:max_pages]
    print(f"INFO: _find_segment_pages returning {len(result)} pages out of {total_pages} total")
    return result


def _create_trimmed_pdf(pdf_bytes: bytes, page_indices: list[int]) -> bytes:
    """
    Creates a new PDF containing only the specified pages from the original PDF.
    Uses PyPDF2/pypdf if available, otherwise falls back to returning full PDF.
    """
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError:
        try:
            from PyPDF2 import PdfReader, PdfWriter
        except ImportError:
            print("WARN: Neither pypdf nor PyPDF2 available. Using full PDF.")
            return pdf_bytes
    
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        writer = PdfWriter()
        for idx in page_indices:
            if idx < len(reader.pages):
                writer.add_page(reader.pages[idx])
        
        output = BytesIO()
        writer.write(output)
        trimmed = output.getvalue()
        print(f"INFO: Trimmed PDF from {len(reader.pages)} pages to {len(page_indices)} pages ({len(trimmed)//1024}KB)")
        return trimmed
    except Exception as e:
        print(f"WARN: PDF trimming failed: {e}. Using full PDF.")
        return pdf_bytes


SEGMENT_EXTRACTION_PROMPT = """You are an expert financial data extractor specializing in Indian company Annual Reports and Investor Presentations. Analyze this document THOROUGHLY and extract ALL revenue and profitability breakdowns.

**WHERE TO LOOK (in order of priority):**
1. "Notes to the Consolidated Financial Statements" → "Segment Information" / "Segment Reporting" sections (these contain the most detailed, audited data)
2. "Financial Highlights" section (summary-level data with absolute numbers)
3. "Management Discussion & Analysis" (narrative data with segment commentary)
4. Any charts, tables, or infographics showing segment/geography breakdowns

**REQUIRED EXTRACTIONS (extract ALL that are available):**

1. **Segment-wise Revenue** — Revenue/Sales by business segment/division. For each segment, extract: absolute revenue (₹ Cr), percentage of total, and YoY growth. Also include inter-segment revenue and eliminations if shown.
2. **Segment-wise Operating Profit / EBITDA** — Operating profit or EBITDA by segment with absolute values and margins.
3. **Segment-wise Results** — Segment Results / Profit Before Tax by segment if shown in the Notes to Financial Statements.
4. **Geography-wise Revenue** — Revenue split by geography (e.g., India vs Outside India, or by country/region). Include both current year and previous year figures.
5. **Domestic vs Export / International Revenue** — Revenue split between domestic and export/international operations.
6. **Sector-wise / Vertical-wise Revenue** — Revenue by end-use industry, customer vertical, or application area.
7. **Product-wise Revenue** — Revenue by product category or service line if available.

**CRITICAL INSTRUCTIONS:**
- Extract BOTH current year AND previous year figures when available (shown in the same table)
- Previous year figures are often shown in italics or in a smaller font — extract them too
- Include "Total" rows in tables for verification
- Use the exact numbers from the document. Do NOT invent, estimate, or calculate data.
- For tables in Notes to Financial Statements, include all columns shown (e.g., "Port and SEZ activities", "Others", "Eliminations", "Total")

**OUTPUT FORMAT:**
- Output clean, well-formatted HTML using tables.
- Use this exact structure for each breakdown found:

<div class="segment-block">
<h4 class="segment-title">[Title, e.g. "Segment-wise Revenue (FY2025)"]</h4>
<table class="segment-table">
<thead><tr><th>Segment</th><th>FY2025 (₹ Cr)</th><th>FY2024 (₹ Cr)</th><th>YoY Growth</th></tr></thead>
<tbody><tr><td>...</td><td>...</td><td>...</td><td>...</td></tr></tbody>
</table>
</div>

- Adapt the column headers to match the actual data in the document.
- If a breakdown is NOT found in the document, output: <p class="segment-not-found">⚠️ [Category] data was not found in this document.</p>
- Include the period/year the data relates to in each table title.

Do NOT include any introductory text, markdown, or explanation outside the HTML structure above."""


async def fetch_segment_data_from_document_async(pdf_url: str, doc_type: str) -> str:
    """
    Fetches segment-wise, geography-wise, domestic/export revenue & profit data
    from a document using Gemini's native PDF analysis.
    
    Args:
        pdf_url: URL of the PDF document
        doc_type: 'annual_report', 'presentation', or 'concall'
    Returns:
        HTML string with formatted segment data tables
    """
    if not GOOGLE_API_KEY:
        return '<p class="segment-not-found">⚠️ Google API Key is not configured. Cannot analyze documents.</p>'
    
    try:
        # Step 1: Download the PDF
        log_progress(f"Downloading {doc_type} document for segment analysis...")
        async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
            response = await client.get(pdf_url, headers=HEADERS, timeout=60.0)
            response.raise_for_status()
            pdf_content = response.content
        
        # Check if it's actually a PDF
        if not pdf_content.strip().startswith(b'%PDF'):
            content_type = response.headers.get('content-type', '').lower()
            if 'html' in content_type:
                return '<p class="segment-not-found">⚠️ The document URL returned an HTML page instead of a PDF. Segment extraction not possible.</p>'
        
        # Step 2: For Annual Reports, trim to relevant pages
        upload_content = pdf_content
        if doc_type == 'annual_report':
            log_progress("Scanning Annual Report for segment/geography pages...")
            segment_pages = await asyncio.to_thread(_find_segment_pages, pdf_content)
            if segment_pages:
                upload_content = await asyncio.to_thread(_create_trimmed_pdf, pdf_content, segment_pages)
            else:
                log_progress("No specific segment pages found. Uploading full document to Gemini...")
        
        # Step 3: For concalls, use text extraction instead of Gemini File API
        if doc_type == 'concall':
            # Concall transcripts are better handled as text
            extracted_text = await get_text_from_pdf_url_async(pdf_url, max_pages_to_process=75, max_chars_to_return=30000)
            if not extracted_text or len(extracted_text.strip()) < 200:
                return '<p class="segment-not-found">⚠️ Could not extract sufficient text from the concall transcript.</p>'
            
            # Use OpenAI/Gemini text model for concall text
            def blocking_text_analysis():
                global genai_client
                if not genai_client:
                    raise ValueError("GenAI client not initialized")
                
                response = genai_client.models.generate_content(
                    model='gemini-3-flash-preview',
                    contents=[
                        types.Part.from_text(text=SEGMENT_EXTRACTION_PROMPT),
                        types.Part.from_text(text=f"Here is the concall transcript text:\n\n{extracted_text}")
                    ]
                )
                return response.text
            
            result_html = await asyncio.to_thread(blocking_text_analysis)
            return result_html
        
        # Step 4: Upload PDF to Gemini File API and analyze (for annual_report and presentation)
        def blocking_gemini_segment_analysis(content):
            global genai_client
            if not genai_client:
                raise ValueError("GenAI client not initialized")
            
            log_progress(f"Uploading {doc_type} to Gemini for segment analysis...")
            
            pdf_file = genai_client.files.upload(
                file=BytesIO(content),
                config=types.UploadFileConfig(
                    display_name=f"segment_analysis_{doc_type}.pdf",
                    mime_type='application/pdf'
                )
            )
            print(f"INFO: PDF uploaded for segment analysis as '{pdf_file.name}'")
            
            log_progress("Extracting segment and geography data with AI...")
            
            response = genai_client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=[
                    types.Part.from_text(text=SEGMENT_EXTRACTION_PROMPT),
                    pdf_file
                ]
            )
            
            # Clean up uploaded file
            try:
                genai_client.files.delete(name=pdf_file.name)
                print(f"INFO: Cleaned up segment analysis file {pdf_file.name}")
            except Exception:
                pass
            
            return response.text
        
        result_html = await asyncio.to_thread(blocking_gemini_segment_analysis, upload_content)
        
        # Strip any markdown code fences if Gemini wraps it
        if result_html:
            result_html = result_html.strip()
            if result_html.startswith('```html'):
                result_html = result_html[7:]
            elif result_html.startswith('```'):
                result_html = result_html[3:]
            if result_html.endswith('```'):
                result_html = result_html[:-3]
            result_html = result_html.strip()
        
        return result_html
        
    except Exception as e:
        print(f"ERROR: fetch_segment_data_from_document_async failed for {pdf_url}: {e}")
        traceback.print_exc()
        return f'<p class="segment-not-found">⚠️ Error analyzing document: {e}</p>'


async def fetch_document_url_for_segment_analysis(ticker: str, section: str) -> tuple:
    """
    Fetches the appropriate document URL for segment analysis based on the section type.
    
    Args:
        ticker: Stock ticker
        section: "Annual Results" or "Quarterly Results"
    Returns:
        tuple: (pdf_url, doc_type) or (None, None) if not found
    """
    try:
        url = BASE_URL.format(ticker=ticker)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, headers=HEADERS, timeout=45.0)
            response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        if section == "Annual Results":
            # Find the latest Annual Report
            annual_links = soup.select('a[class*="Annual+Report"]')
            if annual_links:
                href = annual_links[0].get('href', '').strip().replace(' ', '%20')
                if href:
                    return (href, 'annual_report')
            return (None, None)
        
        elif section == "Quarterly Results":
            # Prefer Investor Presentation, fallback to Concall Transcript
            concalls_section = soup.find('div', class_='concalls')
            if not concalls_section:
                return (None, None)
            
            for item in concalls_section.find_all('li', limit=4):
                # Try Presentation first
                ppt_link = item.find('a', string='PPT', href=True)
                if ppt_link:
                    return (ppt_link['href'], 'presentation')
            
            # Fallback to Concall
            for item in concalls_section.find_all('li', limit=4):
                transcript_link = item.find('a', string='Transcript', href=True)
                if transcript_link:
                    return (transcript_link['href'], 'concall')
            
            return (None, None)
        
        return (None, None)
        
    except Exception as e:
        print(f"ERROR: fetch_document_url_for_segment_analysis failed for {ticker}: {e}")
        return (None, None)


# =====================================================================
# Forensic Agent: Credit Rating + Annual Report Fetcher
# =====================================================================

async def fetch_forensic_documents_async(ticker: str) -> dict:
    """
    Fetches credit rating PDFs (latest 2) and annual report PDF (latest 1)
    from the Screener.in documents section for forensic analysis.
    
    Returns:
        dict: {
            'credit_ratings': [{'label': str, 'link': str, 'text': str}, ...],  # up to 2
            'annual_report': {'label': str, 'link': str, 'text': str} or None
        }
    """
    result = {'credit_ratings': [], 'annual_report': None}
    
    try:
        url = BASE_URL.format(ticker=ticker)
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(url, headers=HEADERS, timeout=45.0)
            response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # --- Find Credit Rating links (latest 2) ---
        credit_links = soup.select('a[class*="Credit+Rating"]')
        credit_tasks = []
        credit_infos = []
        
        for link in credit_links[:2]:  # Latest 2 credit ratings
            href = link.get('href', '')
            if not href:
                continue
            
            # --- FIX: URL Encode the link to handle spaces ---
            # Example: .../June 20_ 2025... -> .../June%2020_%202025...
            href = href.strip().replace(' ', '%20')
            # Extract label: "Rating update" + date/agency from child div
            label_parts = [link.get_text(separator=' ', strip=True)]
            date_div = link.find('div')
            date_text = date_div.text.strip() if date_div else ''
            label = f"Credit Rating - {date_text}" if date_text else "Credit Rating"
            
            info = {'label': label, 'link': href, 'text': ''}
            credit_infos.append(info)
            credit_tasks.append(get_text_from_pdf_url_async(href, max_pages_to_process=20, max_chars_to_return=15000))
        
        # --- Find Annual Report link (latest 1) ---
        annual_links = soup.select('a[class*="Annual+Report"]')
        annual_task = None
        annual_info = None
        
        if annual_links:
            link = annual_links[0]  # Latest annual report only
            href = link.get('href', '')
            if href:
                label_text = link.get_text(separator=' ', strip=True)
                annual_info = {'label': label_text or 'Annual Report', 'link': href, 'text': ''}
                # Annual reports can be large, limit extraction
                annual_task = get_text_from_pdf_url_async(href, max_pages_to_process=30, max_chars_to_return=20000)
        
        # --- Run all PDF extractions in parallel ---
        all_tasks = credit_tasks.copy()
        if annual_task:
            all_tasks.append(annual_task)
        
        if not all_tasks:
            print(f"FORENSIC_DOCS: No credit rating or annual report documents found for {ticker}")
            return result
        
        print(f"FORENSIC_DOCS: Fetching {len(credit_tasks)} credit rating(s) + {'1 annual report' if annual_task else '0 annual reports'} for {ticker}")
        
        extracted = await asyncio.gather(*all_tasks, return_exceptions=True)
        
        # --- Map results back ---
        for i, text_result in enumerate(extracted):
            if i < len(credit_infos):
                # Credit rating result
                if isinstance(text_result, Exception):
                    print(f"FORENSIC_DOCS: Failed to extract credit rating PDF: {text_result}")
                    credit_infos[i]['text'] = f"Error extracting PDF: {text_result}"
                else:
                    credit_infos[i]['text'] = text_result or "No text extracted from PDF"
            else:
                # Annual report result
                if annual_info:
                    if isinstance(text_result, Exception):
                        print(f"FORENSIC_DOCS: Failed to extract annual report PDF: {text_result}")
                        annual_info['text'] = f"Error extracting PDF: {text_result}"
                    else:
                        annual_info['text'] = text_result or "No text extracted from PDF"
        
        result['credit_ratings'] = credit_infos
        result['annual_report'] = annual_info
        
        print(f"FORENSIC_DOCS: Successfully fetched {len(credit_infos)} credit ratings + {'1 annual report' if annual_info else '0'} for {ticker}")
        return result
        
    except Exception as e:
        print(f"FORENSIC_DOCS ERROR: Could not fetch forensic documents for {ticker}. Reason: {e}")
        traceback.print_exc()
        return result


def fetch_latest_documents(ticker: str) -> list[dict]:
    """
    MODIFIED: Now uses Gemini to summarize Results Presentations (PPT) and
    pdfplumber for Concall Transcripts.
    """
    documents = []
    found_types = set()
    try:
        url = BASE_URL.format(ticker=ticker)
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')

        concalls_section = soup.find('div', class_='concalls')
        if not concalls_section:
            print(f"WARN: No 'concalls' section found for {ticker}")
            return []

        for item in concalls_section.find_all('li'):
            if len(found_types) == 2:
                break

            date_element = item.find('div', class_='nowrap')
            date_text = f"({date_element.text.strip()})" if date_element else ""

            # --- Concall Transcript Logic (Unchanged) ---
            if 'Concall' not in found_types:
                transcript_link = item.find('a', string='Transcript')
                if transcript_link and transcript_link.get('href'):
                    doc_info = { "type": "Concall", "text": f"Concall Transcript {date_text}", "link": transcript_link['href'] }
                    log_progress(f"AI is fetching and analyzing concall transcript for {ticker}")
                    summary = get_text_from_pdf_url(doc_info['link']) # Keep using the old method for transcripts
                    if summary:
                        doc_info['content_summary'] = summary
                    documents.append(doc_info)
                    found_types.add('Concall')

            # --- MODIFIED: Presentation (PPT) Logic ---
            if 'Presentation' not in found_types:
                ppt_link = item.find('a', string='PPT')
                if ppt_link and ppt_link.get('href'):
                    doc_info = { 
                        "type": "Presentation", 
                        "text": f"Results Presentation {date_text}", 
                        "link": ppt_link['href'] 
                    }
                    # NEW: Call the Gemini summarizer for the presentation link
                    log_progress(f"AI is fetching and analyzing the latest investor presentation for {ticker}...")
                    summary = summarize_presentation_with_gemini(doc_info['link'])
                    if summary:
                        doc_info['content_summary'] = summary
                    
                    documents.append(doc_info)
                    found_types.add('Presentation')
                    
        return documents
    except Exception as e:
        print(f"ERROR: Could not fetch documents for {ticker}. Reason: {e}")
        traceback.print_exc()
        return []


# =====================================================================
# Peer Comparison Data Fetcher (Direct Screener.in Scrape)
# =====================================================================

def fetch_peer_comparison_from_screener(ticker: str, company_name: str = None) -> dict:
    """
    Fetches peer comparison data directly from Screener.in by scraping the 
    peer comparison table (section id: #peers).
    
    Uses a two-stage approach like fetch_consolidated_async:
    STAGE 1: Fast HTTP fetch (works if content is server-rendered)
    STAGE 2: Playwright browser fetch (fallback for JS-rendered content)
    
    Args:
        ticker: Stock ticker (e.g., 'NH')
        company_name: Optional company name from yfinance for fuzzy matching
    
    Returns:
        dict: {
            'company': {'name': str, 'cmp': str, 'market_cap': str, ...},
            'peers': [{'name': str, 'cmp': str, 'market_cap': str, ...}, ...]
        }
    """
    import asyncio
    
    # Run the async version in a new event loop for sync compatibility
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # If we're already in an async context, run in a new thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, fetch_peer_comparison_from_screener_async(ticker, company_name))
                return future.result(timeout=60)
        else:
            return loop.run_until_complete(fetch_peer_comparison_from_screener_async(ticker, company_name))
    except RuntimeError:
        # No event loop exists, create one
        return asyncio.run(fetch_peer_comparison_from_screener_async(ticker, company_name))
    except Exception as e:
        print(f"ERROR: Sync wrapper for peer comparison failed: {e}")
        return {'company': {}, 'peers': []}


async def fetch_peer_comparison_from_screener_async(ticker: str, company_name: str = None) -> dict:
    """
    Async version of fetch_peer_comparison_from_screener.
    Uses a two-stage approach:
    STAGE 1: Fast httpx fetch
    STAGE 2: Playwright browser fetch (fallback for JS-rendered content)
    
    Args:
        ticker: Stock ticker (e.g., 'NARAYANAHRU')
        company_name: Optional company name from yfinance for fuzzy matching
    """
    from playwright.async_api import async_playwright
    
    standalone_url = f"https://www.screener.in/company/{ticker}/"
    consolidated_url = standalone_url + "consolidated/"
    
    try:
        # =====================================================
        # STAGE 1: FAST HTTP FETCH
        # =====================================================
        log_progress(f"Attempting fast fetch for {ticker} peer comparison...")
        soup = None
        
        async with httpx.AsyncClient() as client:
            for url in [consolidated_url, standalone_url]:
                try:
                    response = await client.get(url, headers=HEADERS, timeout=30.0)
                    response.raise_for_status()
                    soup = BeautifulSoup(response.text, 'html.parser')
                    
                    # Check if peer section has a table (indicating content is loaded)
                    peers_section = soup.select_one("#peers")
                    if peers_section and peers_section.select_one("table"):
                        log_progress(f"Fast fetch found peer table at {url}")
                        return _parse_peer_table(soup, ticker, company_name)
                except httpx.HTTPStatusError as e:
                    print(f"WARN: HTTP error fetching {url}: {e}")
                    continue
                except Exception as e:
                    print(f"WARN: Error fetching {url}: {e}")
                    continue
        
        # =====================================================
        # STAGE 2: PLAYWRIGHT BROWSER FETCH (JS-rendered content)
        # =====================================================
        log_progress(f"Fast fetch insufficient. Using browser for {ticker} peer comparison...")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            try:
                # Try consolidated first, then standalone (consolidated has accurate data)
                html = None
                for url in [consolidated_url, standalone_url]:
                    try:
                        await page.goto(url, wait_until='networkidle', timeout=30000)
                        # Wait for the peers section table
                        try:
                            await page.wait_for_selector('#peers table', timeout=5000)
                            log_progress(f"Populated peer table")
                            html = await page.content()
                            break  # Found table, stop trying
                        except:
                            log_progress(f"Peer table not found, trying next...")
                            continue
                    except Exception as e:
                        print(f"WARN: Peer Table failed to load: {e}")
                        continue
                
                if not html:
                    # Last resort - get whatever we have
                    html = await page.content()
            finally:
                await browser.close()
        
        soup = BeautifulSoup(html, 'html.parser')
        return _parse_peer_table(soup, ticker, company_name)
        
    except Exception as e:
        print(f"ERROR: Async peer comparison fetch failed for {ticker}: {e}")
        traceback.print_exc()
        return {'company': {}, 'peers': []}


def _parse_peer_table(soup, ticker: str, company_name: str = None) -> dict:
    """
    Helper function to parse the peer table from BeautifulSoup object.
    
    Args:
        soup: BeautifulSoup object of the page
        ticker: Stock ticker (e.g., 'NARAYANAHRU')
        company_name: Optional company name from yfinance (e.g., 'Narayana Hrudayalaya Ltd')
                      Used for fuzzy matching when ticker doesn't appear in screener names.
    """
    try:
        peers_section = soup.select_one("#peers")
        if not peers_section:
            print(f"WARN: No #peers section found for {ticker}")
            return {'company': {}, 'peers': []}
        
        table = peers_section.select_one("table")
        if not table:
            print(f"WARN: No peer comparison table found for {ticker}")
            return {'company': {}, 'peers': []}
        
        # Parse the table using pandas
        try:
            df_list = pd.read_html(str(table))
            if not df_list:
                return {'company': {}, 'peers': []}
            df = df_list[0]
        except Exception as e:
            print(f"ERROR: Failed to parse peer comparison table: {e}")
            return {'company': {}, 'peers': []}
        
        # Clean up the DataFrame
        df = clean_df(df)
        
        # Debug: Print actual columns found
        print(f"DEBUG: Screener columns found: {list(df.columns)}")
        
        # Map Screener.in column names to our standard names
        # Screener columns: S.No., Name, CMP Rs., P/E, Mar Cap Rs.Cr., Div Yld %, 
        # NP Qtr Rs.Cr., Qtr Profit Var %, Sales Qtr Rs.Cr., Qtr Sales Var %, ROCE %
        column_mapping = {}
        for col in df.columns:
            col_lower = col.lower().strip()
            
            # Skip S.No. column
            if 's.no' in col_lower or col_lower == 's.no.':
                continue
            
            if 'name' in col_lower:
                column_mapping[col] = 'name'
            # IMPORTANT: Check for P/B ratio patterns BEFORE 'cmp' to avoid false match
            # Screener uses "CMP / BV" for Price-to-Book
            elif 'bv' in col_lower and 'cmp' in col_lower:
                # Handles: "CMP / BV", "CMP/BV", "CMP/ BV", "CMP /BV" etc.
                column_mapping[col] = 'pb_ratio'
                print(f"DEBUG: Mapped column '{col}' -> pb_ratio")
            elif 'cmp' in col_lower or col_lower == 'price':
                column_mapping[col] = 'cmp'
            elif 'mar cap' in col_lower or 'market cap' in col_lower or 'mcap' in col_lower:
                column_mapping[col] = 'market_cap'
            elif 'p/e' in col_lower or col_lower == 'pe':
                column_mapping[col] = 'pe_ratio'
            # P/B ratio: additional patterns
            elif 'p/b' in col_lower or col_lower == 'pb' or 'pbv' in col_lower:
                column_mapping[col] = 'pb_ratio'
                print(f"DEBUG: Mapped column '{col}' -> pb_ratio")
            elif 'div' in col_lower and ('yld' in col_lower or 'yield' in col_lower or '%' in col_lower):
                column_mapping[col] = 'dividend_yield'
            elif 'roce' in col_lower:
                column_mapping[col] = 'roce'
            elif 'roe' in col_lower:
                column_mapping[col] = 'roe'
            # Qtr Sales Var % -> sales_growth_yoy
            elif 'sales' in col_lower and 'var' in col_lower:
                column_mapping[col] = 'sales_growth_yoy'
            # OPM % / Operating Margin / Financing Margin -> ebitda_growth_yoy (operating margin, not net profit)
            elif 'opm' in col_lower or 'operating margin' in col_lower or 'financing margin' in col_lower:
                column_mapping[col] = 'ebitda_growth_yoy'
                print(f"DEBUG: Mapped '{col}' -> ebitda_growth_yoy (operating margin)")
            # Net Profit Gr / Qtr Profit Var -> separate field (NOT ebitda)
            elif 'profit' in col_lower and ('var' in col_lower or 'gr' in col_lower):
                column_mapping[col] = 'net_profit_growth'  # Separate from EBITDA
            # NP Qtr can help calculate NPM if needed
            elif 'np qtr' in col_lower or 'net profit' in col_lower:
                column_mapping[col] = 'np_qtr'
            # Sales Qtr for context
            elif 'sales qtr' in col_lower or 'sales' in col_lower and 'qtr' in col_lower:
                column_mapping[col] = 'sales_qtr'
            elif 'npm' in col_lower or 'np margin' in col_lower or 'net margin' in col_lower:
                column_mapping[col] = 'npm'
        
        print(f"DEBUG: Column mapping: {column_mapping}")
        
        # Rename columns
        df = df.rename(columns=column_mapping)

        
        # Convert to list of dicts
        records = df.to_dict('records')
        
        if not records:
            return {'company': {}, 'peers': []}
        
        # =====================================================================
        # IMPROVED: Two-pass company identification with fuzzy name matching
        # =====================================================================
        # The old logic assumed row 0 was always the searched company, which
        # is incorrect when screener.in has a different sorting order.
        # 
        # New approach:
        # 1. First pass: Find best fuzzy match for company_name among all records
        # 2. If no good match (threshold 0.5), fall back to first row
        # =====================================================================
        
        company_data = {}
        peers_data = []
        
        # Extract all names from records for matching
        record_names = [(i, record.get('name', '')) for i, record in enumerate(records)]
        
        # Helper function for fuzzy matching
        def fuzzy_match_score(name1: str, name2: str) -> float:
            """Calculate similarity ratio between two names (0.0 to 1.0)."""
            if not name1 or not name2:
                return 0.0
            # Normalize: lowercase, remove common suffixes
            n1 = name1.lower().replace('ltd.', '').replace('ltd', '').replace('limited', '').strip()
            n2 = name2.lower().replace('ltd.', '').replace('ltd', '').replace('limited', '').strip()
            return SequenceMatcher(None, n1, n2).ratio()
        
        # Strategy prioritization:
        # 1. Fuzzy match on company_name (most reliable when provided)
        # 2. Ticker substring match (works for some cases like 'INFY' in 'Infosys')
        # 3. Fallback to first row (legacy behavior)
        
        company_index = None
        best_match_score = 0.0
        MATCH_THRESHOLD = 0.5  # Minimum similarity to consider a match
        ticker_upper = ticker.upper()
        
        # STRATEGY 1: Fuzzy match using company_name (if provided)
        if company_name and company_index is None:
            for i, record_name in record_names:
                if not record_name:
                    continue
                score = fuzzy_match_score(company_name, record_name)
                if score > best_match_score:
                    best_match_score = score
                    if score >= MATCH_THRESHOLD:
                        company_index = i
            
            if company_index is not None:
                print(f"DEBUG: Found company by fuzzy match: '{record_names[company_index][1]}' " 
                      f"matches '{company_name}' (score: {best_match_score:.2f})")
        
        # STRATEGY 2: Ticker substring match (fallback)
        if company_index is None:
            for i, record_name in record_names:
                if not record_name:
                    continue
                record_name_upper = record_name.upper()
                # Check if ticker appears in the name (e.g., "INFY" in "Infosys Ltd")  
                if ticker_upper in record_name_upper:
                    company_index = i
                    print(f"DEBUG: Found company by ticker match: '{record_name}' contains '{ticker_upper}'")
                    break
        
        # STRATEGY 3: Fallback to first row (legacy behavior)
        if company_index is None:
            company_index = 0
            print(f"WARN: Could not find {ticker} in peer table by name, using first row as fallback")
        
        # Now process records with proper company identification
        industry_avg = {}
        for i, record in enumerate(records):
            # Clean the record - convert all values to strings and handle NaN
            cleaned_record = {}
            # Include all standard metric columns plus the new ones we're extracting
            allowed_keys = ['name', 'cmp', 'market_cap', 'pe_ratio', 'pb_ratio', 
                          'dividend_yield', 'roce', 'roe', 'sales_growth_yoy', 
                          'ebitda_growth_yoy', 'npm', 'np_qtr', 'sales_qtr']
            for key, value in record.items():
                if key in allowed_keys:
                    if pd.isna(value):
                        cleaned_record[key] = 'N/A'
                    else:
                        cleaned_record[key] = str(value).strip()
            
            # Calculate NPM if we have NP Qtr and Sales Qtr
            np_qtr = cleaned_record.get('np_qtr', 'N/A')
            sales_qtr = cleaned_record.get('sales_qtr', 'N/A')
            if np_qtr != 'N/A' and sales_qtr != 'N/A':
                try:
                    np_val = float(np_qtr.replace(',', ''))
                    sales_val = float(sales_qtr.replace(',', ''))
                    if sales_val > 0:
                        npm_calc = (np_val / sales_val) * 100
                        cleaned_record['npm'] = f"{npm_calc:.2f}"
                except:
                    pass

            
            # Detect the Median / Industry Average row (e.g. "Median: 59 Co.")
            rec_name = cleaned_record.get('name', '')
            if rec_name.lower().startswith('median'):
                industry_avg = cleaned_record
                continue
            
            # Use the identified company_index to determine company vs peer
            if i == company_index:
                cleaned_record['ticker'] = ticker
                company_data = cleaned_record
            else:
                peers_data.append(cleaned_record)
        
        # Limit to 10 peers max
        peers_data = peers_data[:10]
        
        print(f"SUCCESS: Screener.in peer comparison fetched - 1 company + {len(peers_data)} peers" + 
              (f" + industry avg" if industry_avg else ""))
        return {
            'company': company_data,
            'peers': peers_data,
            'industry_avg': industry_avg
        }
        
    except Exception as e:
        print(f"ERROR: Failed to parse peer table: {e}")
        traceback.print_exc()
        return {'company': {}, 'peers': []}
