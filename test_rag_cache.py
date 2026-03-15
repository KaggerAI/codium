import json
import zlib
import pickle
from flask import Flask
from flask_caching import Cache

app = Flask(__name__)
app.config['CACHE_TYPE'] = 'FileSystemCache'
app.config['CACHE_DIR'] = '.cache'
app.config['CACHE_DEFAULT_TIMEOUT'] = 86400 * 30
cache = Cache(app)

with app.app_context():
    # Try RELIANCE
    tick = 'RELIANCE'
    stock_cache_key = f"stock_analysis_{tick}"
    cached_blob = cache.get(stock_cache_key)
    if cached_blob:
        if isinstance(cached_blob, bytes):
            cached_result = pickle.loads(zlib.decompress(cached_blob))
        else:
            cached_result = cached_blob
        
        print("Success extracting", tick, "cache!")
        print("Cache keys:", cached_result.keys())
        
        analysis_key = cached_result.get('analysis_key')
        is_light = cached_result.get('light_cache', False)
        print(f"Is Light Cache: {is_light}, Analysis Key: {analysis_key}")
        
        if analysis_key:
            analyst_texts = cache.get(f"{analysis_key}_analyst_texts")
            if analyst_texts:
                print("Found analyst_texts! Length:", len(str(analyst_texts)))
            else:
                print("No analyst_texts found in cache")
    else:
        print("No cache found for", tick)
