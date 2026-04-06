import asyncio
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        # Intercept network requests
        api_requests = []
        page.on("request", lambda request: api_requests.append(request.url) if '/api/' in request.url or '/schedules/' in request.url or '/company/' in request.url else None)
        
        print("Navigating to Screener...")
        await page.goto("https://www.screener.in/company/HDFCBANK/consolidated/", wait_until="networkidle")
        
        print("Clicking Expenses+ button...")
        # Find the button that calls showSchedule for Expenses
        await page.evaluate("""
            const btns = Array.from(document.querySelectorAll('button'));
            const btn = btns.find(b => b.textContent.includes('Expenses'));
            if(btn) btn.click();
        """)
        
        # Wait for potential AJAX
        await page.wait_for_timeout(3000)
        
        print("Network Requests Intercepted:")
        for url in api_requests:
            if 'HDFCBANK' in url or 'api' in url or 'schedule' in url:
                print(url)
                
        await browser.close()

asyncio.run(run())
