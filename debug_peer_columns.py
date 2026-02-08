import asyncio
import sys
import pandas as pd
from screener_fetcher import fetch_peer_comparison_from_screener_async

# Configure pandas to show all columns
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 1000)

async def check_peer_columns(ticker):
    print(f"Fetching peer comparison for {ticker}...")
    try:
        data = await fetch_peer_comparison_from_screener_async(ticker)
        
        if not data or 'peers' not in data:
            print("No peer data returned.")
            return

        peers = data['peers']
        print(f"\nFound {len(peers)} peers.")
        
        if len(peers) > 0:
            print("\nKeys in first peer object:")
            print(peers[0].keys())
            
            print("\nSample Peer Data (first 3):")
            for i, p in enumerate(peers[:3]):
                print(f"Peer {i+1}: {p}")

    except Exception as e:
        print(f"Error fetching peers: {e}")

if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "PFC"
    asyncio.run(check_peer_columns(ticker))
