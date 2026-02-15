import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

def check(url, label):
    print(f"\n--- Checking {label} ---")
    try:
        resp = requests.get(url, headers=HEADERS)
        text = resp.text
        if "Quarterly Results" in text:
            print("Found 'Quarterly Results' string: YES")
        else:
            print("Found 'Quarterly Results' string: NO")
            
        soup = BeautifulSoup(text, 'html.parser')
        tbl = soup.select_one("#quarters .data-table")
        if tbl:
            print(f"Found #quarters .data-table: YES (Rows: {len(tbl.find_all('tr'))})")
        else:
            print("Found #quarters .data-table: NO")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check("https://www.screener.in/company/E2E/consolidated/", "E2E Consolidated")
    check("https://www.screener.in/company/E2E/", "E2E Standalone")
