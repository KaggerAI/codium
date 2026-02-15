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
    # No changes needed in this function
    df = df.copy()
    if df.columns.nlevels > 1:
        df.columns = ['_'.join(col).strip() for col in df.columns.values]
    else:
        df.columns = df.columns.str.strip().str.replace("Unnamed: 0", "", regex=False)
    for col in df.select_dtypes(include="object"):
        df[col] = df[col].str.strip().str.replace("+", "", regex=False)
    if df.index.dtype == object:
        df.index = df.index.astype(str).str.strip().str.replace("+", "", regex=False)
    return df

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
        "Quarterly Shareholding Pattern": "#shareholding",
    }

    for label, selector in SECTIONS_TO_FIND.items():
        section_div = soup.select_one(selector)
        if section_div:
            # Find the actual data table within the section
            table_html = section_div.select_one(".data-table")
            if table_html:
                try:
                    df_list = await asyncio.to_thread(pd.read_html, str(table_html))
                    if df_list:
                        tables[label] = clean_df(df_list[0])
                except Exception as e:
                    log_progress(f"Could not parse table for '{label}' for {ticker}. Error: {e}")
            else:
                log_progress(f"Found section for '{label}' but no data-table inside for {ticker}.")
        else:
             log_progress(f"Could not find section with selector '{selector}' for {ticker}.")

    # The old "Growth Patterns" came from tables we are no longer using.
    # The primary financial tables are the priority and are now correctly fetched.

    return tables, description, top_ratios, is_consolidated

async def fetch_latest_quarter_header_async(ticker: str) -> str:
    """
    Fast fetch of the latest quarterly result column header (e.g., 'Dec 2023').
    Used for cache validity checking.
    """
    standalone_url = f"https://www.screener.in/company/{ticker}/"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(standalone_url, headers=HEADERS, timeout=15.0)
            if response.status_code != 200:
                return ""
            text = response.text
        except Exception:
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

def fetch_consolidated(ticker: str) -> tuple[dict[str, pd.DataFrame], str, dict, bool]:
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
    raw = pd.read_html(text)
    tables = {LABELS.get(i): clean_df(df) for i, df in enumerate(raw, start=1) if LABELS.get(i)}

    series_list = []
    for p in PATTERNS:
        df = tables.pop(p, None)
        if df is not None:
            # Handle cases where the table might not have 2 columns
            if len(df.columns) >= 2:
                idx, val = df.columns[:2]
                s = df.set_index(idx)[val]
                s.name = p
                series_list.append(s)
    if series_list:
        merged = pd.concat(series_list, axis=1).T
        merged.index.name = ""
        tables["Growth Patterns"] = merged

    return tables, description, top_ratios, is_consolidated

async def get_company_id_async(ticker: str) -> int:
    url = "https://www.screener.in/api/company/search/"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params={"q": ticker}, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    if not data: raise ValueError(f"No company found for ticker '{ticker}'")
    return data[0]["id"]

def get_company_id(ticker: str) -> int:
    url = "https://www.screener.in/api/company/search/"
    resp = requests.get(url, params={"q": ticker})
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
            date_text = f"({date_element.text.strip()})" if date_element else ""

            if 'Concall' not in found_types:
                transcript_link = item.find('a', string='Transcript', href=True)
                if transcript_link:
                    doc_info = {"type": "Concall", "text": f"Concall Transcript {date_text}", "link": transcript_link['href']}
                    tasks_to_run.append(get_text_from_pdf_url_async(doc_info['link']))
                    doc_infos.append(doc_info)
                    found_types.add('Concall')

            if 'Presentation' not in found_types:
                ppt_link = item.find('a', string='PPT', href=True)
                if ppt_link:
                    doc_info = {"type": "Presentation", "text": f"Results Presentation {date_text}", "link": ppt_link['href']}
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
        
        return final_docs
    except Exception as e:
        print(f"ERROR (async): Could not fetch documents for {ticker}. Reason: {e}")
        return []


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

            
            # Use the identified company_index to determine company vs peer
            if i == company_index:
                cleaned_record['ticker'] = ticker
                company_data = cleaned_record
            else:
                peers_data.append(cleaned_record)
        
        # Limit to 10 peers max
        peers_data = peers_data[:10]
        
        print(f"SUCCESS: Screener.in peer comparison fetched - 1 company + {len(peers_data)} peers")
        return {
            'company': company_data,
            'peers': peers_data
        }
        
    except Exception as e:
        print(f"ERROR: Failed to parse peer table: {e}")
        traceback.print_exc()
        return {'company': {}, 'peers': []}
