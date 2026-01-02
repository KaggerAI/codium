import asyncio
from analyst_reports.trendlyne_fetcher import fetch_analyst_reports_async

async def main():
    result = await fetch_analyst_reports_async('ADANIPORTS')
    print(f'Number of reports: {len(result)}\n')
    
    for r in result:
        print(f"=== {r['brokerage']} ===")
        print(f"Recommendation: {r['recommendation']}")
        print(f"Target: {r['target_price']}")
        print(f"Upside: {r['upside']}")
        print(f"Summary length: {len(r['summary'])} chars")
        print(f"Summary: {r['summary']}")
        print()

if __name__ == '__main__':
    asyncio.run(main())
