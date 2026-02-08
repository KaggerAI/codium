import asyncio
import sys
import pprint
from screener_fetcher import fetch_peer_comparison_from_screener_async

async def check_peer_columns(ticker):
    with open('debug_log.txt', 'w', encoding='utf-8') as f:
        f.write(f"Fetching peer comparison for {ticker}...\n")
        try:
            data = await fetch_peer_comparison_from_screener_async(ticker)
            
            if not data or 'peers' not in data:
                f.write("No peer data returned.\n")
                return

            peers = data['peers']
            f.write(f"\nFound {len(peers)} peers.\n")
            
            if len(peers) > 0:
                f.write("\n--- Keys in first peer object ---\n")
                keys = list(peers[0].keys())
                keys.sort()
                pprint.pprint(keys, stream=f)
                
                f.write("\n--- First Peer Object ---\n")
                pprint.pprint(peers[0], stream=f)

        except Exception as e:
            f.write(f"Error fetching peers: {e}\n")

if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "PFC"
    asyncio.run(check_peer_columns(ticker))
