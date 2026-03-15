from screener_fetcher import fetch_consolidated

tables, desc, top_ratios, is_cons, peer_data = fetch_consolidated('NH')
print("PEER DATA NH:")
if peer_data and 'peers' in peer_data:
    print(len(peer_data['peers']))
else:
    print("None")
