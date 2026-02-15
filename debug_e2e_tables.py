import requests
import pandas as pd
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def analyze_url(url, label):
    print(f"\n--- Analyzing {label}: {url} ---")
    try:
        resp = requests.get(url, headers=HEADERS, allow_redirects=True)
        print(f"Status: {resp.status_code}")
        print(f"Final URL: {resp.url}")
        
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Check for specific divs
        quarters = soup.select_one("#quarters")
        print(f"Has #quarters: {quarters is not None}")
        if quarters:
            tbl = quarters.select_one(".data-table")
            print(f"Has table in #quarters: {tbl is not None}")
            
        # Parse tables with pandas
        try:
            dfs = pd.read_html(resp.text)
            print(f"Pandas found {len(dfs)} tables.")
            for i, df in enumerate(dfs):
                print(f"  Table {i}: Shape={df.shape}, Columns={df.columns.tolist()[:3]}")
                if not df.empty:
                     print(f"  First row: {df.iloc[0].tolist()[:3]}")
        except ValueError as ve:
            print(f"Pandas read_html error: {ve}")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    # E2E Consolidated (The problematic one)
    analyze_url("https://www.screener.in/company/E2E/consolidated/", "E2E Consolidated")
    
    # E2E Standalone (The expected one)
    analyze_url("https://www.screener.in/company/E2E/", "E2E Standalone")
