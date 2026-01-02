"""Test if Trendlyne PDFs are accessible without login"""
import httpx
import asyncio

async def test_pdf_access():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    }
    
    pdf_url = "https://trendlyne.com/get-document/report/pdf/91363/"
    
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        print(f"Testing PDF URL: {pdf_url}")
        response = await client.get(pdf_url, headers=headers)
        
        print(f"Status: {response.status_code}")
        print(f"Content-Type: {response.headers.get('content-type')}")
        print(f"Final URL: {response.url}")
        print(f"Content length: {len(response.content)} bytes")
        
        if 'pdf' in response.headers.get('content-type', '').lower():
            print("✅ PDF is accessible!")
            return True
        elif 'login' in str(response.url).lower():
            print("❌ Redirected to login page")
            return False
        else:
            print(f"⚠️ Unexpected response")
            print(f"First 200 chars of content: {response.text[:200]}")
            return False

if __name__ == '__main__':
    asyncio.run(test_pdf_access())
