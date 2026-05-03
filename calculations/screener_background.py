import os
import json
import traceback
import pandas as pd
from datetime import datetime, timedelta
from calculations.pattern_detection import detect_all_patterns

def _enrich_with_patterns(df_chart, tick):
    """Run pattern detection and return serializable overlay list."""
    detected = detect_all_patterns(df_chart, tick)
    overlays = []
    for p in detected:
        overlays.append({
            'pattern': p['pattern'],
            'signal': p['signal'],
            'status': p.get('status', 'forming'),
            'confidence': round(p['confidence'], 2),
            'key_points': p['key_points'],
            'shapes': p['shapes'],
            'annotations': p['annotations'],
            'description': p['description']
        })
    return overlays


def execute_daily_screener_scan():
    """
    Independent background worker for executing the intensive Advanced Screener algorithm.
    It writes output forcefully to flat disk caches rather than yielding synchronous API responses.
    """
    import time
    
    # Safe lock to prevent duplicate concurrent runs in Azure
    lock_file = 'screener.lock'
    if os.path.exists(lock_file):
        try:
            with open(lock_file, 'r') as f:
                lock_time = float(f.read().strip())
            # If lock is older than 2 hours (7200 seconds), assume dead node crash and override
            if time.time() - lock_time > 7200:
                print("Screener scan found a STALE lock file from a previous crash. Overriding...")
            else:
                print("Screener scan is already running. Skipping duplicate execution.")
                return False
        except Exception:
            pass # Corrupt lock file, override
            
        
    try:
        # Create lock file
        with open(lock_file, 'w') as f:
            f.write(str(datetime.utcnow().timestamp()))
            
        print("Starting comprehensive background Market Scan...")
        from calculations.screener_calculations import run_momentum_screener, run_divergence_screener, enrich_market_caps, fetch_unified_market_data
        from calculations.tech_calculations import build_close_figure
        
        csv_path = 'trendlyne_all_stocks_master.csv'
        
        df_stocks = enrich_market_caps(csv_path)
        if df_stocks is None or df_stocks.empty:
            print("Failed to load stock universe.")
            return False
            
        total_stocks = len(df_stocks)
        
        # Sort by MarketCap descending and slice top 1200
        df_stocks = df_stocks.sort_values(by='MarketCap', ascending=False, na_position='last')
        top_1200_df = df_stocks.head(1200)
        tickers = top_1200_df['Ticker'].dropna().tolist()
        ticker_names = dict(zip(top_1200_df['Ticker'], top_1200_df['Stock Name']))
        
        ticker_sectors = {}
        if 'Sector Name' in top_1200_df.columns:
            ticker_sectors = dict(zip(top_1200_df['Ticker'], top_1200_df['Sector Name'].fillna('Unknown').astype(str)))
            
        ticker_industries = {}
        if 'Industry Name' in top_1200_df.columns:
            ticker_industries = dict(zip(top_1200_df['Ticker'], top_1200_df['Industry Name'].fillna('Unknown').astype(str)))
        
        # Start Pre-Loader — unified 10y data source feeds all three screeners
        print(f"Pre-Loading 10-year history internally into RAM for {len(tickers)} tickers...")
        preloaded_data = fetch_unified_market_data(tickers, period="10y")
        print("Pre-Load complete. Executing decoupled screeners...")
        
        # Run rigorous criteria screener
        matched_tickers = run_momentum_screener(tickers, preloaded_data=preloaded_data)
        
        results = []
        for tick in matched_tickers:
            company_name = ticker_names.get(tick, tick)
            sector_name = ticker_sectors.get(tick, 'Unknown')
            industry_name = ticker_industries.get(tick, 'Unknown')
                
            df = preloaded_data.get(tick)
            if df is None or df.empty or len(df) < 20: continue
            
            # Form clean chart
            df_chart = df.copy()
            if isinstance(df_chart['Close'], pd.DataFrame):
                df_chart['Close'] = df_chart['Close'].iloc[:, 0]
                df_chart['High'] = df_chart['High'].iloc[:, 0]
                df_chart['Low'] = df_chart['Low'].iloc[:, 0]
                df_chart['Volume'] = df_chart['Volume'].iloc[:, 0]
            
            df_chart['SMA20'] = df_chart['Close'].rolling(window=20).mean()
            chart_json = build_close_figure(df_chart.dropna(), company_name, years=1).to_json()
            
            # Pattern detection temporarily disabled
            # pattern_overlays = []
            # try:
            #     pattern_overlays = _enrich_with_patterns(df_chart, tick)
            # except Exception as e:
            #     print(f"Pattern detection error for {tick}: {e}")
            
            results.append({
                'ticker': tick,
                'company_name': company_name,
                'sector_name': sector_name,
                'industry_name': industry_name,
                'chart_json': chart_json
                # 'patterns': pattern_overlays
            })
            
        # Compile IST execution timestamp format natively so no library dependence
        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        timestamp_str = now_ist.strftime("%d %b, %I:%M %p").lower() # e.g. 03 apr, 04:00 pm
        timestamp_raw = now_ist.isoformat()
        
        final_payload = {
            'success': True,
            'total_scanned': total_stocks,
            'match_count': len(results),
            'results': results,
            'last_analyzed': timestamp_str,
            'last_analyzed_raw': timestamp_raw
        }
        
        # Atomically write to cache to prevent frontend collision
        temp_file = 'cached_screener_results.tmp.json'
        with open(temp_file, 'w') as f:
            json.dump(final_payload, f)
        os.replace(temp_file, 'cached_screener_results.json')
        
        print(f"Momentum Market Scan completely finalized. Found {len(results)} matches.")

        # =========================================================
        # PHASE 2: DIVERGENCE BOTTOMS SCREENER
        # =========================================================
        print("Starting parallel Divergence Bottoms Market Scan natively via shared memory...")
        
        matched_div_tickers = run_divergence_screener(tickers, preloaded_data=preloaded_data)
        div_results = []
        for tick in matched_div_tickers:
            company_name = ticker_names.get(tick, tick)
            sector_name = ticker_sectors.get(tick, 'Unknown')
            industry_name = ticker_industries.get(tick, 'Unknown')
            
            df = preloaded_data.get(tick)
            if df is None or df.empty or len(df) < 20: continue
            
            # Form clean chart
            df_chart = df.copy()
            if isinstance(df_chart['Close'], pd.DataFrame):
                df_chart['Close'] = df_chart['Close'].iloc[:, 0]
                df_chart['High'] = df_chart['High'].iloc[:, 0]
                df_chart['Low'] = df_chart['Low'].iloc[:, 0]
                df_chart['Volume'] = df_chart['Volume'].iloc[:, 0]
            
            df_chart['SMA20'] = df_chart['Close'].rolling(window=20).mean()
            chart_json = build_close_figure(df_chart.dropna(), company_name, years=1).to_json()
            
            # Pattern detection temporarily disabled
            # pattern_overlays = []
            # try:
            #     pattern_overlays = _enrich_with_patterns(df_chart, tick)
            # except Exception as e:
            #     print(f"Pattern detection error for {tick} (div): {e}")
            
            div_results.append({
                'ticker': tick,
                'company_name': company_name,
                'sector_name': sector_name,
                'industry_name': industry_name,
                'chart_json': chart_json
                # 'patterns': pattern_overlays
            })
            
        div_payload = {
            'success': True,
            'total_scanned': total_stocks,
            'match_count': len(div_results),
            'results': div_results,
            'last_analyzed': timestamp_str,
            'last_analyzed_raw': timestamp_raw
        }
        
        temp_div_file = 'cached_divergence_results.tmp.json'
        with open(temp_div_file, 'w') as f:
            json.dump(div_payload, f)
        os.replace(temp_div_file, 'cached_divergence_results.json')

        print(f"Divergence Bottoms Market Scan completely finalized. Found {len(div_results)} matches.")

        # =========================================================
        # PHASE 3: WYCKOFF SMA SCREENER
        # =========================================================
        print("Starting parallel Wyckoff Market Scan natively via shared memory...")
        from calculations.screener_calculations import run_wyckoff_screener, build_wyckoff_plotly_figure

        wyckoff_matches = run_wyckoff_screener(tickers, preloaded_data=preloaded_data)
        wyckoff_results = []
        for match in wyckoff_matches:
            tick = match['ticker']
            company_name = ticker_names.get(tick, tick)
            sector_name = ticker_sectors.get(tick, 'Unknown')
            industry_name = ticker_industries.get(tick, 'Unknown')
            
            # Use custom Plotly JSON renderer built for Wyckoff
            chart_json = build_wyckoff_plotly_figure(match['df_6y'], match['waves'], match['setup'], tick).to_json()
            
            wyckoff_results.append({
                'ticker': tick,
                'company_name': company_name,
                'sector_name': sector_name,
                'industry_name': industry_name,
                'chart_json': chart_json,
                'signal': match['reason'],
                'reason': match['reason']
            })
            
        wyckoff_payload = {
            'success': True,
            'total_scanned': total_stocks,
            'match_count': len(wyckoff_results),
            'results': wyckoff_results,
            'last_analyzed': timestamp_str,
            'last_analyzed_raw': timestamp_raw
        }
        
        temp_wyckoff_file = 'cached_wyckoff_results.tmp.json'
        with open(temp_wyckoff_file, 'w') as f:
            json.dump(wyckoff_payload, f)
        os.replace(temp_wyckoff_file, 'cached_wyckoff_results.json')

        print(f"Wyckoff Market Scan completely finalized. Found {len(wyckoff_results)} matches.")

        return True
        
    except Exception as e:
        print(f"CRITICAL FAULT IN BACKGROUND SCREENER: {str(e)}")
        traceback.print_exc()
        return False
        
    finally:
        # Guarantee lock cleanup
        try:
            if os.path.exists(lock_file):
                os.remove(lock_file)
        except:
            pass
