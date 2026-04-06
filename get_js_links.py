import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0"}
url = "https://www.screener.in/company/HDFCBANK/consolidated/"

response = requests.get(url, headers=HEADERS)
soup = BeautifulSoup(response.text, 'html.parser')

print("Script links:")
for script in soup.find_all('script'):
    if script.get('src'):
        print(script.get('src'))
