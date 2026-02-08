
import google.generativeai as genai
import os
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GOOGLE_API_KEY"))

model = genai.GenerativeModel("gemini-3-flash-preview")

print("Testing thinking mode with dict...")
try:
    # Try passing thinking_config in a dictionary
    config = {
        "temperature": 1,
        "thinking_config": {"include_thoughts": True}
    }
    response = model.generate_content("What is 2+2? Think step by step.", generation_config=config)
    print("Success!")
    print(response.text)
except Exception as e:
    print(f"Failed with dict: {e}")

print("\nTesting thinking mode in separate kwarg...")
try:
    response = model.generate_content("What is 2+2?", thinking_config={"include_thoughts": True})
    print("Success!")
except Exception as e:
    print(f"Failed with kwarg: {e}")
