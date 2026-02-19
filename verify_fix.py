import asyncio
import sys
from screener_fetcher import fetch_latest_quarter_header_async

# Redirect print to a file
log_file = open("verify_fix_output.txt", "w", encoding="utf-8")
sys.stdout = log_file

async def verify_fix(ticker):
    print(f"Verifying for {ticker}...")
    try:
        # Check Standalone
        standalone_q = await fetch_latest_quarter_header_async(ticker, consolidated=False)
        print(f"[{ticker}] Standalone Latest Quarter: '{standalone_q}'")
        
        # Check Consolidated
        consolidated_q = await fetch_latest_quarter_header_async(ticker, consolidated=True)
        print(f"[{ticker}] Consolidated Latest Quarter: '{consolidated_q}'")
        
        if standalone_q and consolidated_q:
            print(f"SUCCESS: Successfully fetched both. Standalone='{standalone_q}', Consolidated='{consolidated_q}'")
        elif not standalone_q and not consolidated_q:
            print("WARN: Fetched nothing for both.")
        else:
            print("INFO: One mode returned data, the other didn't (or they matched).")
            
    except Exception as e:
        print(f"[{ticker}] Error: {e}")

if __name__ == "__main__":
    print("Starting verification script...")
    # Test with a few common tickers
    asyncio.run(verify_fix("RELIANCE")) # Usually has both
    asyncio.run(verify_fix("TCS"))      # Usually has both
    print("Verification finished.")
    log_file.close()
