import requests
import json

def test_anonymous_limits():
    url = "http://127.0.0.1:8000/api/budget/chat"
    session = requests.Session() # To maintain budget_guest_count in session
    
    print("Testing Anonymous Chat Limits (Expect fail at 11th)...")
    
    for i in range(1, 12):
        payload = {"question": f"Question number {i}"}
        try:
            response = session.post(url, json=payload, timeout=30)
            print(f"Call {i}: Status {response.status_code}")
            
            if i <= 10:
                if response.status_code != 200:
                    print(f"FAILURE: Call {i} should have succeeded but failed with {response.text}")
                    return
            else:
                if response.status_code == 403:
                    print("SUCCESS: 11th call correctly blocked with 403.")
                    data = response.json()
                    print(f"Error Message: {data.get('error')}")
                else:
                    print(f"FAILURE: 11th call should have been blocked but returned {response.status_code}")
                    return
                    
        except Exception as e:
            print(f"ERROR: {e}")
            return

    print("\nVerification Complete.")

if __name__ == "__main__":
    # Ensure handler.py is running on port 8000
    test_anonymous_limits()
