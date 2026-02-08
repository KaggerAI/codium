
import os
from google import genai
from google.genai import types
from dotenv import load_dotenv
import time

load_dotenv()
api_key = os.getenv("GOOGLE_API_KEY")

def test_new_sdk():
    print("--- Starting GenAI v1.0 SDK Verification ---")
    client = genai.Client(api_key=api_key)
    
    # Test 1: Simple Chat
    print("\nTest 1: Simple Chat...")
    try:
        response = client.models.generate_content(
            model='gemini-3-flash-preview',
            contents="What is the capital of France?"
        )
        print(f"Success! Response: {response.text.strip()}")
    except Exception as e:
        print(f"FAILED Test 1: {e}")

    # Test 2: Thinking Mode (The main goal)
    print("\nTest 2: Thinking Mode (HIGH)...")
    try:
        start_time = time.time()
        response = client.models.generate_content(
            model='gemini-3-flash-preview',
            contents="Solve this riddle: I speak without a mouth and hear without ears. I have no body, but I come alive with wind. What am I?",
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="HIGH"),
                include_thoughts=True
            )
        )
        end_time = time.time()
        print(f"Success! (Took {end_time - start_time:.2f}s)")
        print(f"Response: {response.text.strip()[:100]}...")
        if hasattr(response, 'thoughts'):
             print(f"Thoughts found! Length: {len(str(response.thoughts))}")
    except Exception as e:
        print(f"FAILED Test 2: {e}")

    # Test 3: JSON Format
    print("\nTest 3: JSON Format...")
    try:
        response = client.models.generate_content(
            model='gemini-3-flash-preview',
            contents="List 3 stock sectors as JSON. Format: {'sectors': ['A', 'B', 'C']}",
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        print(f"Success! Response: {response.text.strip()}")
    except Exception as e:
        print(f"FAILED Test 3: {e}")

if __name__ == "__main__":
    if not api_key:
        print("Error: GOOGLE_API_KEY not found in .env")
    else:
        test_new_sdk()
