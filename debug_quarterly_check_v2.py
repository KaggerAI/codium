import asyncio
import sys
from screener_fetcher import fetch_latest_quarter_header_async

# Redirect print to a file
log_file = open("debug_output_q.txt", "w", encoding="utf-8")
sys.stdout = log_file

async def debug_quarterly_check(ticker):
    print(f"Checking for {ticker}...")
    try:
        live_latest_q = await fetch_latest_quarter_header_async(ticker)
        print(f"[{ticker}] Live Latest Quarter Header: '{live_latest_q}'")
        print(f"[{ticker}] Length: {len(live_latest_q)}")
        print(f"[{ticker}] Type: {type(live_latest_q)}")
        
    except Exception as e:
        print(f"[{ticker}] Error: {e}")

if __name__ == "__main__":
    print("Starting debug script...")
    # Test with a few common tickers
    asyncio.run(debug_quarterly_check("RELIANCE"))
    asyncio.run(debug_quarterly_check("TCS"))
    asyncio.run(debug_quarterly_check("SBIN"))
    print("Debug script finished.")
    log_file.close()
