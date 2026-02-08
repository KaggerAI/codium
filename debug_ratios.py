
import httpx
from bs4 import BeautifulSoup

def debug_ratios(ticker, mode):
    suffix = "consolidated/" if mode == "consolidated" else ""
    url = f"https://www.screener.in/company/{ticker}/{suffix}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    with httpx.Client() as client:
        resp = client.get(url, headers=headers)
        soup = BeautifulSoup(resp.text, 'html.parser')
        ratios_list = soup.select("#top-ratios li")
        print(f"{mode.capitalize()} Ratios for {ticker}:")
        for li in ratios_list:
            name_span = li.select_one(".name")
            value_span = li.select_one(".value")
            if name_span and value_span:
                print(f"  {name_span.get_text(strip=True)}: {value_span.get_text(strip=True)}")

if __name__ == "__main__":
    debug_ratios("INOXWIND", "consolidated")
    debug_ratios("INOXWIND", "standalone")
