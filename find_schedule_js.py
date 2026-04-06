import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}
url = "https://www.screener.in/company/HDFCBANK/consolidated/"

response = requests.get(url, headers=HEADERS)
html = response.text

if 'showSchedule' in html:
    print("FOUND inside main HTML!")
    
    # Let's extract the rough surrounding text
    idx = html.find('Company.showSchedule')
    print("AROUND Company.showSchedule:")
    print(html[max(0, idx-100) : min(len(html), idx+300)])
else:
    print("NOT found inside main HTML.")
