import asyncio
import httpx
from bs4 import BeautifulSoup
from io import BytesIO

# Mocking the function logic for testing
async def get_text_from_mock(url):
    print(f"Fetching {url}...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    async with httpx.AsyncClient(follow_redirects=True, verify=False) as client:
        response = await client.get(url, headers=headers, timeout=30.0)
        
        pdf_content = response.content
        content_type = response.headers.get('content-type', '').lower()
        is_pdf_signature = pdf_content.strip().startswith(b'%PDF')
        
        print(f"Content-Type: {content_type}")
        print(f"Is PDF Signature: {is_pdf_signature}")
        
        if 'html' in content_type or not is_pdf_signature:
            print("Detected HTML. Parsing...")
            soup = BeautifulSoup(response.text, 'html.parser')
            text = soup.get_text(separator=' ', strip=True)
            return text[:500] + "..." # Return first 500 chars

    return "Failed to detect HTML"

if __name__ == "__main__":
    text = asyncio.run(get_text_from_mock("https://www.crisil.com/mnt/winshare/Ratings/RatingList/RatingDocs/NetwebTechnologiesIndiaLimited_June%2020_%202025_RR_371340.html"))
    print("\n--- EXTRACTED TEXT ---\n")
    print(text)
