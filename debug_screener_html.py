import requests
from bs4 import BeautifulSoup
import pandas as pd
from io import StringIO

HEADERS = {"User-Agent": "Mozilla/5.0"}
URL = "https://www.screener.in/company/PFC/"

def check_html_columns():
    print(f"Fetching {URL}...")
    try:
        response = requests.get(URL, headers=HEADERS)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.content, 'html.parser')
        
        peers_section = soup.select_one("#peers")
        if not peers_section:
            print("WARN: No #peers section found")
            return
        
        table = peers_section.select_one("table")
        if not table:
            print("WARN: No table found in #peers")
            return
            
        print("\n--- Table Headers (th text) ---")
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        print(headers)
        
        print("\n--- Pandas Read HTML Columns ---")
        df_list = pd.read_html(StringIO(str(table)))
        if df_list:
            df = df_list[0]
            print(df.columns.tolist())
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_html_columns()
