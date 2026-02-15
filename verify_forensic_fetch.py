import asyncio
import sys
# Make sure we can import screener_fetcher
sys.path.append('c:\\Users\\harsh\\codium')
from screener_fetcher import fetch_forensic_documents_async

async def test():
    ticker = "NETWEB"
    print(f"Fetching forensic docs for {ticker}...")
    try:
        docs = await fetch_forensic_documents_async(ticker)
        
        print("\n--- RESULTS ---")
        ratings = docs.get('credit_ratings', [])
        print(f"Credit Ratings Found: {len(ratings)}")
        
        for i, cr in enumerate(ratings):
            print(f"\n[Rating {i+1}]")
            print(f"Label: {cr.get('label')}")
            print(f"Link: {cr.get('link')}")
            text = cr.get('text', '')
            print(f"Text Length: {len(text)}")
            print(f"Preview (first 200 chars): {text[:200].replace(chr(10), ' ')}")  # Replace newlines for cleaner output
            if not text:
                print("WARNING: No text extracted!")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test())
