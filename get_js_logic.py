import requests
import re
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0"}
url = "https://www.screener.in/company/HDFCBANK/consolidated/"

response = requests.get(url, headers=HEADERS)
soup = BeautifulSoup(response.text, 'html.parser')

for script in soup.find_all('script'):
    src = script.get('src')
    if src and ('screener.in' in src or src.startswith('/')):
        if src.startswith('/'):
            src = "https://www.screener.in" + src
            
        print(f"Fetching {src}...")
        try:
            js_resp = requests.get(src, headers=HEADERS)
            if 'showSchedule' in js_resp.text:
                print(f"FOUND in {src}!")
                # Get the function body roughly
                matches = re.finditer(r'showSchedule\s*[=:]\s*function.*?(?=,\w+:function|\};)', js_resp.text, re.DOTALL)
                for m in matches:
                    print(m.group(0)[:1000])
                    break
                
                # Also try another common pattern: showSchedule(e,t,i){...}
                matches2 = re.finditer(r'showSchedule\([^)]*\)\s*\{[^\}]+\}', js_resp.text, re.DOTALL)
                for m in matches2:
                    print(m.group(0)[:1000])
        except Exception as e:
            print(f"Error fetching {src}: {e}")
