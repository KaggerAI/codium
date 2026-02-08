
import google.generativeai as genai
import sys

print(f"google-generativeai version: {genai.__version__}")
try:
    if hasattr(genai.types, 'ThinkingConfig'):
        print("ThinkingConfig IS available in genai.types")
    else:
        print("ThinkingConfig is NOT available in genai.types")
        
    # Check if GenerationConfig supports it
    import inspect
    sig = inspect.signature(genai.types.GenerationConfig)
    print(f"GenerationConfig params: {sig.parameters.keys()}")

except Exception as e:
    print(f"Error checking: {e}")
