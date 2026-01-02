import httpx
import asyncio
from bs4 import BeautifulSoup

async def test_pdf_extraction():
    url = "https://trendlyne.com/research-reports/post/ADANIPORTS/27/"
    
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
    
    soup = BeautifulSoup(response.text, 'html.parser')
    
    # Find first panel
    panels = soup.find_all('div', class_=lambda c: c and 'panel-post' in c)
    print(f"Found {len(panels)} panels\n")
    
    for i, panel in enumerate(panels[:3]):
        print(f"=== Panel {i+1} ===")
        
        # Try to find PDF link
        pdf_link = panel.find('a', href=lambda x: x and '/get-document/report/pdf/' in x)
        print(f"PDF Link found: {pdf_link is not None}")
        
        if pdf_link:
            print(f"PDF href: {pdf_link.get('href')}")
            print(f"PDF HTML: {pdf_link}")
        else:
            # Show all links in this panel
            all_links = panel.find_all('a')
            print(f"All links in panel ({len(all_links)} total):")
            for link in all_links[:5]:
                href = link.get('href', '')
                print(f"  - {href[:80]}")
        print()

if __name__ == '__main__':
    asyncio.run(test_pdf_extraction())
