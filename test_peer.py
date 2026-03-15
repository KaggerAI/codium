from handler import app
from handler import run_batch_precache
from handler import cache
import pickle, zlib

def test_cache_and_swap():
    with app.app_context():
        ticker = 'TCS'
        print(f"\n--- Testing Light Cache Generation for {ticker} ---")
        
        # Clear existing cache
        cache_key = f"stock_analysis_{ticker}"
        cache.delete(cache_key)
        
        # Run precache
        run_batch_precache('test_job_1', [ticker])
        
        # Verify cache
        cached_blob = cache.get(cache_key)
        if not cached_blob:
            print("ERROR: Cache blob not found!")
            return
            
        cached_obj = pickle.loads(zlib.decompress(cached_blob))
        peer_comp = cached_obj.get('peer_comparison')
        
        print("\n--- Cache Verification ---")
        if peer_comp and peer_comp.get('peers'):
            print(f"SUCCESS: Found {len(peer_comp['peers'])} peers in light cache!")
            print(f"First peer: {peer_comp['peers'][0].get('name')}")
        else:
            print("ERROR: No peers found in light cache!")
            print(peer_comp)

if __name__ == "__main__":
    test_cache_and_swap()
