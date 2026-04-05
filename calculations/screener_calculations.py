import yfinance as yf
import pandas as pd
import numpy as np
import traceback

def chunk_list(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]

def fetch_unified_market_data(tickers, period="2y", progress_callback=None):
    """
    Downloads historical data for all tickers once and stores them natively in a RAM dictionary.
    Used by the rigorous background workers to run multiple independent analytical algorithms
    simultaneously locally without continuously draining yfinance and slowing down execution.
    """
    history_dict = {}
    chunks = list(chunk_list(tickers, 300))
    total_processed = 0
    total = len(tickers)
    
    for chunk_idx, chunk in enumerate(chunks):
        ns_tickers = [f"{t}.NS" for t in chunk]
        if progress_callback:
            progress_callback(f"Downloading unified data chunk {chunk_idx + 1}/{len(chunks)}...", total_processed, total)
            
        try:
            data = yf.download(ns_tickers, period=period, group_by="ticker", threads=True, progress=False)
            total_processed += len(chunk)
            
            if len(ns_tickers) == 1:
                history_dict[chunk[0]] = data
            else:
                for idx, t in enumerate(chunk):
                    ns_t = ns_tickers[idx]
                    if isinstance(data.columns, pd.MultiIndex):
                        if ns_t in data.columns.get_level_values(0):
                            history_dict[t] = data[ns_t]
                    elif not data.empty and 'Close' in data.columns:
                        history_dict[t] = data
        except Exception as e:
            traceback.print_exc()
            continue
            
    return history_dict

def run_momentum_screener(tickers, progress_callback=None, preloaded_data=None):
    """
    Evaluates a list of tickers based on Short-Term Momentum Swing Screener criteria.
    Criteria:
    1. 20-day Average Daily Traded Value > ₹5 Crores
    2. 50-day Average Daily Traded Value > ₹5 Crores
    3. 20-day SMA NOT lower than 50-day SMA for the last 20 consecutive days.
    4. REJECTION: stock current close < 50-day SMA AND its 50-day SMA sloping downwards for 20 consecutive days.
    5. Close > 10-day SMA and 20-day SMA
    6. 10-day SMA > 20-day SMA
    7. Close > Yesterday's Close
    8. ATR(1) > 60% of ATR(20)
    9. Close > Daily Low + [(Daily High - Daily Low) * 0.40]
    """
    matched_tickers = []
    failed_tickers = []
    
    if preloaded_data is not None:
        # Utilize Shared Architecture Dictionary locally
        tickers_data = [(t, preloaded_data.get(t)) for t in tickers if t in preloaded_data]
        chunks_loop = [tickers_data] # Create a single artificial chunk to reuse the exact existing inner logic cleanly
    else:
        # Fall-back legacy independent behavior
        chunks = list(chunk_list(tickers, 500))
        total_processed = 0
        chunks_loop = []
        for chunk_idx, chunk in enumerate(chunks):
            ns_tickers = [f"{t}.NS" for t in chunk]
            try:
                if progress_callback:
                    progress_callback(f"Downloading data for chunk {chunk_idx + 1}/{len(chunks)}...", total_processed, len(tickers))
                data = yf.download(ns_tickers, period="4mo", group_by="ticker", threads=True, progress=False)
                total_processed += len(chunk)
                
                if len(ns_tickers) == 1:
                    chunks_loop.append([(chunk[0], data)])
                else:
                    t_data = []
                    for idx, t in enumerate(chunk):
                        ns_t = ns_tickers[idx]
                        if isinstance(data.columns, pd.MultiIndex):
                            if ns_t in data.columns.get_level_values(0):
                                t_data.append((t, data[ns_t]))
                        elif not data.empty and 'Close' in data.columns:
                            t_data.append((t, data))
                    chunks_loop.append(t_data)
            except Exception as e:
                continue

    for tickers_data in chunks_loop:
        for tick, df in tickers_data:
            try:
                if df is None or df.empty or len(df) < 200:
                    continue
                        
                # Drop NaN rows
                df = df.dropna(subset=['Close', 'Volume', 'High', 'Low'])
                if len(df) < 200:
                    continue
                        
                # Extract Series (using .squeeze() or [column] to ensure 1D arrays cleanly)
                close = df['Close']
                if isinstance(close, pd.DataFrame):
                    close = close.iloc[:, 0]
                high = df['High']
                if isinstance(high, pd.DataFrame):
                    high = high.iloc[:, 0]
                low = df['Low']
                if isinstance(low, pd.DataFrame):
                    low = low.iloc[:, 0]
                volume = df['Volume']
                if isinstance(volume, pd.DataFrame):
                    volume = volume.iloc[:, 0]
                
                # 1 & 2: ADTV
                traded_value = volume * close
                adtv_20 = traded_value.rolling(window=20).mean()
                adtv_50 = traded_value.rolling(window=50).mean()
                
                p_adtv20 = adtv_20.iloc[-1]
                p_adtv50 = adtv_50.iloc[-1]
                
                if pd.isna(p_adtv20) or pd.isna(p_adtv50):
                    continue
                    
                if p_adtv20 <= 50000000 or p_adtv50 <= 50000000:
                    continue
                    
                # SMAs
                sma10 = close.rolling(window=10).mean()
                sma20 = close.rolling(window=20).mean()
                sma50 = close.rolling(window=50).mean()
                
                # 3: 20-day SMA NOT lower than 50-day SMA for the last 20 consecutive days
                # 3: 20-day SMA NOT lower than 50-day SMA for the last 20 consecutive days
                # If it has been strictly lower for all 20 consecutive days, we reject
                last_20_sma20 = sma20.iloc[-20:]
                last_20_sma50 = sma50.iloc[-20:]
                if (last_20_sma20 < last_20_sma50).all():
                    continue
                    
                # 4: Rejection - Close < 50 SMA AND 50 SMA sloping downwards for 20 days
                last_21_sma50 = sma50.iloc[-21:]
                sma50_slope = last_21_sma50.diff().iloc[1:]  # 20 slope diffs
                
                is_close_below_sma50 = float(close.iloc[-1]) < float(sma50.iloc[-1])
                is_sma50_down_20d = (sma50_slope < 0).all()
                
                if is_close_below_sma50 and is_sma50_down_20d:
                    continue
                    
                # 5 & 6 & 7: Trend Alignment
                p_close = float(close.iloc[-1])
                p_prev_close = float(close.iloc[-2])
                p_sma10 = float(sma10.iloc[-1])
                p_sma20 = float(sma20.iloc[-1])
                
                if p_close <= p_sma10: continue
                if p_close <= p_sma20: continue
                if p_sma10 <= p_sma20: continue
                if p_close <= p_prev_close: continue
                
                # 8: ATR Check
                prev_close = close.shift(1)
                tr1 = high - low
                tr2 = (high - prev_close).abs()
                tr3 = (low - prev_close).abs()
                # Vectorized TR calculation
                tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                
                atr20 = tr.rolling(window=20).mean()
                p_tr1 = float(tr.iloc[-1])
                p_atr20 = float(atr20.iloc[-1])
                
                if p_tr1 <= (0.60 * p_atr20):
                    continue
                    
                # 9: Close within upper 60% of day's range
                p_high = float(high.iloc[-1])
                p_low = float(low.iloc[-1])
                day_range = p_high - p_low
                
                if p_close <= (p_low + (day_range * 0.40)):
                    continue
                    
                # Match found!
                matched_tickers.append(tick)
                
            except Exception as e:
                failed_tickers.append(tick)
                continue
                
    return matched_tickers

import os
import concurrent.futures

def _fetch_mc(ticker):
    try:
        t = yf.Ticker(f"{ticker}.NS")
        try:
            mcap = t.fast_info['marketCap']
        except:
            mcap = t.info.get('marketCap', 0)
        return ticker, mcap if mcap is not None else 0
    except:
        return ticker, 0

def enrich_market_caps(csv_path='trendlyne_all_stocks_master.csv', progress_callback=None):
    if not os.path.exists(csv_path):
        return None
        
    df = pd.read_csv(csv_path)
    if 'MarketCap' not in df.columns:
        df['MarketCap'] = np.nan
        
    missing = df[df['MarketCap'].isna() | (df['MarketCap'] == '')]
    tickers_to_fetch = missing['Ticker'].dropna().unique().tolist()
    
    if not tickers_to_fetch:
        return df
        
    if progress_callback:
        progress_callback(f"Fetching missing market caps for {len(tickers_to_fetch)} stocks...", 0, len(tickers_to_fetch))
        
    results = {}
    completed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=50) as executor:
        future_to_ticker = {executor.submit(_fetch_mc, t): t for t in tickers_to_fetch}
        for future in concurrent.futures.as_completed(future_to_ticker):
            tick, mcap = future.result()
            results[tick] = mcap
            completed += 1
            if progress_callback and completed % 50 == 0:
                progress_callback(f"Fetched Market Caps: {completed}/{len(tickers_to_fetch)}", completed, len(tickers_to_fetch))
                
    # Update dataframe
    for tick, mcap in results.items():
        df.loc[df['Ticker'] == tick, 'MarketCap'] = mcap
            
    # Save back safely
    try:
        df.to_csv(csv_path, index=False)
    except Exception as e:
        print(f"Error saving updated CSV: {e}")
        
    if progress_callback:
        progress_callback("Market cap enrichment complete.", len(tickers_to_fetch), len(tickers_to_fetch))
        
    return df

# =====================================================================
# DIVERGENCE BOTTOMS SCREENER LOGIC
# =====================================================================

def div_EMA(series, timeperiod):
    return series.ewm(span=timeperiod, adjust=False).mean()

def div_RSI(series, timeperiod):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/timeperiod, min_periods=timeperiod, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/timeperiod, min_periods=timeperiod, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def div_AD(high, low, close, volume):
    diff = high - low
    diff = diff.replace(0, np.nan)
    mfm = ((close - low) - (high - close)) / diff
    return (mfm.fillna(0) * volume).cumsum()

def div_is_swing_high(data, idx, left_bars, right_bars, column='Close'):
    if idx < left_bars or idx > len(data) - 1 - right_bars:
        return False
    val = data[column].iloc[idx]
    left_window = data[column].iloc[idx - left_bars: idx + 1]
    right_window = data[column].iloc[idx: idx + right_bars + 1]
    return (val >= left_window.max()) and (val >= right_window.max())

def div_is_swing_low(data, idx, left_bars, right_bars, column='Close'):
    if idx < left_bars or idx > len(data) - 1 - right_bars:
        return False
    val = data[column].iloc[idx]
    left_window = data[column].iloc[idx - left_bars: idx + 1]
    right_window = data[column].iloc[idx: idx + right_bars + 1]
    return (val <= left_window.min()) and (val <= right_window.min())

def div_identify_swings(df, left_bars, right_bars, column='Close'):
    data = df.reset_index(drop=True)
    swing_points = []
    for i in range(len(data)):
        if div_is_swing_high(data, i, left_bars, right_bars, column):
            swing_points.append({'Index': i, 'Value': data[column].iloc[i], 'Type': 'High'})
        elif div_is_swing_low(data, i, left_bars, right_bars, column):
            swing_points.append({'Index': i, 'Value': data[column].iloc[i], 'Type': 'Low'})
    return pd.DataFrame(swing_points).sort_values(by='Index') if swing_points else pd.DataFrame(columns=['Index', 'Value', 'Type'])

def div_analyze_behavior(swings_df):
    behaviour = {'HH': False, 'HL': False}
    if swings_df.empty:
        return behaviour
    highs = swings_df[swings_df['Type'] == 'High'].reset_index(drop=True)
    lows = swings_df[swings_df['Type'] == 'Low'].reset_index(drop=True)
    if len(highs) >= 2 and highs.iloc[-1]['Value'] > highs.iloc[-2]['Value']:
        behaviour['HH'] = True
    if len(lows) >= 2 and lows.iloc[-1]['Value'] > lows.iloc[-2]['Value']:
        behaviour['HL'] = True
    return behaviour

def evaluate_divergence_signal(df, left_bars=6, right_bars=4, signal_lookback_days=2):
    """
    Checks if a structural divergence buy signal fired in the last N days.
    Returns boolean True if condition is met.
    """
    try:
        df['RSI14'] = div_RSI(df['Close'], timeperiod=14)
        df['ADL'] = div_AD(df['High'], df['Low'], df['Close'], df['Volume'])
        df['ADL_EMA'] = div_EMA(df['ADL'], timeperiod=55)
        df['ADL_EMA_Slope'] = (df['ADL_EMA'] - df['ADL_EMA'].shift(3)) / 3

        swings_close = div_identify_swings(df, left_bars, right_bars, column='Close')
        swings_hilo = div_identify_swings(df, left_bars, right_bars, column='Low')
        
        if swings_close.empty and swings_hilo.empty:
            return False

        divergence_bar_idx = None 

        for i in range(left_bars, len(df)):
            row = df.iloc[i]
            psw_close = swings_close[swings_close['Index'] <= (i - right_bars)]
            psw_hilo = swings_hilo[swings_hilo['Index'] <= (i - right_bars)]
            
            if len(psw_close) < 2 and len(psw_hilo) < 2:
                continue

            beh_close = div_analyze_behavior(psw_close)
            beh_hilo = div_analyze_behavior(psw_hilo)
            
            match_close = beh_close['HH']
            match_hilo = beh_hilo['HH']

            div_close, div_hilo = False, False
            
            # Divergence on Close
            lows_c = psw_close[psw_close['Type'] == 'Low']
            if len(lows_c) >= 2:
                prev_idx, last_idx = int(lows_c.iloc[-2]['Index']), int(lows_c.iloc[-1]['Index'])
                if lows_c.iloc[-1]['Value'] < lows_c.iloc[-2]['Value']:
                    if df['RSI14'].iloc[last_idx] > df['RSI14'].iloc[prev_idx]:
                        div_close = True

            # Divergence on Lows
            lows_h = psw_hilo[psw_hilo['Type'] == 'Low']
            if len(lows_h) >= 2:
                prev_idx, last_idx = int(lows_h.iloc[-2]['Index']), int(lows_h.iloc[-1]['Index'])
                if lows_h.iloc[-1]['Value'] < lows_h.iloc[-2]['Value']:
                    if df['RSI14'].iloc[last_idx] > df['RSI14'].iloc[prev_idx]:
                        div_hilo = True

            if div_close or div_hilo:
                divergence_bar_idx = i

            adl_condition = (row['ADL_EMA_Slope'] > 0) if pd.notna(row['ADL_EMA_Slope']) else False

            if divergence_bar_idx is not None:
                bars_since_divergence = i - divergence_bar_idx
                
                if bars_since_divergence > 10:
                    divergence_bar_idx = None
                elif adl_condition and (match_close or match_hilo):
                    divergence_bar_idx = None 
                    bars_ago = (len(df) - 1) - i
                    # If this condition triggered recently, return True immediately
                    if bars_ago <= signal_lookback_days:
                        return True
                        
        return False
    except:
        return False

def run_divergence_screener(tickers, progress_callback=None, preloaded_data=None):
    """
    Evaluates a list of tickers based on the Divergence Bottoms Screener criteria.
    """
    matched_tickers = []
    
    if preloaded_data is not None:
        # Utilize Shared Architecture Dictionary locally
        tickers_data = [(t, preloaded_data.get(t)) for t in tickers if t in preloaded_data]
        chunks_loop = [tickers_data]
    else:
        # Needs two years of data per user script
        chunks = list(chunk_list(tickers, 300))
        total_processed = 0
        chunks_loop = []
        for chunk_idx, chunk in enumerate(chunks):
            ns_tickers = [f"{t}.NS" for t in chunk]
            try:
                if progress_callback:
                    progress_callback(f"Downloading 2-yr data chunk {chunk_idx + 1}/{len(chunks)}...", total_processed, len(tickers))
                    
                data = yf.download(ns_tickers, period="2y", group_by="ticker", threads=True, progress=False)
                total_processed += len(chunk)
                
                if len(ns_tickers) == 1:
                    chunks_loop.append([(chunk[0], data)])
                else:
                    t_data = []
                    for idx, t in enumerate(chunk):
                        ns_t = ns_tickers[idx]
                        if isinstance(data.columns, pd.MultiIndex):
                            if ns_t in data.columns.get_level_values(0):
                                t_data.append((t, data[ns_t]))
                        elif not data.empty and 'Close' in data.columns:
                            t_data.append((t, data))
                    chunks_loop.append(t_data)
            except:
                continue
                
    for tickers_data in chunks_loop:
        for tick, df in tickers_data:
            try:
                if df is None or df.empty or len(df) < 100:
                    continue
                    
                # Drop NaN rows efficiently
                df = df.dropna(subset=['Close', 'Volume', 'High', 'Low']).copy()
                
                # Fix Series indexing (same as Momentum screener) to prevent DataFrame issues
                if isinstance(df['Close'], pd.DataFrame):
                    df['Close'] = df['Close'].iloc[:, 0]
                    df['Volume'] = df['Volume'].iloc[:, 0]
                    df['High'] = df['High'].iloc[:, 0]
                    df['Low'] = df['Low'].iloc[:, 0]
                
                # Match the exact 2-year slice length to match previous independent logic
                df = df.tail(504).copy()
                
                if evaluate_divergence_signal(df, left_bars=6, right_bars=4, signal_lookback_days=2):
                    matched_tickers.append(tick)
                    
            except:
                continue
            
    return matched_tickers

# =====================================================================
# WYCKOFF SMA SCREENER LOGIC
# =====================================================================

class WyckoffSMACycleReader:
    """
    6-Year Wyckoff Screener integrating structural Wave Analysis 
    with the SMA 100 to map absolute Markdown, Accumulation, and Markup cycles.
    """
    def __init__(self, df):
        self.df = df.copy()
        # 1. Institutional Moving Average (Context)
        if 'SMA_100' not in self.df.columns:
            self.df['SMA_100'] = self.df['Close'].rolling(window=100).mean()
        
    def _calculate_atr(self, period=20):
        high_low = self.df['High'] - self.df['Low']
        high_close = np.abs(self.df['High'] - self.df['Close'].shift())
        low_close = np.abs(self.df['Low'] - self.df['Close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        return ranges.max(axis=1).rolling(period).mean().bfill()

    def build_waves(self, atr_factor=2.5):
        """ Constructs discrete Buying and Selling Waves, filtering market noise. """
        atr = self._calculate_atr()
        pivots = []
        is_up = None
        ext_price = self.df['Close'].iloc[0]
        ext_idx = 0
        
        for i in range(1, len(self.df)):
            c, h, l, a = self.df['Close'].iloc[i], self.df['High'].iloc[i], self.df['Low'].iloc[i], atr.iloc[i]
            
            if is_up is None:
                if c > ext_price + a * atr_factor:
                    is_up, ext_price, ext_idx = True, h, i
                    pivots.append({'type': 'Low', 'idx': ext_idx, 'price': ext_price})
                elif c < ext_price - a * atr_factor:
                    is_up, ext_price, ext_idx = False, l, i
                    pivots.append({'type': 'High', 'idx': ext_idx, 'price': ext_price})
            elif is_up:
                if h > ext_price:
                    ext_price, ext_idx = h, i
                elif c < ext_price - a * atr_factor:
                    pivots.append({'type': 'High', 'idx': ext_idx, 'price': ext_price})
                    is_up, ext_price, ext_idx = False, l, i
            else:
                if l < ext_price:
                    ext_price, ext_idx = l, i
                elif c > ext_price + a * atr_factor:
                    pivots.append({'type': 'Low', 'idx': ext_idx, 'price': ext_price})
                    is_up, ext_price, ext_idx = True, h, i
                    
        pivots.append({'type': 'High' if is_up else 'Low', 'idx': ext_idx, 'price': ext_price})
        
        waves = []
        for i in range(1, len(pivots)):
            start_p, end_p = pivots[i-1], pivots[i]
            w_df = self.df.iloc[start_p['idx']:end_p['idx']+1]
            waves.append({
                'type': 'Up' if end_p['type'] == 'High' else 'Down',
                'start_idx': start_p['idx'], 'end_idx': end_p['idx'],
                'start_price': start_p['price'], 'end_price': end_p['price'],
                'total_vol': w_df['Volume'].sum(), 'avg_vol': w_df['Volume'].mean()
            })
        return waves

    def map_cycle_phases(self, waves):
        """ 
        Finds the Active TR by explicitly requiring a Markdown (Price < SMA100) 
        and an emerging Markup (Price > SMA100).
        """
        best_setup = None
        
        for i in range(len(waves) - 5):
            if waves[i]['type'] != 'Down': continue
            sc_wave = waves[i]
            sc_idx_df = sc_wave['end_idx']
            
            lookback_start = max(0, sc_idx_df - 200)
            prior_action = self.df.iloc[lookback_start:sc_idx_df]
            if len(prior_action) < 50: continue
            
            time_below_sma = (prior_action['Close'] < prior_action['SMA_100']).mean()
            if time_below_sma < 0.65: continue
            
            prior_peak_idx = prior_action['High'].idxmax()
            
            ar_wave = waves[i+1]
            if ar_wave['type'] != 'Up': continue
            
            ice = sc_wave['end_price']
            creek = ar_wave['end_price']
            if creek <= ice: continue
            
            is_dead = any(w['end_price'] > creek * 1.4 for w in waves[i+2:])
            if is_dead: continue
            
            post_ar_down = [w for w in waves[i+2:] if w['type'] == 'Down']
            if not post_ar_down: continue
            phase_c_wave = min(post_ar_down, key=lambda w: w['end_price'])
            pc_idx = waves.index(phase_c_wave)
            
            post_pc_up = [w for w in waves[pc_idx+1:] if w['type'] == 'Up']
            sos_wave = None
            for w in post_pc_up:
                sma_at_sos = self.df['SMA_100'].iloc[w['end_idx']]
                if w['end_price'] > sma_at_sos and w['avg_vol'] > phase_c_wave['avg_vol']:
                    sos_wave = w
                    break
            if not sos_wave: continue
            sos_idx = waves.index(sos_wave)
            
            post_sos_down = [w for w in waves[sos_idx+1:] if w['type'] == 'Down']
            if not post_sos_down: continue
            lps_wave = post_sos_down[-1]
            lps_idx = waves.index(lps_wave)
            
            sma_at_lps = self.df['SMA_100'].iloc[lps_wave['end_idx']]
            if lps_wave['end_price'] <= phase_c_wave['end_price']: continue
            if lps_wave['avg_vol'] > sos_wave['avg_vol']: continue
            
            current_price = self.df['Close'].iloc[-1]
            current_sma = self.df['SMA_100'].iloc[-1]
            
            trigger, reason = None, ""
            
            if lps_idx == len(waves) - 1:
                if current_price >= current_sma * 0.95: 
                    trigger = 'LPS'
                    reason = "Phase D: LPS forming. Price establishing support on SMA 100."
            elif lps_idx == len(waves) - 2 and waves[-1]['type'] == 'Up':
                if current_price > current_sma and current_price < creek:
                    trigger = 'SOS2'
                    reason = "Phase D: Initiating internal markup above SMA 100."
                elif current_price > creek and current_price > current_sma:
                    trigger = 'JAC'
                    reason = "Phase E: Breakout. Markup officially confirmed above Creek & SMA 100."
            
            if trigger:
                best_setup = {
                    'Peak_Idx': prior_peak_idx, 'SC': sc_wave, 'AR': ar_wave,
                    'Phase_C': phase_c_wave, 'SOS': sos_wave, 'LPS': lps_wave,
                    'Ice': ice, 'Creek': creek, 'Reason': reason
                }
                
        return best_setup

def build_wyckoff_plotly_figure(df, waves, setup, ticker):
    import plotly.graph_objects as go
    
    fig = go.Figure()
    
    # 1. Shaded Cycle Regions
    if setup['Peak_Idx'] in df.index and setup['SC']['end_idx'] < len(df):
        peak_date = setup['Peak_Idx']
        sc_date = df.index[setup['SC']['end_idx']]
        fig.add_vrect(x0=peak_date, x1=sc_date, fillcolor="red", opacity=0.1, line_width=0, layer="below")
    
    if setup['SC']['end_idx'] < len(df) and setup['SOS']['end_idx'] < len(df):
        sc_date = df.index[setup['SC']['end_idx']]
        sos_date = df.index[setup['SOS']['end_idx']]
        fig.add_vrect(x0=sc_date, x1=sos_date, fillcolor="blue", opacity=0.1, line_width=0, layer="below")
        
    if setup['SOS']['end_idx'] < len(df):
        sos_date = df.index[setup['SOS']['end_idx']]
        fig.add_vrect(x0=sos_date, x1=df.index[-1], fillcolor="green", opacity=0.1, line_width=0, layer="below")
        
    fig.add_trace(go.Scatter(
        x=df.index, y=[float(v) for v in df['Close']], 
        mode='lines',
        name='Close Price', 
        line=dict(color='#ffffff', width=0.5), 
        opacity=0.5
    ))
    
    fig.add_trace(go.Scatter(
        x=df.index, y=[float(v) for v in df['SMA_100']], 
        mode='lines',
        name='100-Day SMA', 
        line=dict(color='orange', width=2)
    ))

    # 3. Wyckoff TR Bounds
    if setup['SC']['end_idx'] < len(df):
        sc_date = df.index[setup['SC']['end_idx']]
        fig.add_trace(go.Scatter(x=[sc_date, df.index[-1]], y=[setup['Ice'], setup['Ice']], mode='lines', name='Ice (Support)', line=dict(color='red', width=1.5, dash='dash')))
        fig.add_trace(go.Scatter(x=[sc_date, df.index[-1]], y=[setup['Creek'], setup['Creek']], mode='lines', name='Creek (Resistance)', line=dict(color='green', width=1.5, dash='dash')))

    # 4. ZigZag Waves
    for w in waves:
        if w['start_idx'] < len(df) and w['end_idx'] < len(df):
            color = 'lime' if w['type'] == 'Up' else 'red'
            fig.add_trace(go.Scatter(
                x=[df.index[w['start_idx']], df.index[w['end_idx']]],
                y=[w['start_price'], w['end_price']],
                mode='lines', line=dict(color=color, width=2), opacity=0.8,
                showlegend=False
            ))

    # 5. Annotations
    def annotate(wave, label, color, position='bottom'):
        if wave['end_idx'] >= len(df): return
        idx = wave['end_idx']
        price = wave['end_price']
        y_offset = price * (0.94 if position == 'bottom' else 1.06)
        symbol = 'triangle-up' if position == 'bottom' else 'triangle-down'
        
        fig.add_trace(go.Scatter(
            x=[df.index[idx]], y=[y_offset], mode='markers+text',
            marker=dict(symbol=symbol, color=color, size=15),
            text=[label], textposition='bottom center' if position == 'bottom' else 'top center',
            textfont=dict(color=color, size=11, family="Arial Black"),
            showlegend=False
        ))

    annotate(setup['SC'], 'SC', 'white', 'bottom')
    annotate(setup['AR'], 'AR', 'white', 'top')
    pc_label = 'Spring' if setup['Phase_C']['end_price'] < setup['Ice'] else 'Test (Phase C)'
    annotate(setup['Phase_C'], pc_label, '#ff00ff', 'bottom')
    annotate(setup['SOS'], 'SOS', 'cyan', 'top')
    annotate(setup['LPS'], 'LPS (BUY)', 'yellow', 'bottom')

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        hovermode="x unified",
        margin=dict(l=40, r=40, t=40, b=40),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        xaxis=dict(showgrid=False, zeroline=False),
        yaxis=dict(showgrid=True, gridcolor='rgba(255,255,255,0.05)', zeroline=False),
    )
    return fig

def run_wyckoff_screener(tickers, preloaded_data=None, progress_callback=None):
    """
    Evaluates a list of tickers based on the Wyckoff SMA method.
    Uses preloaded_data (shared 10y dict from fetch_unified_market_data) when available.
    Falls back to independent yfinance download when preloaded_data is None.
    Returns matched results as dictionaries containing the setup info and chart dataframe.
    """
    matched_results = []
    
    if preloaded_data is not None:
        # Utilize Shared Architecture Dictionary locally (same pattern as momentum/divergence)
        tickers_data = [(t, preloaded_data.get(t)) for t in tickers if t in preloaded_data]
        chunks_loop = [tickers_data]
    else:
        # Fall-back legacy independent behavior
        chunks = list(chunk_list(tickers, 300))
        total_processed = 0
        chunks_loop = []
        
        for chunk_idx, chunk in enumerate(chunks):
            ns_tickers = [f"{t}.NS" for t in chunk]
            try:
                if progress_callback:
                    progress_callback(f"Downloading 10-yr data for Wyckoff chunk {chunk_idx + 1}/{len(chunks)}...", total_processed, len(tickers))
                    
                data = yf.download(ns_tickers, period="10y", group_by="ticker", threads=True, progress=False)
                total_processed += len(chunk)
                
                if len(ns_tickers) == 1:
                    chunks_loop.append([(chunk[0], data)])
                else:
                    t_data = []
                    for idx, t in enumerate(chunk):
                        ns_t = ns_tickers[idx]
                        if isinstance(data.columns, pd.MultiIndex):
                            if ns_t in data.columns.get_level_values(0):
                                t_data.append((t, data[ns_t]))
                        elif not data.empty and 'Close' in data.columns:
                            t_data.append((t, data))
                    chunks_loop.append(t_data)
            except:
                continue
            
    for tickers_data in chunks_loop:
        for tick, df in tickers_data:
            try:
                if df is None or df.empty or len(df) < 1512:
                    continue
                    
                df = df.dropna(subset=['Close', 'Volume', 'High', 'Low']).copy()
                if len(df) < 1512:
                    continue
                    
                # Fix Series indexing if needed (like the momentum screener)
                # Ensure df is truly flat, avoiding MultiIndex dimensionality artifacts that Plotly parses as objects
                if isinstance(df.columns, pd.MultiIndex):
                    try:
                        df.columns = df.columns.droplevel('Ticker')
                    except:
                        pass
                
                df = df.dropna()
                
                df['SMA_100'] = df['Close'].rolling(window=100).mean()
                df_6y = df.iloc[-1512:].copy()
                
                reader = WyckoffSMACycleReader(df_6y)
                waves = reader.build_waves()
                setup = reader.map_cycle_phases(waves)
                
                if setup:
                    matched_results.append({
                        'ticker': tick,
                        'setup': setup,
                        'reason': setup['Reason'],
                        'waves': waves,
                        'df_6y': df_6y
                    })
            except Exception as e:
                continue
            
    return matched_results

