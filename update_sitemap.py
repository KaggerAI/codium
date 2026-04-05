import csv
import xml.etree.ElementTree as ET
import pandas as pd
import re

print("Loading existing CSV...")
csv_file = 'trendlyne_all_stocks_master.csv'
df = pd.read_csv(csv_file)
existing_tickers = set(df['Ticker'].dropna().tolist())

print(f"Loaded {len(existing_tickers)} existing tickers.")

print("Parsing sitemap.txt...")
tree = ET.parse('sitemap.txt')
root = tree.getroot()

ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}

urls = root.findall('sm:url/sm:loc', ns)
if not urls:
    # Handle if no namespace
    urls = root.findall('.//{http://www.sitemaps.org/schemas/sitemap/0.9}loc')

print(f"Found {len(urls)} URLs in sitemap.")

new_rows = []
for loc in urls:
    url = loc.text
    # https://trendlyne.com/equity/8/20MICRONS/20-microns-ltd/
    match = re.search(r'/equity/(\d+)/([^/]+)/([^/]+)/', url)
    if match:
        tid = match.group(1)
        ticker = match.group(2)
        slug = match.group(3)
        
        if ticker not in existing_tickers:
            # Format slug to Name
            name = " ".join([w.capitalize() for w in slug.split('-')])
            res_url = f"https://trendlyne.com/research-reports/stock/{tid}/{ticker}/"
            
            new_rows.append({
                'Stock Name': name,
                'Ticker': ticker,
                'Trendlyne ID': tid,
                'Research Report URL': res_url
            })

print(f"Found {len(new_rows)} new tickers to add.")

if new_rows:
    new_df = pd.DataFrame(new_rows)
    # Ensure columns match even if MarketCap exists
    for col in df.columns:
        if col not in new_df.columns:
            new_df[col] = None
    
    # Reorder columns to match original
    new_df = new_df[df.columns]
    
    df_combined = pd.concat([df, new_df], ignore_index=True)
    df_combined.to_csv(csv_file, index=False)
    print("CSV successfully updated.")
else:
    print("No new tickers to add.")
