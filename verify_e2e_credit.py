import asyncio
import sys
import os

# Make sure we can import screener_fetcher
sys.path.append('c:\\Users\\harsh\\codium')
from screener_fetcher import fetch_forensic_documents_async

async def test():
    ticker = "E2E"
    print(f"Fetching forensic docs for {ticker}...")
    try:
        docs = await fetch_forensic_documents_async(ticker)
        
        print("\n--- RESULTS ---")
        ratings = docs.get('credit_ratings', [])
        print(f"Credit Ratings Found: {len(ratings)}")
        
        full_text_dump = ""
        
        for i, cr in enumerate(ratings):
            full_text_dump += f"\n\n=== RATING REPORT {i+1} ===\n"
            full_text_dump += f"Label: {cr.get('label')}\n"
            full_text_dump += f"Link: {cr.get('link')}\n"
            text = cr.get('text', '')
            full_text_dump += f"Text Length: {len(text)}\n"
            full_text_dump += "-" * 20 + "\n"
            full_text_dump += text
            full_text_dump += "\n" + "-" * 20 + "\n"
            
            print(f"\n[Rating {i+1}]")
            print(f"Label: {cr.get('label')}")
            print(f"Link: {cr.get('link')}")
            print(f"Text Length: {len(text)}")
            print(f"Preview (first 500 chars): {text[:500]}")
            
        with open("debug_e2e_credit_text.txt", "w", encoding="utf-8") as f:
            f.write(full_text_dump)
            
        print("\nFull extracted text saved to 'debug_e2e_credit_text.txt'")
            
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test())
