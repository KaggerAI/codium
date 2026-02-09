import requests
import os
import json

PARALLEL_API_KEY = "p0_2905273eb2eb4340a6b57950c77462c7" # From previous context/env if available, or I'll just try to get it from .env
RUN_ID = "trun_7e1a40da1a4d40c78c09901d2985ff90"

def test_stream():
    headers = {
        "x-api-key": PARALLEL_API_KEY,
        "Accept": "text/event-stream"
    }
    url = f"https://api.parallel.ai/v1beta/tasks/runs/{RUN_ID}/events"
    
    print(f"Connecting to {url}...")
    try:
        response = requests.get(url, headers=headers, stream=True, timeout=30)
        print(f"Status Code: {response.status_code}")
        for line in response.iter_lines():
            if line:
                print(f"RAW: {line.decode('utf-8')}")
            else:
                print("RAW: (empty line)")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_stream()
