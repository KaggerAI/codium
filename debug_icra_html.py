import asyncio
import httpx
from bs4 import BeautifulSoup

async def debug_icra_html():
    url = "https://www.icra.in/Rationale/ShowRationaleReport/?Id=139291"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    print(f"Fetching {url}...")
    async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
        try:
            response = await client.get(url, headers=headers, timeout=30.0)
            print(f"Status: {response.status_code}")
            
            soup = BeautifulSoup(response.text, 'html.parser')
            
            print("\n--- IFRAMES ---")
            for iframe in soup.find_all('iframe'):
                print(str(iframe))
                
            print("\n--- EMBEDS ---")
            for embed in soup.find_all('embed'):
                print(str(embed))
                
            print("\n--- DOWNLOAD LINKS ---")
            # Look for links that might download the PDF
            for a in soup.find_all('a', href=True):
                if 'download' in a.get_text().lower() or '.pdf' in a['href'].lower():
                    print(f"Text: {a.get_text(strip=True)} | Href: {a['href']}")
            
            # Save raw HTML just in case
            with open("debug_icra_raw.html", "w", encoding="utf-8") as f:
                f.write(response.text)
                
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(debug_icra_html())
