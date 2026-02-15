import requests
import time
import json
import sys

def test_forensic_fallback(ticker):
    url = "http://localhost:8000/agent/forensic/analyze"
    print(f"Testing Forensic Analysis Fallback for: {ticker}")
    print(f"Requesting: {url}")
    
    try:
        response = requests.post(url, json={"ticker": ticker}, timeout=120)
        print(f"Initial Response Code: {response.status_code}")
        
        if response.status_code == 200:
            res_json = response.json()
            if res_json.get('status') == 'processing':
                job_id = res_json.get('job_id')
                print(f"SUCCESS: Job started with ID: {job_id}")
                print("Now polling for status...")
                
                status_url = f"http://localhost:8000/agent/forensic/status/{job_id}"
                for i in range(60): # Poll for 10 minutes (10s intervals)
                    time.sleep(10)
                    try:
                        status_res = requests.get(status_url, timeout=10)
                        if not status_res.text:
                            print(f"[{i+1}/60] Empty response from status API")
                            continue
                        status_json = status_res.json()
                        
                        progress = status_json.get('progress', 'Unknown')
                        status = status_json.get('status', 'Unknown')
                        
                        print(f"[{i+1}/60] Status: {status} | Progress: {progress}")
                        
                        if status == 'complete':
                            print("\nSUCCESS: Forensic Analysis completed successfully!")
                            return True
                        elif status == 'error':
                            print(f"\nFAILED: Analysis error: {status_json.get('error')}")
                            # Check for specific Nan error that might cause JSON issues
                            return False
                    except Exception as poll_e:
                        print(f"[{i+1}/60] Poll failed: {poll_e}")
                        if hasattr(poll_e, 'response') and poll_e.response:
                             print(f"Response: {poll_e.response.text}")
            else:
                print(f"Unexpected response: {res_json}")
        else:
            print(f"Error Response: {response.text}")
            
    except Exception as e:
        print(f"Request failed: {e}")
    
    return False

if __name__ == "__main__":
    ticker = sys.argv[1] if len(sys.argv) > 1 else "NYKAA"
    test_forensic_fallback(ticker)
