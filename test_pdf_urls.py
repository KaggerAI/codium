import asyncio
import traceback

async def test():
    try:
        from analyst_reports.trendlyne_fetcher import fetch_analyst_reports_async
        
        result = await fetch_analyst_reports_async('ADANIPORTS')
        
        print(f"Total reports: {len(result)}")
        
        for i, r in enumerate(result[:3]):
            print(f"\nReport {i+1}:")
            print(f"  Brokerage: {r['brokerage']}")
            print(f"  PDF URL: {r.get('pdf_url')}")
            print(f"  Has PDF: {bool(r.get('pdf_url'))}")
            
    except Exception as e:
        print(f"ERROR: {e}")
        traceback.print_exc()

if __name__ == '__main__':
    asyncio.run(test())
