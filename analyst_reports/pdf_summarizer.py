"""
Trendlyne PDF Summarizer

Handles authentication to Trendlyne and downloading analyst report PDFs
for AI summarization using Gemini.
"""

import httpx
import os
from io import BytesIO
import mimetypes
from google import genai
from google.genai import types
import asyncio
import traceback

# Load credentials from environment
TRENDLYNE_USERNAME = os.getenv("TRENDLYNE_USERNAME")
TRENDLYNE_PASSWORD = os.getenv("TRENDLYNE_PASSWORD")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

genai_client = None
if GOOGLE_API_KEY:
    try:
        genai_client = genai.Client(api_key=GOOGLE_API_KEY)
    except Exception as e:
        print(f"ERROR: Failed to initialize Google GenAI client in pdf_summarizer.py: {e}")

# Session storage (in-memory, will be lost on server restart)
_trendlyne_session = None
_session_lock = asyncio.Lock()


async def login_to_trendlyne() -> httpx.AsyncClient:
    """
    Logs into Trendlyne and returns an authenticated httpx.AsyncClient session.
    Reuses existing session if available.
    """
    global _trendlyne_session
    
    if not TRENDLYNE_USERNAME or not TRENDLYNE_PASSWORD:
        raise ValueError("Credentials not configured. Set USERNAME and PASSWORD environment variables.")
    
    # Browser-like headers to avoid 403
    HEADERS = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
    }
    
    async with _session_lock:
        # Check if existing session is still valid
        if _trendlyne_session:
            try:
                test_response = await _trendlyne_session.get("https://trendlyne.com/", headers=HEADERS, timeout=10.0)
                if test_response.status_code == 200 and "logout" in test_response.text.lower():
                    print("INFO: Reusing existing Trendlyne session")
                    return _trendlyne_session
            except:
                _trendlyne_session = None
        
        # Create new session
        print("INFO: Creating new Trendlyne session...")
        client = httpx.AsyncClient(follow_redirects=True, timeout=30.0, headers=HEADERS)
        
        try:
            # Step 1: Get main page to establish session cookies
            main_page = await client.get("https://trendlyne.com/")
            main_page.raise_for_status()
            
            # Step 2: Get login page 
            login_page = await client.get("https://trendlyne.com/login/", headers=HEADERS)
            
            # Extract CSRF token if present
            import re
            csrf_token = None
            csrf_match = re.search(r'name=["\']csrfmiddlewaretoken["\'][^>]*value=["\']([^"\']+)["\']', login_page.text)
            if csrf_match:
                csrf_token = csrf_match.group(1)
                print(f"INFO: Found CSRF token")
            
            # Step 3: Submit login form
            login_data = {
                "username": TRENDLYNE_USERNAME,  # Try 'username' key
                "email": TRENDLYNE_USERNAME,     # Also try 'email' key
                "password": TRENDLYNE_PASSWORD,
            }
            
            if csrf_token:
                login_data["csrfmiddlewaretoken"] = csrf_token
            
            login_headers = {
                **HEADERS,
                "Referer": "https://trendlyne.com/login/",
                "Origin": "https://trendlyne.com",
                "Content-Type": "application/x-www-form-urlencoded",
            }
            
            # Try different login endpoints
            login_endpoints = [
                "https://trendlyne.com/login/",
                "https://trendlyne.com/accounts/login/",
                "https://trendlyne.com/api/v1/login/",
            ]
            
            login_success = False
            for endpoint in login_endpoints:
                try:
                    login_response = await client.post(
                        endpoint,
                        data=login_data,
                        headers=login_headers
                    )
                    
                    # Check if login was successful
                    if login_response.status_code in [200, 302]:
                        # Verify by checking if we're now logged in
                        check_response = await client.get("https://trendlyne.com/", headers=HEADERS)
                        if "logout" in check_response.text.lower() or "my portfolio" in check_response.text.lower():
                            print(f"INFO: Successfully logged into Trendlyne via {endpoint}")
                            _trendlyne_session = client
                            login_success = True
                            break
                except Exception as e:
                    print(f"DEBUG: Login endpoint {endpoint} failed: {e}")
                    continue
            
            if not login_success:
                raise Exception("Login failed - could not authenticate with any login endpoint")
            
            return client
            
        except Exception as e:
            await client.aclose()
            raise



async def download_analyst_pdf_async(pdf_url: str) -> bytes:
    """
    Downloads an analyst PDF from Trendlyne using authenticated session.
    
    Args:
        pdf_url: Full URL to the PDF (e.g., https://trendlyne.com/get-document/report/pdf/91972/)
    
    Returns:
        PDF content as bytes
    """
    client = await login_to_trendlyne()
    
    print(f"INFO: Downloading analyst PDF from {pdf_url}...")
    response = await client.get(pdf_url, timeout=60.0)
    response.raise_for_status()
    
    # Verify it's actually a PDF
    content_type = response.headers.get("content-type", "")
    if "pdf" not in content_type.lower() and not pdf_url.endswith(".pdf"):
        # Might have been redirected to login page
        if "login" in response.text.lower():
            raise Exception("PDF download redirected to login - session may have expired")
        raise Exception(f"Expected PDF but got content-type: {content_type}")
    
    print(f"INFO: Successfully downloaded PDF ({len(response.content)} bytes)")
    return response.content


async def summarize_analyst_pdf_async(pdf_url: str) -> str:
    """
    Downloads analyst PDF from Trendlyne and summarizes it using Gemini.
    Similar to presentation summarization but tuned for analyst reports.
    
    Args:
        pdf_url: Full URL to the Trendlyne PDF
    
    Returns:
        AI-generated summary string
    """
    if not GOOGLE_API_KEY:
        return "Error: Google API Key is not configured."
    
    try:
        # Download PDF with authentication
        pdf_content = await download_analyst_pdf_async(pdf_url)
        
        # Define synchronous Gemini tasks (to avoid event loop conflicts)
        def blocking_gemini_tasks(content):
            global genai_client
            if not genai_client:
                raise ValueError("GenAI client not initialized in pdf_summarizer.py")
                
            print("INFO: Uploading analyst PDF to Google AI File Service...")
            mime_type = "application/pdf"
            
            pdf_file = genai_client.files.upload(
                file=BytesIO(content),
                config=types.UploadFileConfig(
                    display_name=pdf_url.split('/')[-2] + ".pdf",  # Use document ID as filename
                    mime_type=mime_type
                )
            )
            
            print(f"INFO: PDF uploaded successfully as '{pdf_file.name}'")
            
            prompt = """You are an expert financial analyst. Analyze this brokerage research report and provide a comprehensive summary.

**Your output should include:**

1. **Investment Thesis & Recommendation:**
   - Main investment rationale
   - Price target and upside potential
   - Risk-reward assessment

2. **Financial Highlights:**
   - Key financials: Revenue, EBITDA, PAT, margins (extract actual numbers with periods)
   - Growth rates (YoY, QoQ if mentioned)
   - Valuation metrics (P/E, EV/EBITDA, etc.)

3. **Business Developments:**
   - Recent operational performance
   - New orders, contracts, or product launches
   - Management guidance or commentary

4. **Industry & Competitive Landscape:**
   - Industry trends affecting the company
   - Competitive position
   - Market share insights

5. **Key Risks:**
   - Downside scenarios
   - Concerns or red flags mentioned

6. **Analyst's View:**
   - Detailed reasoning behind the recommendation
   - Catalysts for stock price movement
   - Time horizon for the target price

**Format:** Use markdown with clear sections. Include numbers and data points. Be thorough but structured."""

            print("INFO: Calling Gemini to summarize analyst report...")
            
            response = genai_client.models.generate_content(
                model='gemini-3-flash-preview',
                contents=[
                    types.Part.from_text(text=prompt),
                    pdf_file
                ]
            )
            
            genai_client.files.delete(name=pdf_file.name)
            print(f"INFO: Cleaned up uploaded file {pdf_file.name}")
            
            return response.text
        
        # Run Gemini processing in a separate thread
        return await asyncio.to_thread(blocking_gemini_tasks, pdf_content)
        
    except Exception as e:
        print(f"ERROR: Failed to summarize analyst PDF {pdf_url}. Reason: {e}")
        traceback.print_exc()
        return f"Error: Failed to analyze analyst report. {str(e)}"
