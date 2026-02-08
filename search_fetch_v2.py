import re
import os

def search_in_file(filename):
    if not os.path.exists(filename):
        return
    with open(filename, 'r', encoding='utf-8') as f:
        for i, line in enumerate(f, 1):
            if 'fetch_consolidated' in line and (line.strip().startswith('tables') or 'await' in line or '=' in line):
                 print(f"{filename}:{i}: {line.strip()}")

search_in_file('handler.py')
search_in_file('screener_fetcher.py')
