import requests
import re
import json

r = requests.get(
    'https://www.mcxindia.com/market-data/most-active-contracts',
    headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
    timeout=15
)

json_blocks = re.findall(r'var\s+\w+\s*=\s*(\[.*?\]);', r.text, re.DOTALL)
data = json.loads(json_blocks[0])  # Block 0 = "by value" — most liquid

print("Full records from Block 0 (most active by value):")
for rec in data:
    print(json.dumps(rec, indent=2))
    print()
