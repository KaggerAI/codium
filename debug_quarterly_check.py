import asyncio
import json
from screener_fetcher import fetch_latest_quarter_header_async

async def debug_quarterly_check(ticker):
    print(f"Checking for {ticker}...")
    try:
        live_latest_q = await fetch_latest_quarter_header_async(ticker)
        print(f"Live Latest Quarter Headers: '{live_latest_q}'")
        
        # Simulate what the cache logic does
        # Let's pretend we have a cached value from a previous quarter, e.g., "Sep 2024" if current is "Dec 2024"
        # Or just checking format compatibility
        
        cached_example = "Sep 2024" 
        print(f"Comparing with hypothetical cached value: '{cached_example}'")
        
        if live_latest_q and live_latest_q != cached_example:
            print("Logic says: NEW RESULTS FOUND (Correct)")
        else:
            print("Logic says: No new results (Incorrect if live is different)")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    # Test with a few common tickers likely to have recent results
    asyncio.run(debug_quarterly_check("RELIANCE"))
    asyncio.run(debug_quarterly_check("TCS"))
    asyncio.run(debug_quarterly_check("INFY"))
