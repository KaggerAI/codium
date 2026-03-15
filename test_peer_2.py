from screener_fetcher import fetch_consolidated

tables, desc, top_ratios, is_cons, peer_data = fetch_consolidated('TCS')
print("PEER DATA:")
print(peer_data)
