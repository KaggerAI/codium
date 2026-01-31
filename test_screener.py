import pandas as pd
import requests

url = 'https://www.screener.in/company/RELIANCE/consolidated/'
headers = {'User-Agent': 'Mozilla/5.0'}
response = requests.get(url, headers=headers)

try:
    tables = pd.read_html(response.text)
    print(f"Total tables found: {len(tables)}")
    
    # Let's look for table 9 (1-indexed, so index 8)
    if len(tables) >= 9:
        df = tables[8]
        print("\nTABLE 9 (Financial Ratios):")
        print(f"Index column: {df.columns[0]}")
        for idx, row in df.iterrows():
            print(f"Row {idx}: {row.iloc[0]}")
    else:
        print("\nLess than 9 tables found. Printing all table heads.")
        for i, df in enumerate(tables):
            print(f"\nTable {i}:")
            print(df.head(2))
except Exception as e:
    print(f"Error: {e}")
