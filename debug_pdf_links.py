"""Debug script to find where PDF URLs are in Trendlyne HTML"""
import httpx
import asyncio
from bs4 import BeautifulSoup
import re

async def debug_pdf_links():
    url = "https://trendlyne.com/research-reports/post/ADANIPORTS/27/"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
    
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url, headers=headers)
        print(f"Status: {response.status_code}")
        
    html = response.text
    soup = BeautifulSoup(html, 'html.parser')
    
    # Find all panels
    panels = soup.find_all('div', class_=lambda c: c and 'panel-post' in c)
    print(f"\nFound {len(panels)} panels\n")
    
    if not panels:
        print("No panels found! Let me check what's in the HTML...")
        print(f"HTML length: {len(html)} chars")
        print(f"Contains 'panel-post': {'panel-post' in html}")
        print(f"Contains 'get-document': {'get-document' in html}")
        return
    
    # Analyze first panel in detail
    panel = panels[0]
    print("=== First Panel Analysis ===\n")
    
    # 1. Check for /get-document/report/pdf/ links
    pdf_links = panel.find_all('a', href=lambda x: x and '/get-document/report/pdf/' in x)
    print(f"1. Links with '/get-document/report/pdf/': {len(pdf_links)}")
    for link in pdf_links:
        print(f"   href: {link.get('href')}")
    
    # 2. Check for any 'pdf' in href
    pdf_links2 = panel.find_all('a', href=lambda x: x and 'pdf' in x.lower())
    print(f"\n2. Links with 'pdf' anywhere in href: {len(pdf_links2)}")
    for link in pdf_links2:
        print(f"   href: {link.get('href')}")
    
    # 3. Check for /posts/ links (for fallback)
    post_links = panel.find_all('a', href=lambda x: x and '/posts/' in x)
    print(f"\n3. Links with '/posts/': {len(post_links)}")
    for link in post_links:
        href = link.get('href', '')
        print(f"   href: {href}")
        match = re.search(r'/posts/(\d+)/', href)
        if match:
            print(f"   -> Document ID: {match.group(1)}")
    
    # 4. Look for any link with title containing 'download'
    download_links = panel.find_all('a', title=lambda x: x and 'download' in x.lower())
    print(f"\n4. Links with title containing 'download': {len(download_links)}")
    for link in download_links:
        print(f"   title: {link.get('title')}")
        print(f"   href: {link.get('href')}")
    
    # 5. Print all links in the panel
    all_links = panel.find_all('a')
    print(f"\n5. ALL links in first panel ({len(all_links)} total):")
    for i, link in enumerate(all_links):
        href = link.get('href', 'None')[:70]
        print(f"   {i+1}. {href}")
    
    # 6. Search raw HTML for pdf patterns
    print("\n6. Searching raw HTML for '/get-document/' pattern:")
    matches = re.findall(r'/get-document/report/pdf/\d+/', html)
    print(f"   Found {len(matches)} matches: {matches[:5]}")

if __name__ == '__main__':
    asyncio.run(debug_pdf_links())
