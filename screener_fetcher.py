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
import google.generativeai as genai
from progress_logger import log_progress
import httpx
import asyncio
import pandas_market_calendars as mcal

# import os
# import openai

# --- Gemini API Configuration ---
# NOTE: Ensure GOOGLE_API_KEY is set as an environment variable
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
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

async def fetch_consolidated_async(ticker: str) -> tuple[dict[str, pd.DataFrame], str]:
    url = BASE_URL.format(ticker=ticker)
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=HEADERS, timeout=30.0)
        response.raise_for_status()
        text = response.text

    soup = BeautifulSoup(text, 'html.parser')
    description = ""
    try:
        about_p = soup.select_one(".company-info .about p")
        if about_p:
            description = about_p.get_text(strip=True)
    except Exception as e:
        log_progress(f"WARN: Could not parse company description for {ticker}. Reason: {e}")

    # pd.read_html is a blocking (non-async) function, so we run it in a separate thread.
    raw_tables = await asyncio.to_thread(pd.read_html, text)
    
    tables = {LABELS.get(i): clean_df(df) for i, df in enumerate(raw_tables, start=1) if LABELS.get(i)}
    series_list = []
    for p in PATTERNS:
        df = tables.pop(p, None)
        if df is not None and len(df.columns) >= 2:
            idx, val = df.columns[:2]
            s = df.set_index(idx)[val]
            s.name = p
            series_list.append(s)
    if series_list:
        merged = pd.concat(series_list, axis=1).T
        merged.index.name = ""
        tables["Growth Patterns"] = merged
    return tables, description

def fetch_consolidated(ticker: str) -> tuple[dict[str, pd.DataFrame], str]:
    
    url = BASE_URL.format(ticker=ticker)
    response = requests.get(url, headers=HEADERS)
    response.raise_for_status()
    text = response.text
    soup = BeautifulSoup(text, 'html.parser')

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

    return tables, description

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

async def fetch_chart_data_async(client: httpx.AsyncClient, company_id: int, query: str, days: int = 10000) -> dict:
    url = f"https://www.screener.in/api/company/{company_id}/chart/"
    resp = await client.get(url, params={"q": query, "days": days}, timeout=30.0)
    resp.raise_for_status()
    return resp.json()

def fetch_chart_data(company_id: int, query: str, days: int = 10000) -> dict:
    url = f"https://www.screener.in/api/company/{company_id}/chart/"
    resp = requests.get(url, params={"q": query, "days": days})
    resp.raise_for_status()
    return resp.json()

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

    # --- START: CORRECTED CODE TO FILL GAPS ON TRADING DAYS ONLY ---

    # 1. NORMALIZE THE INDEX: This is the crucial fix. It sets the time part
    #    of all timestamps to midnight (00:00:00), ensuring they can be matched
    #    with the clean dates from the market calendar.
    combined_df.index = combined_df.index.normalize() # <--- THE CRITICAL FIX

    # 2. Get the Indian (Bombay Stock Exchange) market calendar.
    bse = mcal.get_calendar('BSE')

    # 3. Determine the date range from your now-normalized data.
    start_date = combined_df.index.min()
    end_date = combined_df.index.max()

    # 4. Get a list of all valid trading days within that range.
    valid_trading_days = bse.valid_days(start_date=start_date, end_date=end_date)

    # 5. Re-index the DataFrame. This will now work correctly because the
    #    normalized dates in your data will match the dates in valid_trading_days.
    business_days_df = combined_df.reindex(valid_trading_days)

    # 6. Use linear interpolation to fill the NaN values.
    interpolated_df = business_days_df.interpolate(method='linear', limit_direction='both')

    return interpolated_df

async def get_text_from_pdf_url_async(pdf_url: str, max_pages_to_process=50, max_chars_to_return=10000) -> str:
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(pdf_url, headers=HEADERS, timeout=45.0)
            response.raise_for_status()
        
        pdf_file = BytesIO(response.content)

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

        return await asyncio.to_thread(blocking_pdf_extraction)
    except Exception as e:
        print(f"ERROR (async): Failed to get text from PDF URL {pdf_url}. Reason: {e}")
        return None

def get_text_from_pdf_url(pdf_url: str, max_pages_to_process=50, max_chars_to_return=10000) -> str:
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
        async with httpx.AsyncClient(follow_redirects=True) as client:
            print(f"Downloading presentation from {pdf_url} for Gemini analysis...")
            response = await client.get(pdf_url, headers=HEADERS, timeout=60.0)
            response.raise_for_status()
            pdf_content = response.content

        # Step 2: Define a synchronous function that handles ALL Gemini operations.
        def blocking_gemini_tasks(content):
            log_progress("Uploading PDF to Google AI File Service...")
            mime_type = mimetypes.guess_type(pdf_url)[0] or 'application/pdf'
            
            # This is a synchronous call
            pdf_file = genai.upload_file(
                path=BytesIO(content),
                display_name=pdf_url.split('/')[-1],
                mime_type=mime_type
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

            log_progress("Calling Gemini 2.5 Flash to extract insights from Investor Presentation...")
            model = genai.GenerativeModel('gemini-2.5-flash-lite') # Using the latest model
            
            # Use the SYNCHRONOUS version of the call inside this blocking function
            response = model.generate_content([prompt, pdf_file])

            # This is also a synchronous call
            genai.delete_file(pdf_file.name)
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
        pdf_file = genai.upload_file(
            path=BytesIO(pdf_content),
            display_name=pdf_url.split('/')[-1],
            mime_type=mime_type,
        )
        print(f"PDF uploaded successfully as '{pdf_file.name}'.")

        # CHANGED: The prompt is now much more detailed to ensure a comprehensive summary.
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

        log_progress("Calling Gemini 2.5 Flash to extract insights from Investor Presentation...")
        model = genai.GenerativeModel('gemini-2.5-flash-lite') # Using the latest model
        # model = genai.GenerativeModel('gemini-1.5-flash-latest')
        response = model.generate_content([prompt, pdf_file])

        genai.delete_file(pdf_file.name)
        print(f"Cleaned up uploaded file {pdf_file.name}.")

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
        
