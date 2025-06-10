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

def fetch_consolidated(ticker: str) -> tuple[dict[str, pd.DataFrame], str]:
    """
    MODIFIED: Now returns a third item: a dictionary containing DataFrames for
    segmental and geographical data if found.
    Return -> (financial_tables, description, segment_data)
    """
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
            print(f"INFO: Successfully scraped company description for {ticker}.")
        else:
            print(f"WARN: Could not find the description paragraph for {ticker}.")
    except Exception as e:
        print(f"WARN: Could not parse company description for {ticker}. Reason: {e}")

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

def get_company_id(ticker: str) -> int:
    url = "https://www.screener.in/api/company/search/"
    resp = requests.get(url, params={"q": ticker})
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"No company found for ticker '{ticker}'")
    return data[0]["id"]

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
    return pd.concat(dfs, axis=1)

def get_text_from_pdf_url(pdf_url: str, max_pages_to_process=20, max_chars_to_return=10000) -> str:
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
                if "question and answer session" in page_text.lower() or "moderator:" in page_text.lower():
                    print(f"INFO: Found start keyword on page {i+1}. Starting text extraction from here.")
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

def fetch_latest_documents(ticker: str) -> list[dict]:
    """
    MODIFIED: Now extracts text content from both the Concall Transcript
    and the Results Presentation PDF.
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
                    print(f"INFO: Fetching concall text for {ticker}...")
                    summary = get_text_from_pdf_url(doc_info['link'])
                    if summary:
                        doc_info['content_summary'] = summary
                    documents.append(doc_info)
                    found_types.add('Concall')

            # --- MODIFIED: Presentation (PPT) Logic ---
            if 'Presentation' not in found_types:
                ppt_link = item.find('a', string='PPT')
                if ppt_link and ppt_link.get('href'):
                    # Create the dictionary for the presentation document
                    doc_info = { 
                        "type": "Presentation", 
                        "text": f"Results Presentation {date_text}", 
                        "link": ppt_link['href'] 
                    }
                    # NEW: Call the PDF text extractor for the presentation link
                    print(f"INFO: Fetching presentation text for {ticker}...")
                    summary = get_text_from_pdf_url(doc_info['link'])
                    if summary:
                        # Add the extracted text to the dictionary
                        doc_info['content_summary'] = summary
                    
                    documents.append(doc_info)
                    found_types.add('Presentation')
                    
        return documents
    except Exception as e:
        print(f"ERROR: Could not fetch documents for {ticker}. Reason: {e}")
        traceback.print_exc()
        return []
