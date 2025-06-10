import time
import json
import xml.etree.ElementTree as ET
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import re

def parse_xml_response(xml_string):
    """Parse XML response and extract analyst data"""
    try:
        root = ET.fromstring(xml_string)
        buy_count = 0
        sell_count = 0
        hold_count = 0
        
        for elem in root.iter():
            if elem.tag.lower() in ['buy', 'strong_buy']:
                buy_count = int(elem.text or 0)
            elif elem.tag.lower() in ['sell', 'strong_sell']:
                sell_count = int(elem.text or 0)
            elif elem.tag.lower() == 'hold':
                hold_count = int(elem.text or 0)
                
        return {
            'buy': buy_count,
            'sell': sell_count,
            'hold': hold_count,
            'reco_type': 'Hold'
        }
    except ET.ParseError as e:
        print(f"XML parsing error: {e}")
        return None

def scrape_analyst_data_from_html(driver, stock_code):
    """Extract analyst recommendations directly from the HTML page"""
    try:
        print("Attempting HTML scraping...")
        
        # Wait for page to fully load
        time.sleep(3)
        
        # Get page source
        page_source = driver.page_source
        
        # Method 1: Look for specific analyst recommendation elements
        analyst_data = {'buy': 0, 'sell': 0, 'hold': 0}
        
        # Try to find elements with analyst data
        try:
            # Look for common patterns in MoneyControl pages
            selectors_to_try = [
                "[data-testid*='analyst']",
                ".analyst-recommendation",
                ".broker-recommendation", 
                "[class*='analyst']",
                "[class*='recommendation']",
                "[id*='analyst']",
                "[id*='recommendation']"
            ]
            
            for selector in selectors_to_try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if elements:
                    print(f"Found elements with selector: {selector}")
                    for elem in elements:
                        text = elem.text.lower()
                        print(f"Element text: {text}")
                        
        except Exception as e:
            print(f"Element finding error: {e}")
        
        # Method 2: Pattern matching in page source
        page_text = page_source.lower()
        
        # Look for various patterns that might contain analyst data
        patterns = [
            r'(\d+)\s*analysts?\s*recommend\s*buy',
            r'(\d+)\s*analysts?\s*recommend\s*sell', 
            r'(\d+)\s*analysts?\s*recommend\s*hold',
            r'buy\s*:\s*(\d+)',
            r'sell\s*:\s*(\d+)',
            r'hold\s*:\s*(\d+)',
            r'"buy"\s*:\s*(\d+)',
            r'"sell"\s*:\s*(\d+)',
            r'"hold"\s*:\s*(\d+)',
            r'buy["\']?\s*:\s*["\']?(\d+)',
            r'sell["\']?\s*:\s*["\']?(\d+)',
            r'hold["\']?\s*:\s*["\']?(\d+)'
        ]
        
        for i, pattern in enumerate(patterns):
            matches = re.findall(pattern, page_text)
            if matches:
                print(f"Found pattern {i}: {pattern} -> {matches}")
                if i % 3 == 0:  # buy patterns
                    analyst_data['buy'] = int(matches[0])
                elif i % 3 == 1:  # sell patterns  
                    analyst_data['sell'] = int(matches[0])
                else:  # hold patterns
                    analyst_data['hold'] = int(matches[0])
        
        # Method 3: Look for JSON data in script tags
        try:
            script_tags = driver.find_elements(By.TAG_NAME, "script")
            for script in script_tags:
                script_content = script.get_attribute("innerHTML")
                if script_content and any(word in script_content.lower() for word in ['analyst', 'recommendation', 'buy', 'sell', 'hold']):
                    # Try to extract JSON data
                    json_matches = re.findall(r'\{[^{}]*(?:"buy"|"sell"|"hold")[^{}]*\}', script_content)
                    for json_match in json_matches:
                        try:
                            data = json.loads(json_match)
                            if 'buy' in data or 'sell' in data or 'hold' in data:
                                print(f"Found JSON data: {data}")
                                analyst_data.update({k: int(v) for k, v in data.items() if k in ['buy', 'sell', 'hold'] and str(v).isdigit()})
                        except:
                            continue
        except Exception as e:
            print(f"Script tag parsing error: {e}")
        
        # Method 4: Look for specific text patterns
        text_patterns = [
            r'(\d+)\s+buy\s+(\d+)\s+hold\s+(\d+)\s+sell',
            r'buy\s*(\d+).*?hold\s*(\d+).*?sell\s*(\d+)',
            r'(\d+).*?buy.*?(\d+).*?hold.*?(\d+).*?sell'
        ]
        
        for pattern in text_patterns:
            matches = re.findall(pattern, page_text)
            if matches:
                print(f"Found text pattern: {pattern} -> {matches}")
                match = matches[0]
                if len(match) == 3:
                    analyst_data['buy'] = int(match[0])
                    analyst_data['hold'] = int(match[1]) 
                    analyst_data['sell'] = int(match[2])
                    break
        
        print(f"Extracted analyst data: {analyst_data}")
        return analyst_data if any(analyst_data.values()) else None
        
    except Exception as e:
        print(f"HTML scraping error: {e}")
        return None

def get_analyst_recommendations(stock_code: str):
    """
    Fetches analyst recommendations using multiple methods:
    1. Fixed API call with correct content-type
    2. Comprehensive HTML scraping
    """
    
    # URL configurations - using the correct full URL format
    api_url = "https://www.moneycontrol.com/tech_charts/tech_chart_main/get_broker_recos.php"
    main_page_url = f"https://www.moneycontrol.com/india/stockpricequote/refineries/relianceindustries/{stock_code}"

    # Selenium Setup
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    service = ChromeService(ChromeDriverManager().install())
    driver = None
    
    try:
        driver = webdriver.Chrome(service=service, options=chrome_options)
        
        # Step 1: Visit the main page
        print(f"Visiting: {main_page_url}")
        driver.get(main_page_url)
        time.sleep(5)
        
        # Step 2: Try API call with corrected content-type
        print(f"Attempting API call with multipart/form-data...")
        
        js_script = f"""
            const callback = arguments[arguments.length - 1];
            
            // Create FormData for multipart/form-data
            const formData = new FormData();
            formData.append('sc_id', '{stock_code}');
            formData.append('dur', '12M');
            
            fetch("{api_url}", {{
                "method": "POST",
                "body": formData,
                "credentials": "same-origin"
            }})
            .then(response => {{
                console.log('Response status:', response.status);
                console.log('Response type:', response.headers.get('content-type'));
                return response.text();
            }})
            .then(data => {{
                console.log('Raw response length:', data.length);
                console.log('Raw response start:', data.substring(0, 200));
                callback({{ 
                    "raw_response": data, 
                    "status": "success",
                    "response_type": data.trim().startsWith('<') ? 'xml' : 'json'
                }});
            }})
            .catch(error => {{
                console.log('Fetch error:', error);
                callback({{ "error": error.toString() }});
            }});
        """
        
        # Execute the API call
        result = driver.execute_async_script(js_script)
        
        reco_data = None
        
        if "error" not in result and result.get('raw_response'):
            raw_response = result.get('raw_response', '').strip()
            print(f"API Response preview: {raw_response[:300]}...")
            
            # Parse response
            if result.get('response_type') == 'json' or raw_response.startswith('{'):
                try:
                    reco_data = json.loads(raw_response)
                    print("Successfully parsed JSON response")
                except json.JSONDecodeError:
                    print("JSON parsing failed")
                    
            elif not raw_response.startswith('<?xml') or 'Error' not in raw_response:
                # Try to parse if it's not an error XML
                try:
                    reco_data = parse_xml_response(raw_response)
                except:
                    pass
        
        # Step 3: HTML scraping fallback (this is more likely to work)
        if not reco_data:
            print("API call unsuccessful, using HTML scraping...")
            html_data = scrape_analyst_data_from_html(driver, stock_code)
            if html_data:
                reco_data = html_data
                reco_data['reco_type'] = 'Hold'
        
        # Step 4: Process the data
        if reco_data and isinstance(reco_data, dict):
            buy_count = int(reco_data.get('buy', 0))
            sell_count = int(reco_data.get('sell', 0))
            hold_count = int(reco_data.get('hold', 0))
            total_analysts = buy_count + sell_count + hold_count
            
            if total_analysts == 0:
                # Try one more time with a different approach
                print("No data found, trying alternative page inspection...")
                
                # Print page title and some content for debugging
                print(f"Page title: {driver.title}")
                
                # Look for any numbers that might be analyst counts
                all_numbers = re.findall(r'\b\d+\b', driver.page_source)
                print(f"Found numbers on page: {all_numbers[:20]}")  # First 20 numbers
                
                return {
                    "source": "moneycontrol.com",
                    "ticker": stock_code,
                    "summary_text": "No analyst recommendations found. The page structure may have changed.",
                    "recommendations": {},
                    "debug_info": {
                        "page_title": driver.title,
                        "numbers_found": all_numbers[:10]
                    }
                }

            # Calculate percentages
            buy_percent = round((buy_count / total_analysts) * 100) if total_analysts > 0 else 0
            sell_percent = round((sell_count / total_analysts) * 100) if total_analysts > 0 else 0
            hold_percent = round((hold_count / total_analysts) * 100) if total_analysts > 0 else 0
            
            # Determine consensus
            max_count = max(buy_count, sell_count, hold_count)
            if buy_count == max_count:
                consensus = "Buy"
            elif sell_count == max_count:
                consensus = "Sell"
            else:
                consensus = "Hold"
            
            summary_text = f"{total_analysts} analysts have given their recommendations, with {buy_percent}% recommending to Buy."

            recommendation_data = {
                "source": "moneycontrol.com",
                "ticker": stock_code,
                "summary_text": summary_text,
                "recommendations": {
                    "buy_percent": buy_percent,
                    "sell_percent": sell_percent,
                    "hold_percent": hold_percent,
                    "buy_count": buy_count,
                    "sell_count": sell_count,
                    "hold_count": hold_count,
                    "consensus": consensus,
                    "total_analysts": total_analysts
                }
            }
            return recommendation_data
        else:
            print("No valid recommendation data found")
            return None

    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        if driver:
            print("Closing browser...")
            driver.quit()

# Alternative function to inspect the page structure
def inspect_page_structure(stock_code: str):
    """Helper function to inspect the page and find where analyst data is located"""
    
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    
    service = ChromeService(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    
    try:
        url = f"https://www.moneycontrol.com/india/stockpricequote/refineries/relianceindustries/{stock_code}"
        print(f"Inspecting page: {url}")
        driver.get(url)
        time.sleep(5)
        
        print(f"Page title: {driver.title}")
        
        # Look for elements that might contain analyst data
        keywords = ['analyst', 'recommendation', 'buy', 'sell', 'hold', 'consensus', 'rating']
        
        for keyword in keywords:
            elements = driver.find_elements(By.XPATH, f"//*[contains(text(), '{keyword}')]")
            if elements:
                print(f"\nFound elements containing '{keyword}':")
                for i, elem in enumerate(elements[:3]):  # Limit to first 3
                    try:
                        print(f"  {i+1}. Tag: {elem.tag_name}, Text: {elem.text[:100]}")
                        print(f"     Class: {elem.get_attribute('class')}")
                        print(f"     ID: {elem.get_attribute('id')}")
                    except:
                        continue
                        
        # Look for tables that might contain the data
        tables = driver.find_elements(By.TAG_NAME, "table")
        print(f"\nFound {len(tables)} tables on the page")
        
        for i, table in enumerate(tables[:5]):  # Check first 5 tables
            try:
                table_text = table.text.lower()
                if any(word in table_text for word in ['analyst', 'buy', 'sell', 'hold']):
                    print(f"Table {i+1} contains analyst-related text:")
                    print(f"  Text preview: {table.text[:200]}")
            except:
                continue
    
    finally:
        driver.quit()

# Main execution
if __name__ == "__main__":
    print("=== Enhanced Moneycontrol Analyst Recommendations Scraper ===\n")
    
    stock_code = "RI"
    
    # First, inspect the page structure
    print("Step 1: Inspecting page structure...")
    inspect_page_structure(stock_code)
    
    print(f"\nStep 2: Fetching analyst recommendations for {stock_code}...")
    recommendations = get_analyst_recommendations(stock_code)
    
    if recommendations:
        print("\n🎉 Successfully retrieved recommendations!")
        print("="*50)
        print(json.dumps(recommendations, indent=4))
    else:
        print("\n❌ Failed to retrieve recommendations")
        print("\nTroubleshooting suggestions:")
        print("1. The page structure may have changed")
        print("2. Try using the page inspector function above to find the correct elements")
        print("3. Check if the stock symbol is correct")
        print("4. The analyst data might be loaded via a different API endpoint")
