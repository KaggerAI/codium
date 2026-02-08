"""
Alternative PDF Summarizer using Browser Cookies

Since Trendlyne has strong bot detection, we can use cookies from your logged-in browser session.
"""

import httpx
import os
from io import BytesIO
from google import genai
from google.genai import types
import asyncio
import traceback
import json

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
genai_client = None
if GOOGLE_API_KEY:
    try:
        genai_client = genai.Client(api_key=GOOGLE_API_KEY)
    except Exception as e:
        print(f"ERROR: Failed to initialize Google GenAI client in pdf_summarizer_cookies.py: {e}")

# Store cookies here - loaded from env var or file
TRENDLYNE_COOKIES = {}


def load_cookies_from_file():
    """Load cookies from environment variable first, then from cookies.json file"""
    global TRENDLYNE_COOKIES
    
    # First, try to load from environment variable (for Azure)
    env_cookies = os.getenv("TRENDLYNE_COOKIES")
    if env_cookies:
        try:
            TRENDLYNE_COOKIES = json.loads(env_cookies)
            print(f"INFO: Loaded {len(TRENDLYNE_COOKIES)} cookies from TRENDLYNE_COOKIES environment variable")
            return
        except json.JSONDecodeError as e:
            print(f"WARNING: Failed to parse TRENDLYNE_COOKIES env var: {e}")
    
    # Fallback to JSON file (for local development)
    cookie_file = os.path.join(os.path.dirname(__file__), 'trendlyne_cookies.json')
    
    if os.path.exists(cookie_file):
        with open(cookie_file, 'r') as f:
            TRENDLYNE_COOKIES = json.load(f)
        print(f"INFO: Loaded {len(TRENDLYNE_COOKIES)} cookies from trendlyne_cookies.json file")
    else:
        print("WARNING: No cookies found.")


async def download_analyst_pdf_with_cookies(pdf_url: str) -> bytes:
    """Download PDF using browser cookies"""
    load_cookies_from_file()
    
    if not TRENDLYNE_COOKIES:
        raise Exception("No cookies configured. Please export cookies from your browser.")
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://trendlyne.com/',
    }
    
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0, cookies=TRENDLYNE_COOKIES) as client:
        print(f"INFO: Downloading PDF from {pdf_url}...")
        response = await client.get(pdf_url, headers=headers)
        response.raise_for_status()
        
        content_type = response.headers.get("content-type", "")
        if "pdf" not in content_type.lower():
            if "login" in str(response.url).lower():
                raise Exception("Cookies expired - please re-export cookies from browser")
            raise Exception(f"Expected PDF but got content-type: {content_type}")
        
        print(f"INFO: Downloaded PDF successfully ({len(response.content)} bytes)")
        return response.content


async def summarize_analyst_pdf_async(pdf_url: str) -> str:
    """Summarize analyst PDF using browser cookies"""
    if not GOOGLE_API_KEY:
        return "Error: Google API Key is not configured."
    
    try:
        pdf_content = await download_analyst_pdf_with_cookies(pdf_url)
        
        def blocking_gemini_tasks(content):
            global genai_client
            if not genai_client:
                raise ValueError("GenAI client not initialized in pdf_summarizer_cookies.py")
                
            print("INFO: Uploading PDF to Gemini...")
            pdf_file = genai_client.files.upload(
                file=BytesIO(content),
                config=types.UploadFileConfig(
                    display_name=pdf_url.split('/')[-2] + ".pdf",
                    mime_type="application/pdf"
                )
            )
            
            prompt = """You are an expert financial analyst. Analyze this brokerage research report and provide a comprehensive, well-structured summary.

**CRITICAL FORMATTING REQUIREMENTS:**
- Use markdown tables for ALL financial data and metrics
- Use headers (##, ###) for clear section organization
- Use bullet points for lists
- Use **bold** for important numbers and metrics
- Use > blockquotes for key analyst opinions
- For revenue, EBITDA, and PAT fugures, use crores for INR and millions for USD. Make conversions where required.

**Your output MUST include:**

## 📊 Investment Snapshot

| Metric | Value |
|--------|-------|
| **Recommendation** | BUY/SELL/HOLD |
| **Target Price** | ₹XXX |
| **Upside/Downside** | XX% |
| **Time Horizon** | X months |
| **Risk Rating** | Low/Medium/High |

---

## 💰 Financial Highlights

Create a table with quarterly/annual financial metrics:

| Period | Revenue (Cr) | EBITDA (Cr) | PAT (Cr) | Margins (%) | YoY Growth |
|--------|--------------|-------------|----------|-------------|------------|
| FY24 | | | | | |
| FY25E | | | | | |

**Key Ratios:**

| Ratio | Current | Target | Industry Avg |
|-------|---------|--------|--------------|
| P/E | | | |
| EV/EBITDA | | | |
| ROE | | | |
| ROCE | | | |

---

## 📈 Investment Thesis

> **Main Rationale:** [Quote the analyst's core thesis]

**Key Growth Drivers:**
- Point 1
- Point 2
- Point 3

**Catalysts for Re-rating:**
1. Near-term catalyst
2. Medium-term opportunity
3. Long-term structural advantage

---

## 🏢 Business & Operational Updates

- Recent performance highlights
- New orders/contracts/products
- Market share changes
- Management guidance

---

## 🌐 Industry & Competition

| Factor | Status | Impact |
|--------|--------|--------|
| Industry Growth | | Positive/Neutral/Negative |
| Competitive Position | | Strong/Moderate/Weak |
| Market Share Trend | | Gaining/Stable/Losing |

**Key Industry Trends:**
- Trend 1
- Trend 2

---

## ⚠️ Key Risks

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|------------|
| Risk 1 | High/Med/Low | Revenue/Margin/Both | What company is doing |
| Risk 2 | | | |

---

## 🎯 Valuation Analysis

**Valuation Method:**
- Primary method used (DCF/PE multiple/Sum of parts)
- Key assumptions
- Fair value derivation

**Sensitivity Analysis** (if mentioned):
- Best case scenario
- Base case
- Worst case

---

## 📝 Analyst's Final View

> **Key Quote:** [Direct quote from analyst summary]

**Investment Horizon:** X months/quarters
**Confidence Level:** Based on analyst tone
**Action:** Accumulate/Buy/Hold at current levels

---

**IMPORTANT:** Extract ACTUAL numbers from the PDF. If data is not available, write "Not disclosed". Use Indian number format (Crores, Lakhs) and ₹ symbol.
"""
            
            response = genai_client.models.generate_content(
                model='gemini-3-flash-preview', # Standardizing to the model used in handler.py and screens_fetcher
                contents=[
                    types.Part.from_text(text=prompt),
                    pdf_file
                ]
            )
            
            genai_client.files.delete(name=pdf_file.name)
            return response.text
        
        return await asyncio.to_thread(blocking_gemini_tasks, pdf_content)
        
    except Exception as e:
        print(f"ERROR: Failed to summarize PDF. Reason: {e}")
        traceback.print_exc()
        return f"Error: {str(e)}"


# For testing
if __name__ == '__main__':
    async def test():
        result = await summarize_analyst_pdf_async("https://trendlyne.com/get-document/report/pdf/91363/")
        print(result)
    
    asyncio.run(test())
