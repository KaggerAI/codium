import asyncio
import sys
import os
import pandas as pd

sys.path.append(os.getcwd())
from screener_fetcher import fetch_consolidated_async

async def main():
    print("--- Verifying Data Fetch ---")
    
    # Check E2E (Standalone Only)
    print("\n[CHECK] E2E (Should fetch Standalone data)...")
    tables, desc, ratios, is_cons = await fetch_consolidated_async("E2E")
    print(f"Is Consolidated: {is_cons}")
    print(f"Table Keys: {list(tables.keys())}")
    
    if "Quarterly Results" in tables:
        df = tables["Quarterly Results"]
        print(f"Quarterly Results Rows: {len(df)}")
        if len(df) > 5:
            print("SUCCESS: E2E Quarterly Results found and populated.")
        else:
            print("FAILURE: E2E Quarterly Results empty or too small.")
    else:
        print("FAILURE: E2E Quarterly Results table MISSING.")

    # Check RELIANCE (Consolidated)
    print("\n[CHECK] RELIANCE (Should stay Consolidated)...")
    tables_r, desc_r, ratios_r, is_cons_r = await fetch_consolidated_async("RELIANCE")
    print(f"Is Consolidated: {is_cons_r}")
    if is_cons_r and "Quarterly Results" in tables_r:
         print("SUCCESS: RELIANCE identified as Consolidated and has data.")
    else:
         print("FAILURE: RELIANCE check failed.")

if __name__ == "__main__":
    asyncio.run(main())
