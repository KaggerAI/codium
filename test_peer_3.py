import bs4
from screener_fetcher import fetch_peer_comparison_from_screener

print("Fetching peers for TCS using full fetcher...")
peers = fetch_peer_comparison_from_screener('TCS')
print(peers)
