#!/usr/bin/env python3
"""
tech_calculations.py — All technical analysis and charting functions
"""
import time
from datetime import datetime
import pandas as pd
import numpy as np
import yfinance as yf
import talib
from tvDatafeed import TvDatafeed, Interval
import plotly.graph_objects as go

import os

# Initialize TradingView datafeed (authenticated if credentials provided, else guest)
_tv_user = os.environ.get('TV_USERNAME', '')
_tv_pass = os.environ.get('TV_PASSWORD', '')
if _tv_user and _tv_pass:
    tv = TvDatafeed(username=_tv_user, password=_tv_pass)
    print(f"INFO: TvDatafeed initialized with authenticated session (user: {_tv_user})")
else:
    tv = TvDatafeed()
    print("INFO: TvDatafeed initialized as guest (set TV_USERNAME & TV_PASSWORD for stable connection)")

# -------------------------------------------------------------------
# 1) Data Fetching and Technical Calculation Helpers
# -------------------------------------------------------------------

def fetch_histogram(symbol, exchange, start_date, end_date, max_retries=3, interval='daily', force_yf=False):
    """
    Fetch historical price data from TradingView or yfinance.
    
    Args:
        interval: 'daily' (default) or 'weekly'
        force_yf: Skip TradingView completely and fetch straight from yfinance to avoid rate limits
    """
    # Determine TradingView interval
    tv_interval = Interval.in_weekly if interval == 'weekly' else Interval.in_daily
    yf_interval = '1wk' if interval == 'weekly' else '1d'
    
    # Adjust n_bars for weekly data
    if interval == 'weekly':
        n_bars = max(300, ((end_date - start_date).days // 7) + 5)
    else:
        n_bars = max(1100, (end_date - start_date).days + 5)
    
    raw = None
    if not force_yf:
        tv_symbol = symbol.replace('-', '_')
        for _ in range(max_retries):
            try:
                raw = tv.get_hist(symbol=tv_symbol, exchange=exchange,
                                  interval=tv_interval, n_bars=n_bars)
                if raw is not None and not raw.empty:
                    break
            except Exception:
                time.sleep(1)
                
    if raw is None or raw.empty:
        yf_sym = '^NSEI' if symbol.upper()=='NIFTY' and exchange=='NSE' else f"{symbol}.NS"
        try:
            ticker_obj = yf.Ticker(yf_sym)
            raw = ticker_obj.history(start=start_date.strftime('%Y-%m-%d'),
                                     end=end_date.strftime('%Y-%m-%d'),
                                     interval=yf_interval)
            
            if raw is not None and not raw.empty:
                # Remove timezone if yfinance attached it
                if raw.index.tz is not None:
                    raw.index = raw.index.tz_convert(None)
            else:
                return pd.DataFrame()
                
        except Exception as e:
            print(f"WARN: yfinance fallback failed for {yf_sym}: {e}")
            return pd.DataFrame()
    df = raw.copy()
    df = df[(df.index >= pd.to_datetime(start_date)) & (df.index <= pd.to_datetime(end_date))]
    df.rename(columns={'open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'}, inplace=True)
    return df


def align_data_indices(a, b):
    idx = a.index.intersection(b.index)
    return a.loc[idx], b.loc[idx]


def calculate_relative_strength(stock, index, length=55):
    if len(stock) < length or len(index) < length:
        return pd.Series(index=stock.index, data=np.nan)
    sp = stock['Close'].pct_change(length)
    ip = index['Close'].pct_change(length)
    return sp - ip


def is_swing_high(df, i, L, R, col='Close'):
    if i < L or i > len(df) - 1 - R:
        return False
    v = df[col].iat[i]
    return v >= df[col].iloc[i-L:i+1].max() and v >= df[col].iloc[i:i+R+1].max()


def is_swing_low(df, i, L, R, col='Close'):
    if i < L or i > len(df) - 1 - R:
        return False
    v = df[col].iat[i]
    return v <= df[col].iloc[i-L:i+1].min() and v <= df[col].iloc[i:i+R+1].min()


def identify_swing_points(df, L, R, column='Close'):
    data = df.reset_index(drop=True)
    pts = []
    for i in range(len(data)):
        if is_swing_high(data, i, L, R, column):
            pts.append({'Index': i, 'Value': data[column].iat[i], 'Type': 'High'})
        elif is_swing_low(data, i, L, R, column):
            pts.append({'Index': i, 'Value': data[column].iat[i], 'Type': 'Low'})
    return pd.DataFrame(pts)


def identify_swing_points_high(df, L, R):
    data = df.reset_index(drop=True)
    pts = []
    for i in range(L, len(data) - R):
        v = data['High'].iat[i]
        if v >= data['High'].iloc[i-L:i+1].max() and v >= data['High'].iloc[i:i+R+1].max():
            pts.append({'Index': i, 'Value': v, 'Type': 'High'})
    return pd.DataFrame(pts)


def identify_swing_points_low(df, L, R):
    data = df.reset_index(drop=True)
    pts = []
    for i in range(L, len(data) - R):
        v = data['Low'].iat[i]
        if v <= data['Low'].iloc[i-L:i+1].min() and v <= data['Low'].iloc[i:i+R+1].min():
            pts.append({'Index': i, 'Value': v, 'Type': 'Low'})
    return pd.DataFrame(pts)


def analyze_swing_behaviour(sw):
    beh = {'HH': False, 'HL': False, 'LH': False, 'LL': False}
    highs = sw[sw['Type'] == 'High']
    lows  = sw[sw['Type'] == 'Low']
    if len(highs) >= 2:
        beh['HH'] = highs['Value'].iloc[-1] > highs['Value'].iloc[-2]
        beh['LH'] = not beh['HH']
    if len(lows) >= 2:
        beh['HL'] = lows['Value'].iloc[-1] > lows['Value'].iloc[-2]
        beh['LL'] = not beh['HL']
    return beh

# ===================================================================
# START: NEW HELPER FUNCTIONS
# These functions work directly with the 'beh' (behavior) dictionary,
# ensuring that any overrides are correctly processed.
# ===================================================================
def get_swing_tokens_from_behaviour(beh):
    """Generates trend tokens (HH, LL, etc.) from a behavior dictionary."""
    toks = []
    # Check if a high comparison was possible/determined
    if beh.get('HH') or beh.get('LH'):
        toks.append('HH' if beh.get('HH') else 'LH')
    # Check if a low comparison was possible/determined
    if beh.get('HL') or beh.get('LL'):
        toks.append('HL' if beh.get('HL') else 'LL')
    return toks

def compute_price_action_trend_from_behaviour(beh):
    """Computes the trend ('Uptrend', etc.) from a behavior dictionary."""
    if beh.get('HH') and beh.get('HL'):
        return 'Uptrend'
    if beh.get('LH') and beh.get('LL'):
        return 'Downtrend'
    return 'Sideways'
# ===================================================================
# END: NEW HELPER FUNCTIONS
# ===================================================================


def compute_market_structure(df):
    up   = (df['EMA13'] > df['EMA55']) & (df['EMA55'] > df['EMA144'])
    down = (df['EMA13'] < df['EMA55']) & (df['EMA55'] < df['EMA144'])
    mildup   = (df['EMA13'] > df['EMA55']) & (df['EMA55'] < df['EMA144'])
    milddown = (df['EMA13'] < df['EMA55']) & (df['EMA55'] > df['EMA144'])
    i = -1
    if up.iloc[i]:
        return 'Uptrend'
    elif down.iloc[i]:
        return 'Downtrend'
    elif mildup.iloc[i]:
        return 'Mild Uptrend'
    elif milddown.iloc[i]:
        return 'Mild Downtrend'
    else:
        return 'Sideways'


def compute_fib_strength(sw, trend, cp, df):
    lvls = [0.236, 0.382, 0.5, 0.618]
    highs = sw[sw['Type'] == 'High']
    lows  = sw[sw['Type'] == 'Low']
    if trend == 'Uptrend' and len(highs) >= 1 and len(lows) >= 1:
        last_h = highs['Value'].iloc[-1]
        prev_l = lows['Value'].iloc[-1]
        retr = {l: last_h - (last_h - prev_l) * l for l in lvls}
        fib_ok = cp >= retr[0.382]
        show = ''
        for l in lvls:
            if cp >= retr[l]:
                show = f'({l})'
                break
        return f"{'Strong' if fib_ok else 'Weak'} {show}".strip()
    if trend == 'Downtrend' and len(lows) >= 1 and len(highs) >= 1:
        last_l = lows['Value'].iloc[-1]
        prev_h = highs['Value'].iloc[-1]
        retr = {l: last_l + (prev_h - last_l) * l for l in lvls}
        fib_ok = cp <= retr[0.382]
        show = ''
        for l in lvls:
            if cp <= retr[l]:
                show = f'({l})'
                break
        return f"{'Strong' if fib_ok else 'Weak'} {show}".strip()
    return 'Not Applicable'


def find_support_resistance(sw, df):
    cp  = df['Close'].iat[-1]
    atr = df['ATR14'].iat[-1]
    support, resistance = None, None
    min_sup_gap, min_res_gap = float('inf'), float('inf')
    for _, pivot in sw.iterrows():
        idx = int(pivot['Index'])
        val = pivot['Value']
        typ = pivot['Type']
        gap = abs(cp - val)
        if gap < atr:
            continue
        if ((typ == 'Low' and val < cp) or (typ == 'High' and val <= cp)) and gap < min_sup_gap:
            min_sup_gap = gap
            support = (df['Low'].iat[idx], df['High'].iat[idx])
        if ((typ == 'High' and val > cp) or (typ == 'Low' and val >= cp)) and gap < min_res_gap:
            min_res_gap = gap
            resistance = (df['Low'].iat[idx], df['High'].iat[idx])
    return support, resistance


def detect_ema_crosses(df):
    crosses = []
    for i in range(1, len(df)):
        prev13, prev144 = df['EMA13'].iat[i-1], df['EMA144'].iat[i-1]
        cur13,  cur144  = df['EMA13'].iat[i],   df['EMA144'].iat[i]
        if prev13 <= prev144 < cur13 > cur144:
            crosses.append(f"GC@{i}")
        if prev13 >= prev144 > cur13 < cur144:
            crosses.append(f"DC@{i}")
    return crosses


# -------------------------------------------------------------------
# RSI Divergence Detection Functions
# -------------------------------------------------------------------

def detect_rsi_divergence(df, L=6, R=4, lookback_pivots=5):
    """
    Detect bullish and bearish RSI divergences based on swing pivots.
    
    Bullish Divergence: Price makes Lower Low, RSI makes Higher Low
    Bearish Divergence: Price makes Higher High, RSI makes Lower High
    
    Returns: dict with:
        - divergence_type: 'Bullish Divergence', 'Bearish Divergence', or 'No Divergence'
        - last_date: date of the most recent divergence (or None)
        - divergences: list of divergence points for charting
          Each point: {'type': 'bullish'/'bearish', 'start_idx': int, 'end_idx': int,
                       'start_price': float, 'end_price': float, 'start_rsi': float, 'end_rsi': float}
    """
    result = {
        'divergence_type': 'No Divergence',
        'last_date': None,
        'divergences': []
    }
    
    if 'RSI14' not in df.columns or len(df) < L + R + 10:
        return result
    
    data = df.reset_index(drop=True)
    
    # Identify swing lows in price (for bullish divergence)
    swing_lows = []
    for i in range(L, len(data) - R):
        val = data['Close'].iat[i]
        if val <= data['Close'].iloc[i-L:i+1].min() and val <= data['Close'].iloc[i:i+R+1].min():
            rsi_val = data['RSI14'].iat[i]
            if not np.isnan(rsi_val):
                swing_lows.append({'idx': i, 'price': val, 'rsi': rsi_val})
    
    # Identify swing highs in price (for bearish divergence)
    swing_highs = []
    for i in range(L, len(data) - R):
        val = data['Close'].iat[i]
        if val >= data['Close'].iloc[i-L:i+1].max() and val >= data['Close'].iloc[i:i+R+1].max():
            rsi_val = data['RSI14'].iat[i]
            if not np.isnan(rsi_val):
                swing_highs.append({'idx': i, 'price': val, 'rsi': rsi_val})
    
    divergences = []
    
    # Check for bullish divergences (last N pairs of swing lows)
    recent_lows = swing_lows[-lookback_pivots:] if len(swing_lows) >= 2 else swing_lows
    for i in range(1, len(recent_lows)):
        prev = recent_lows[i-1]
        curr = recent_lows[i]
        # Bullish: Price Lower Low, RSI Higher Low
        if curr['price'] < prev['price'] and curr['rsi'] > prev['rsi']:
            divergences.append({
                'type': 'bullish',
                'start_idx': prev['idx'],
                'end_idx': curr['idx'],
                'start_price': prev['price'],
                'end_price': curr['price'],
                'start_rsi': prev['rsi'],
                'end_rsi': curr['rsi']
            })
    
    # Check for bearish divergences (last N pairs of swing highs)
    recent_highs = swing_highs[-lookback_pivots:] if len(swing_highs) >= 2 else swing_highs
    for i in range(1, len(recent_highs)):
        prev = recent_highs[i-1]
        curr = recent_highs[i]
        # Bearish: Price Higher High, RSI Lower High
        if curr['price'] > prev['price'] and curr['rsi'] < prev['rsi']:
            divergences.append({
                'type': 'bearish',
                'start_idx': prev['idx'],
                'end_idx': curr['idx'],
                'start_price': prev['price'],
                'end_price': curr['price'],
                'start_rsi': prev['rsi'],
                'end_rsi': curr['rsi']
            })
    
    result['divergences'] = divergences
    
    # Determine most recent divergence
    if divergences:
        # Sort by end_idx descending to get most recent
        sorted_divs = sorted(divergences, key=lambda x: x['end_idx'], reverse=True)
        most_recent = sorted_divs[0]
        result['divergence_type'] = 'Bullish Divergence' if most_recent['type'] == 'bullish' else 'Bearish Divergence'
        result['last_date'] = df.index[most_recent['end_idx']].strftime('%d %b %Y')
    
    return result


def build_rsi_divergence_figure(df, ticker, years=1, line_color='#ffffff'):
    """
    Build a dual-pane Plotly figure:
    - Top: Price with swing pivots
    - Bottom: RSI with divergence lines and labels
    
    Both panes share the x-axis for synchronized interaction.
    
    Args:
        line_color: Color for the price line (default: white for AI Chart Analysis)
    """
    from plotly.subplots import make_subplots
    
    # Filter to specified years of data
    d = df[df.index >= df.index.max() - pd.DateOffset(years=years)].copy()
    d_reset = d.reset_index(drop=True)
    
    # Pivot ranges: 5,2 for weekly (5Y), 6,4 for daily (1Y/3Y)
    pivot_left = 5 if years == 5 else 6
    pivot_right = 2 if years == 5 else 4
    
    # Detect divergences
    div_result = detect_rsi_divergence(d, L=pivot_left, R=pivot_right, lookback_pivots=5)
    divergences = div_result['divergences']
    
    # Identify swing points for display
    sp = identify_swing_points(d, pivot_left, pivot_right, 'Close')
    
    # Create subplots with 2 rows, shared x-axis
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.6, 0.4],
        subplot_titles=(f'{ticker} Price Pivots', 'RSI with Divergences')
    )
    
    # === Top Pane: Price with Pivots ===
    fig.add_trace(
        go.Scatter(x=list(d.index), y=[float(v) for v in d['Close']], 
                   mode='lines', name='Close', line=dict(color=line_color)),
        row=1, col=1
    )
    if 'SMA20' in d.columns:
        fig.add_trace(
            go.Scatter(x=list(d.index), y=[float(v) for v in d['SMA20']], mode='lines', name='SMA-20', line=dict(color='#fbbf24', width=1.5, dash='dot')),
            row=1, col=1
        )
    
    # Add swing high markers
    highs = sp[sp['Type'] == 'High']
    if not highs.empty:
        fig.add_trace(
            go.Scatter(x=d.index[highs['Index']], y=[float(v) for v in highs['Value']], 
                       mode='markers', name='Swing High', 
                       marker=dict(symbol='triangle-up', size=10, color='red')),
            row=1, col=1
        )
    
    # Add swing low markers
    lows = sp[sp['Type'] == 'Low']
    if not lows.empty:
        fig.add_trace(
            go.Scatter(x=d.index[lows['Index']], y=[float(v) for v in lows['Value']], 
                       mode='markers', name='Swing Low', 
                       marker=dict(symbol='triangle-down', size=10, color='green')),
            row=1, col=1
        )
    
    # === Bottom Pane: RSI with Divergence Lines ===
    fig.add_trace(
        go.Scatter(x=list(d.index), y=[float(v) for v in d['RSI14']], 
                   mode='lines', name='RSI', line=dict(color='#22d3ee', width=1.5)),
        row=2, col=1
    )
    
    # Add RSI EMA
    if 'RSI_EMA13' in d.columns:
        fig.add_trace(
            go.Scatter(x=list(d.index), y=[float(v) for v in d['RSI_EMA13']], 
                       mode='lines', name='RSI EMA-13', line=dict(color='#fbbf24', width=1, dash='dot')),
            row=2, col=1
        )
    
    # Add overbought/oversold reference lines
    fig.add_hline(y=70, line_dash="dash", line_color="red", opacity=0.5, row=2, col=1)
    fig.add_hline(y=30, line_dash="dash", line_color="green", opacity=0.5, row=2, col=1)
    
    # Draw divergence lines on RSI chart
    for div in divergences:
        start_idx = div['start_idx']
        end_idx = div['end_idx']
        
        # Make sure indices are within our filtered data range
        if start_idx >= len(d) or end_idx >= len(d):
            continue
            
        color = 'green' if div['type'] == 'bullish' else 'red'
        label = 'Bull' if div['type'] == 'bullish' else 'Bear'
        
        # Draw line connecting RSI points
        fig.add_trace(
            go.Scatter(
                x=[d.index[start_idx], d.index[end_idx]],
                y=[div['start_rsi'], div['end_rsi']],
                mode='lines+text',
                line=dict(color=color, width=2),
                text=['', label],
                textposition='top right',
                textfont=dict(color='white', size=10),
                showlegend=False
            ),
            row=2, col=1
        )
        
        # Add background box for text label
        fig.add_annotation(
            x=d.index[end_idx],
            y=div['end_rsi'],
            text=label,
            showarrow=False,
            font=dict(color='white', size=10, family='Arial Black'),
            bgcolor=color,
            borderpad=3,
            row=2, col=1
        )
    
    # Update layout with explicit domain settings for proper subplot separation
    fig.update_layout(
        height=800,  # Increased for dual-pane display
        margin=dict(b=80, t=50),  # Add margins for title and legend
        hovermode='x unified',
        legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.08),
        xaxis2=dict(
            type='date',
            showspikes=True,
            spikemode='across',
            spikesnap='cursor',
            spikethickness=1,
            spikedash='dot',
            spikecolor='lightgrey'
        ),
        yaxis=dict(title='Price', domain=[0.45, 1.0]),  # Top chart takes upper 55%
        yaxis2=dict(title='RSI', range=[0, 100], domain=[0.0, 0.38])  # Bottom chart takes lower 38%
    )
    
    return fig


# -------------------------------------------------------------------
# 2) Chart Generation Functions
# -------------------------------------------------------------------

def find_trendlines(df, swing_points, trendline_type='support', min_touches=3, tolerance=0.005, atr_values=None, atr_multiplier=0.5):
    """
    Find valid trendlines that connect 3+ swing points without crossing price.
    
    Args:
        df: DataFrame with price data (must have 'Close' column)
        swing_points: DataFrame with 'Index', 'Value', 'Type' columns
        trendline_type: 'support' (connects lows) or 'resistance' (connects highs)
        min_touches: Minimum number of swing points the line must touch (default 3)
        tolerance: Fallback percentage tolerance (used when atr_values not provided or NaN)
        atr_values: Optional numpy array of ATR-14 values (same length as df)
        atr_multiplier: Multiplier for ATR tolerance (default 0.5)
    
    Returns:
        List of trendlines, each as dict with 'start_idx', 'end_idx', 'start_val', 'end_val', 'touches'
    """
    close_prices = df['Close'].values
    
    # Filter swing points by type
    if trendline_type == 'support':
        points = swing_points[swing_points['Type'] == 'Low'].copy()
    else:
        points = swing_points[swing_points['Type'] == 'High'].copy()
    
    if len(points) < min_touches:
        return []
    
    points = points.sort_values('Index').reset_index(drop=True)
    trendlines = []
    
    # Try all pairs of points as potential trendline start/end
    for i in range(len(points) - 1):
        for j in range(i + 1, len(points)):
            idx1, val1 = int(points.iloc[i]['Index']), float(points.iloc[i]['Value'])
            idx2, val2 = int(points.iloc[j]['Index']), float(points.iloc[j]['Value'])
            
            if idx2 <= idx1:
                continue
            
            # Calculate slope
            slope = (val2 - val1) / (idx2 - idx1)
            
            # Check how many swing points lie on or near this line
            touches = []
            
            for k in range(len(points)):
                idx_k, val_k = int(points.iloc[k]['Index']), float(points.iloc[k]['Value'])
                if idx_k < idx1 or idx_k > idx2:
                    continue
                
                # Calculate expected value on the line at this index
                line_val = val1 + slope * (idx_k - idx1)
                
                # Check if point is on the line (within tolerance)
                # Use ATR-based tolerance if available, else fall back to percentage
                if atr_values is not None and idx_k < len(atr_values) and not np.isnan(atr_values[idx_k]):
                    tol_abs = atr_values[idx_k] * atr_multiplier
                    is_touch = abs(val_k - line_val) < tol_abs
                else:
                    is_touch = abs(val_k - line_val) / line_val < tolerance
                
                if is_touch:
                    touches.append((idx_k, val_k))
            
            if len(touches) < min_touches:
                continue
            
            # Validate: trendline must not cross price between swing points
            is_valid = True
            for idx in range(idx1, idx2 + 1):
                if idx >= len(close_prices):
                    break
                    
                price = close_prices[idx]
                line_val = val1 + slope * (idx - idx1)
                
                if trendline_type == 'support':
                    # Support line should be below or at price (with small tolerance)
                    cross_tol = atr_values[idx] * 0.1 if (atr_values is not None and idx < len(atr_values) and not np.isnan(atr_values[idx])) else price * 0.001
                    if line_val > price + cross_tol:  # Line crosses above price
                        is_valid = False
                        break
                else:
                    # Resistance line should be above or at price (with small tolerance)
                    cross_tol = atr_values[idx] * 0.1 if (atr_values is not None and idx < len(atr_values) and not np.isnan(atr_values[idx])) else price * 0.001
                    if line_val < price - cross_tol:  # Line crosses below price
                        is_valid = False
                        break
            
            if is_valid:
                trendlines.append({
                    'start_idx': idx1,
                    'end_idx': idx2,
                    'start_val': val1,
                    'end_val': val2,
                    'touches': len(touches),
                    'slope': slope
                })
    
    # Remove duplicate/overlapping trendlines - keep the ones with most touches
    if not trendlines:
        return []
    
    # Sort by touches (descending) and take the best non-overlapping ones
    trendlines.sort(key=lambda x: x['touches'], reverse=True)
    
    # Keep top trendlines that don't significantly overlap
    final_lines = []
    for line in trendlines:
        is_overlapping = False
        for existing in final_lines:
            # Check if this line overlaps significantly with an existing one
            overlap_start = max(line['start_idx'], existing['start_idx'])
            overlap_end = min(line['end_idx'], existing['end_idx'])
            if overlap_end > overlap_start:
                overlap_ratio = (overlap_end - overlap_start) / (line['end_idx'] - line['start_idx'])
                if overlap_ratio > 0.5:
                    is_overlapping = True
                    break
        
        if not is_overlapping:
            final_lines.append(line)
        
        if len(final_lines) >= 2:  # Limit to 2 trendlines per type
            break
    
    return final_lines


def build_close_figure(df, ticker, years=1, line_color='#ffffff'):
    """
    Build Close price chart with swing points and trendlines.
    
    Args:
        line_color: Color for the price line (default: white for AI Chart Analysis)
    """
    d = df[df.index >= df.index.max() - pd.DateOffset(years=years)].reset_index()
    d_original_index = d.set_index(d.columns[0])  # Preserve datetime index for plotting
    d_reset = d.reset_index(drop=True)  # Numeric index for trendline calculation
    
    # ATR-14 values for tolerance
    atr_vals = d_reset['ATR14'].values if 'ATR14' in d_reset.columns else None
    
    # Pivot ranges: 5,2 for weekly (5Y), 6,4 for daily (1Y/3Y)
    pivot_left = 5 if years == 5 else 6
    pivot_right = 2 if years == 5 else 4
    
    sp = identify_swing_points(d_reset, pivot_left, pivot_right, 'Close')
    fig = go.Figure()
    
    # Price line
    fig.add_trace(go.Scatter(x=list(d_original_index.index), y=[float(v) for v in d_original_index['Close']], mode='lines', name='Close', line=dict(color=line_color)))
    
    # SMA-20
    if 'SMA20' in d_original_index.columns:
        fig.add_trace(go.Scatter(x=list(d_original_index.index), y=[float(v) for v in d_original_index['SMA20']], mode='lines', name='SMA-20', line=dict(color='#fbbf24', width=1.5, dash='dot')))
    
    # Swing highs
    highs = sp[sp['Type']=='High']
    if not highs.empty:
        fig.add_trace(go.Scatter(x=d_original_index.index[highs['Index']], y=[float(v) for v in highs['Value']], mode='markers', name='Swing High', marker=dict(symbol='triangle-up', size=12, color='red')))
    
    # Swing lows
    lows = sp[sp['Type']=='Low']
    if not lows.empty:
        fig.add_trace(go.Scatter(x=d_original_index.index[lows['Index']], y=[float(v) for v in lows['Value']], mode='markers', name='Swing Low', marker=dict(symbol='triangle-down', size=12, color='green')))
    
    # Find and draw support trendlines (green, dashed)
    support_lines = find_trendlines(d_reset, sp, trendline_type='support', min_touches=3, atr_values=atr_vals, atr_multiplier=0.5)
    for i, line in enumerate(support_lines):
        # Extend line to current date
        start_idx = line['start_idx']
        end_idx = min(line['end_idx'] + 20, len(d_reset) - 1)  # Extend 20 bars or to end
        extended_val = line['start_val'] + line['slope'] * (end_idx - start_idx)
        
        fig.add_trace(go.Scatter(
            x=[d_original_index.index[start_idx], d_original_index.index[end_idx]],
            y=[line['start_val'], extended_val],
            mode='lines',
            name=f'Support ({line["touches"]} touches)',
            line=dict(color='#22c55e', width=1, dash='3,1.5'),
            showlegend=(i == 0)  # Only show legend for first support line
        ))
    
    # Find and draw resistance trendlines (red, dashed)
    resistance_lines = find_trendlines(d_reset, sp, trendline_type='resistance', min_touches=3, atr_values=atr_vals, atr_multiplier=0.5)
    for i, line in enumerate(resistance_lines):
        # Extend line to current date
        start_idx = line['start_idx']
        end_idx = min(line['end_idx'] + 20, len(d_reset) - 1)  # Extend 20 bars or to end
        extended_val = line['start_val'] + line['slope'] * (end_idx - start_idx)
        
        fig.add_trace(go.Scatter(
            x=[d_original_index.index[start_idx], d_original_index.index[end_idx]],
            y=[line['start_val'], extended_val],
            mode='lines',
            name=f'Resistance ({line["touches"]} touches)',
            line=dict(color='#ef4444', width=1, dash='3,1.5'),
            showlegend=(i == 0)  # Only show legend for first resistance line
        ))
    
    fig.update_layout(title=f"{ticker} Price Pivots", height=515, margin=dict(t=40, b=80), legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.15), hovermode='x unified', xaxis=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikedash='dot', spikecolor='lightgrey'))
    return fig


def build_hl_figure(df, ticker, years=1, line_color='#ffffff'):
    """
    Build High/Low pivot chart with swing points and trendlines.
    
    Args:
        line_color: Color for the price line (default: white for AI Chart Analysis)
    """
    d = df[df.index >= df.index.max() - pd.DateOffset(years=years)].reset_index()
    d_original_index = d.set_index(d.columns[0])  # Preserve datetime index for plotting
    d_reset = d.reset_index(drop=True)  # Numeric index for trendline calculation
    
    # ATR-14 values for tolerance
    atr_vals = d_reset['ATR14'].values if 'ATR14' in d_reset.columns else None
    
    # Pivot ranges: 5,2 for weekly (5Y), 6,4 for daily (1Y/3Y)
    pivot_left = 5 if years == 5 else 6
    pivot_right = 2 if years == 5 else 4
    
    sh = identify_swing_points_high(d_reset, pivot_left, pivot_right)
    sl = identify_swing_points_low(d_reset, pivot_left, pivot_right)
    
    # Combine swing points for trendline detection
    sp = pd.concat([sh, sl]).sort_values('Index').reset_index(drop=True)
    
    fig = go.Figure()
    
    # Price line
    fig.add_trace(go.Scatter(x=d_original_index.index, y=[float(v) for v in d_original_index['Close']], mode='lines', name='Close', line=dict(color=line_color)))
    
    # SMA-20
    if 'SMA20' in d_original_index.columns:
        fig.add_trace(go.Scatter(x=list(d_original_index.index), y=[float(v) for v in d_original_index['SMA20']], mode='lines', name='SMA-20', line=dict(color='#fbbf24', width=1.5, dash='dot')))
    
    # Swing highs
    if not sh.empty:
        fig.add_trace(go.Scatter(x=d_original_index.index[sh['Index']], y=[float(v) for v in sh['Value']], mode='markers', name='Swing High', marker=dict(symbol='triangle-up', size=12, color='red')))
    
    # Swing lows
    if not sl.empty:
        fig.add_trace(go.Scatter(x=d_original_index.index[sl['Index']], y=[float(v) for v in sl['Value']], mode='markers', name='Swing Low', marker=dict(symbol='triangle-down', size=12, color='green')))
    
    # Find and draw support trendlines (green, dashed)
    support_lines = find_trendlines(d_reset, sp, trendline_type='support', min_touches=3, atr_values=atr_vals, atr_multiplier=0.5)
    for i, line in enumerate(support_lines):
        start_idx = line['start_idx']
        end_idx = min(line['end_idx'] + 20, len(d_reset) - 1)
        extended_val = line['start_val'] + line['slope'] * (end_idx - start_idx)
        
        fig.add_trace(go.Scatter(
            x=[d_original_index.index[start_idx], d_original_index.index[end_idx]],
            y=[line['start_val'], extended_val],
            mode='lines',
            name=f'Support ({line["touches"]} touches)',
            line=dict(color='#22c55e', width=1, dash='3,1.5'),
            showlegend=(i == 0)
        ))
    
    # Find and draw resistance trendlines (red, dashed)
    resistance_lines = find_trendlines(d_reset, sp, trendline_type='resistance', min_touches=3, atr_values=atr_vals, atr_multiplier=0.5)
    for i, line in enumerate(resistance_lines):
        start_idx = line['start_idx']
        end_idx = min(line['end_idx'] + 20, len(d_reset) - 1)
        extended_val = line['start_val'] + line['slope'] * (end_idx - start_idx)
        
        fig.add_trace(go.Scatter(
            x=[d_original_index.index[start_idx], d_original_index.index[end_idx]],
            y=[line['start_val'], extended_val],
            mode='lines',
            name=f'Resistance ({line["touches"]} touches)',
            line=dict(color='#ef4444', width=1, dash='3,1.5'),
            showlegend=(i == 0)
        ))
    
    fig.update_layout(title=f"{ticker} High/Low Pivots", height=515, margin=dict(t=40, b=80), legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.15), hovermode='x unified', xaxis=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikedash='dot', spikecolor='lightgrey'))
    return fig


def build_ema_figure(df, ticker, years=1):
    d = df[df.index >= df.index.max() - pd.DateOffset(years=years)]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['Close']], mode='lines', name='Close', line=dict(color='#ffffff')))
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['EMA13']], mode='lines', name='EMA13', line=dict(dash='dot', width=1.25, color='#60a5fa')))
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['EMA55']], mode='lines', name='EMA55', line=dict(dash='dot', width=1.25, color='red')))
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['EMA144']], mode='lines', name='EMA144', line=dict(dash='dot', width=1.25, color='green')))
    fig.update_layout(title=f"{ticker} EMA Stack", height=515, margin=dict(t=40, b=80), legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.15), hovermode='x unified', xaxis=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikedash='dot', spikecolor='lightgrey'))
    return fig


def build_rsi_figure(df, ticker, years=1):
    d = df[df.index >= df.index.max() - pd.DateOffset(years=years)]
    fig = go.Figure()

    # RSI zone background bands
    zones = [
        (80, 100, 'Overbought',   'rgba(239,68,68,0.15)'),   # red
        (55,  80, 'Bullish',      'rgba(34,197,94,0.08)'),    # green
        (45,  55, 'No Trade<br>Zone', 'rgba(148,163,184,0.08)'),  # grey (wrapped)
        (20,  45, 'Bearish',      'rgba(239,68,68,0.08)'),    # red
        ( 0,  20, 'Oversold',     'rgba(34,197,94,0.15)'),    # green (user requested)
    ]
    for y0, y1, label, color in zones:
        fig.add_hrect(y0=y0, y1=y1, fillcolor=color, line_width=0, layer='below')
        # Zone label on the right margin
        fig.add_annotation(
            x=1.02, y=(y0 + y1) / 2, xref='paper', yref='y',
            text=label, showarrow=False,
            font=dict(size=9, color='#94a3b8'),
            xanchor='left'
        )

    # RSI line
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['RSI14']], mode='lines', name='RSI', line=dict(color='#22d3ee', width=1.5)))
    # RSI EMA-13
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['RSI_EMA13']], mode='lines', name='RSI EMA-13', line=dict(dash='dot', width=1, color='#fbbf24')))

    fig.update_layout(
        title=f'{ticker} RSI', height=515,
        yaxis=dict(range=[0, 100]),
        legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.15),
        hovermode='x unified',
        margin=dict(t=40, b=80, r=100),  # room for zone labels, title up, legend down
        xaxis=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikedash='dot', spikecolor='lightgrey')
    )
    return fig


def build_adl_figure(df, ticker, years=1):
    from plotly.subplots import make_subplots
    d = df[df.index >= df.index.max() - pd.DateOffset(years=years)].copy()
    sp = identify_swing_points(d, 6, 4, 'Close')

    # Create 2-row subplots: top for price+SMA20, bottom for ADL+Volume
    # Bottom slightly taller as per user request (0.45 top, 0.55 bottom)
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.45, 0.55],
        subplot_titles=(f'{ticker} Price', 'ADL & Volume'),
        specs=[
            [{"secondary_y": False}],  # Top pane (Price)
            [{"secondary_y": True}]    # Bottom pane (ADL/Volume)
        ]
    )

    # === TOP PANE (Row 1): Close Price + SMA 20 ===
    fig.add_trace(
        go.Scatter(x=d.index, y=[float(v) for v in d['Close']], mode='lines', name='Close', line=dict(color='#ffffff')),
        row=1, col=1
    )
    if 'SMA20' in d.columns:
        fig.add_trace(
            go.Scatter(x=d.index, y=[float(v) for v in d['SMA20']], mode='lines', name='SMA-20', line=dict(color='#fbbf24', width=1.5, dash='dot')),
            row=1, col=1
        )

    # Add swing high markers
    highs = sp[sp['Type'] == 'High']
    if not highs.empty:
        fig.add_trace(
            go.Scatter(x=d.index[highs['Index']], y=[float(v) for v in highs['Value']], 
                       mode='markers', name='Swing High', 
                       marker=dict(symbol='triangle-up', size=10, color='red')),
            row=1, col=1
        )
    
    # Add swing low markers
    lows = sp[sp['Type'] == 'Low']
    if not lows.empty:
        fig.add_trace(
            go.Scatter(x=d.index[lows['Index']], y=[float(v) for v in lows['Value']], 
                       mode='markers', name='Swing Low', 
                       marker=dict(symbol='triangle-down', size=10, color='green')),
            row=1, col=1
        )

    # === BOTTOM PANE (Row 2): Volume & ADL ===
    # --- Volume bars (background, secondary_y=False) ---
    price_change = d['Close'].diff()
    vol_colors = ['rgba(34,197,94,0.55)' if pc >= 0 else 'rgba(239,68,68,0.55)' for pc in price_change.fillna(0)]
    fig.add_trace(
        go.Bar(x=d.index, y=[float(v) for v in d['Volume']], name='Volume',
               marker=dict(color=vol_colors, line=dict(width=0)), showlegend=True),
        secondary_y=False, row=2, col=1
    )

    # --- ADL & ADL EMA lines (secondary_y=True, on top) ---
    fig.add_trace(
        go.Scatter(x=d.index, y=[float(v) for v in d['ADL']], mode='lines', name='ADL',
                   line=dict(color='#3b82f6', width=1.5)),
        secondary_y=True, row=2, col=1
    )
    fig.add_trace(
        go.Scatter(x=d.index, y=[float(v) for v in d['ADL_EMA']], mode='lines', name='ADL EMA',
                   line=dict(dash='dot', width=1.25, color='orange')),
        secondary_y=True, row=2, col=1
    )

    # --- Axes Configuration ---
    fig.update_yaxes(title_text='Price', showgrid=True, row=1, col=1)
    # Hide Volume tick labels and grid
    fig.update_yaxes(title_text='', showticklabels=False, showgrid=False, secondary_y=False, row=2, col=1)
    # Put ADL labels on the left, but do not show overlapping grid
    fig.update_yaxes(title_text='ADL', secondary_y=True, side='left', showgrid=False, row=2, col=1)

    fig.update_layout(
        height=800,
        margin=dict(t=60, b=80),
        hovermode='x unified',
        legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.15),
        xaxis=dict(showspikes=True, spikemode='across', spikesnap='cursor',
                   spikethickness=1, spikedash='dot', spikecolor='lightgrey'),
        xaxis2=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor',
                    spikethickness=1, spikedash='dot', spikecolor='lightgrey')
    )
    return fig


def build_rs_figure(df, ticker, years=1):
    d = df[df.index >= df.index.max() - pd.DateOffset(years=years)]
    fig = go.Figure()

    rs_vals = np.array([float(v) for v in d['RS']])
    dates = list(d.index)

    # Positive RS (green): clip negatives to zero for fill
    rs_pos = np.where(rs_vals >= 0, rs_vals, 0)
    fig.add_trace(go.Scatter(
        x=dates, y=rs_pos.tolist(), mode='lines', name='RS (Outperforming)',
        line=dict(color='rgba(34,197,94,0.9)', width=1.5),
        fill='tozeroy', fillcolor='rgba(34,197,94,0.15)'
    ))

    # Negative RS (red): clip positives to zero for fill
    rs_neg = np.where(rs_vals < 0, rs_vals, 0)
    fig.add_trace(go.Scatter(
        x=dates, y=rs_neg.tolist(), mode='lines', name='RS (Underperforming)',
        line=dict(color='rgba(239,68,68,0.9)', width=1.5),
        fill='tozeroy', fillcolor='rgba(239,68,68,0.15)'
    ))

    # Zero reference line
    fig.add_hline(y=0, line_dash='dash', line_color='rgba(148,163,184,0.5)', line_width=1)

    fig.update_layout(
        title=f"{ticker} Relative Strength vs Nifty", height=515,
        margin=dict(t=40, b=80),
        legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.15),
        yaxis=dict(tickformat='.2%'),
        hovermode='x unified',
        xaxis=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor',
                   spikethickness=1, spikedash='dot', spikecolor='lightgrey')
    )
    return fig

# -------------------------------------------------------------------
# 3) Main Orchestration and Summary Functions
# -------------------------------------------------------------------

def evaluate_ticker_signal(ticker, in_position=False, price_pattern="", left_price=6, right_price=4, recency_candles=3, interval='daily', force_yf=False, years=5, idx_df=None):
    """
    Evaluate ticker signal with optional interval selection.
    
    Args:
        interval: 'daily' (default) or 'weekly' for 5-year charts
        force_yf: If True, bypass TradingView and use yfinance directly
        years: Number of years of historical data to fetch (default 5)
        idx_df: Pre-fetched NIFTY index DataFrame. If provided, skips the
                redundant NIFTY download (useful when batch-processing multiple tickers).
    """
    end = datetime.today()
    start = end - pd.DateOffset(years=years)
    sym = ticker.replace('.NS','')
    df = fetch_histogram(sym, 'NSE', start, end, interval=interval, force_yf=force_yf)
    if df.empty:
        return {"Ticker": ticker, "Signal": "NO DATA", "Data": df}
    if idx_df is None:
        idx_df = fetch_histogram('NIFTY', 'NSE', start, end, interval=interval, force_yf=force_yf)
    df, idx_df = align_data_indices(df, idx_df)
    if len(df) < left_price + right_price + 1:
        return {"Ticker": ticker, "Signal": "INSUFFICIENT DATA", "Data": df}
    
    # Indicators
    df['RS']     = calculate_relative_strength(df, idx_df)
    df['ATR14']  = talib.ATR(df.High, df.Low, df.Close, timeperiod=14)
    df['EMA13']  = talib.EMA(df.Close, timeperiod=13)
    df['SMA20']  = talib.SMA(df.Close, timeperiod=20)
    df['EMA21']  = talib.EMA(df.Close, timeperiod=21)
    df['EMA55']  = talib.EMA(df.Close, timeperiod=55)
    df['EMA144'] = talib.EMA(df.Close, timeperiod=144)
    df['RSI14']  = talib.RSI(df.Close, timeperiod=14)
    df['RSI_EMA13'] = talib.EMA(df['RSI14'], timeperiod=13)
    df['ADL']    = talib.AD(df.High, df.Low, df.Close, df.Volume)
    df['ADL_EMA']= talib.EMA(df.ADL, timeperiod=55)
    n = 3
    df['ADL_EMA_Slope'] = (df['ADL_EMA'] - df['ADL_EMA'].shift(n)) / n
    
    # Swing-based entry logic
    sp = identify_swing_points(df, left_price, right_price, 'Close')
    beh = analyze_swing_behaviour(sp[sp['Index'] <= len(df)-1])
    rs_positive = df['RS'].iat[-1] > 0 if not np.isnan(df['RS'].iat[-1]) else False
    price_ok    = beh['HH'] and beh['HL']
    if not in_position and rs_positive and price_ok:
        signal = "BUY"
    elif in_position and not (rs_positive and price_ok):
        signal = "SELL"
    else:
        signal = "HOLD"
    return {"Ticker": ticker, "Signal": signal, "Data": df}


def generate_summary(result):
    df = result.get('Data')
    if df is None or df.empty:
        return []
    
    # Get latest prices for override checks
    current_close = df['Close'].iat[-1]
    current_high = df['High'].iat[-1]
    current_low = df['Low'].iat[-1]
    
    # --- Price Action Analysis (based on Close prices) ---
    sw_close = identify_swing_points(df, 6, 4, 'Close')
    beh_close = analyze_swing_behaviour(sw_close)
    
    # Apply override logic
    close_highs = sw_close[sw_close['Type'] == 'High']
    close_lows = sw_close[sw_close['Type'] == 'Low']
    if not close_highs.empty and current_close > close_highs['Value'].iloc[-1]:
        beh_close['HH'] = True
        beh_close['LH'] = False
    if not close_lows.empty and current_close < close_lows['Value'].iloc[-1]:
        beh_close['LL'] = True
        beh_close['HL'] = False

    # Generate final trend and tokens from the *corrected* behavior
    pt_close = compute_price_action_trend_from_behaviour(beh_close)
    tk_close = get_swing_tokens_from_behaviour(beh_close)

    # --- Price Action Analysis (based on High/Low prices) ---
    sh = identify_swing_points_high(df, 6, 4)
    sl = identify_swing_points_low(df, 6, 4)
    sw_hl = pd.concat([sh, sl]).sort_values('Index')
    beh_hl = analyze_swing_behaviour(sw_hl)
    
    # ===================================================================
    # START: High/Low Price Action Override Logic
    # ===================================================================
    # Apply override logic using the latest high and low prices
    if not sh.empty and current_high > sh['Value'].iloc[-1]:
        beh_hl['HH'] = True
        beh_hl['LH'] = False
    if not sl.empty and current_low < sl['Value'].iloc[-1]:
        beh_hl['LL'] = True
        beh_hl['HL'] = False
    # ===================================================================
    # END: High/Low Price Action Override Logic
    # ===================================================================

    # Generate final trend and tokens from the *corrected* High/Low behavior
    pt_hl = compute_price_action_trend_from_behaviour(beh_hl)
    tk_hl = get_swing_tokens_from_behaviour(beh_hl)
    
    # --- Other Indicators ---
    mstr = compute_market_structure(df)
    e13, e55, e144 = df['EMA13'].iat[-1], df['EMA55'].iat[-1], df['EMA144'].iat[-1]
    estack = f"EMA13 {'>' if e13>e55 else '<'} EMA55 {'>' if e55>e144 else '<'} EMA144"
    fs = compute_fib_strength(sw_close, pt_close, current_close, df)
    
    rsi = df['RSI14'].iat[-1]
    sentiment = 'Bullish' if rsi > 55 else ('Bearish' if rsi < 45 else 'Neutral')
    if rsi > 80: sentiment = 'Overbought'
    elif rsi < 20: sentiment = 'Oversold'
    else:
        recent = df['RSI14'].iloc[-4:]
        cross_down = ((recent.shift(1) > 80) & (recent <= 80)).any()
        cross_up = ((recent.shift(1) < 20) & (recent >= 20)).any()
        if cross_down: sentiment = 'Price has Topped Out'
        elif cross_up: sentiment = 'Price has Bottomed Out'
        else: sentiment = 'Bullish' if rsi > 55 else ('Bearish' if rsi < 45 else 'Neutral')
            
    adl_condition = (df['ADL_EMA_Slope'].iat[-1] > 0) if not np.isnan(df['ADL_EMA_Slope'].iat[-1]) else False
    rs_positive = df['RS'].iat[-1] > 0 if not np.isnan(df['RS'].iat[-1]) else False
    sup, res = find_support_resistance(sw_close, df)
    
    # Detect RSI divergences
    div_result = detect_rsi_divergence(df, L=6, R=4, lookback_pivots=5)
    div_type = div_result['divergence_type']
    div_date = div_result['last_date']
    div_display = f"{div_type}" if div_date is None else f"{div_type} (Last: {div_date})"
    
    rows = [
        ("Current Price", f"{current_close:.2f}"),
        ("Price-Action Trend (based on Close prices)", f"{pt_close} ({', '.join(tk_close)})"),
        ("Price-Action Trend (based on High/Low prices)", f"{pt_hl} ({', '.join(tk_hl)})"),
        ("Trend Strength (based on Fibonacci retracement)", fs),
        ("Market Structure (based on EMA Stack)", f"{mstr} ({estack})"),
        ("Market Sentiment (based on RSI)", f"{sentiment} (RSI: {rsi:.2f})"),
        ("Hidden Trend Divergence (based on RSI)", div_display),
        ("Relative Strength vs Nifty", f"{'Positive' if rs_positive else 'Negative'}"),
        ("Accumulating or Distributing (based on Volume)", f"{'Accumulating' if adl_condition else 'Distributing'}")
    ]
    if sup: rows.append(("Support Zone", f"{sup[0]:.2f} – {sup[1]:.2f}"))
    if res: rows.append(("Resistance Zone", f"{res[0]:.2f} – {res[1]:.2f}"))
    return [{"key": k, "value": v} for k, v in rows]


def generate_per_chart_summaries(result):
    """
    Generate organic, natural-language summaries for each of the 7 chart types.
    Returns a dict keyed by chart tab name.
    All rules-based — no AI API calls needed.
    """
    df = result.get('Data')
    if df is None or df.empty:
        return {}

    current_close = float(df['Close'].iat[-1])
    current_high = float(df['High'].iat[-1])
    current_low = float(df['Low'].iat[-1])

    # ── Close Price Swing Analysis ──────────────────────────────────
    sw_close = identify_swing_points(df, 6, 4, 'Close')
    beh_close = analyze_swing_behaviour(sw_close)

    close_highs = sw_close[sw_close['Type'] == 'High']
    close_lows = sw_close[sw_close['Type'] == 'Low']
    if not close_highs.empty and current_close > close_highs['Value'].iloc[-1]:
        beh_close['HH'] = True; beh_close['LH'] = False
    if not close_lows.empty and current_close < close_lows['Value'].iloc[-1]:
        beh_close['LL'] = True; beh_close['HL'] = False

    pt_close = compute_price_action_trend_from_behaviour(beh_close)

    # ── High/Low Swing Analysis ─────────────────────────────────────
    sh = identify_swing_points_high(df, 6, 4)
    sl = identify_swing_points_low(df, 6, 4)
    sw_hl = pd.concat([sh, sl]).sort_values('Index')
    beh_hl = analyze_swing_behaviour(sw_hl)

    if not sh.empty and current_high > sh['Value'].iloc[-1]:
        beh_hl['HH'] = True; beh_hl['LH'] = False
    if not sl.empty and current_low < sl['Value'].iloc[-1]:
        beh_hl['LL'] = True; beh_hl['HL'] = False

    pt_hl = compute_price_action_trend_from_behaviour(beh_hl)

    # ── Shared indicators ───────────────────────────────────────────
    mstr = compute_market_structure(df)
    e13 = float(df['EMA13'].iat[-1])
    e55 = float(df['EMA55'].iat[-1])
    e144 = float(df['EMA144'].iat[-1])
    rsi = float(df['RSI14'].iat[-1])
    rsi_ema = float(df['RSI_EMA13'].iat[-1])
    adl_slope = float(df['ADL_EMA_Slope'].iat[-1]) if not np.isnan(df['ADL_EMA_Slope'].iat[-1]) else 0.0
    adl_current = float(df['ADL'].iat[-1])
    adl_ema_current = float(df['ADL_EMA'].iat[-1])
    rs_val = float(df['RS'].iat[-1]) if not np.isnan(df['RS'].iat[-1]) else 0.0
    sup, res = find_support_resistance(sw_close, df)
    div_result = detect_rsi_divergence(df, L=6, R=4, lookback_pivots=5)

    # ── Pattern labels (matching AI Chart Analysis table) ───────────
    tk_close = get_swing_tokens_from_behaviour(beh_close)
    tk_hl = get_swing_tokens_from_behaviour(beh_hl)
    estack = f"EMA13 {'>' if e13 > e55 else '<'} EMA55 {'>' if e55 > e144 else '<'} EMA144"
    # RSI sentiment
    if rsi > 80: rsi_sentiment = 'Overbought'
    elif rsi < 20: rsi_sentiment = 'Oversold'
    elif rsi > 55: rsi_sentiment = 'Bullish'
    elif rsi < 45: rsi_sentiment = 'Bearish'
    else: rsi_sentiment = 'Neutral'
    adl_label = 'Accumulating' if (adl_slope > 0 and adl_current > adl_ema_current) else 'Distributing'
    rs_label = 'Positive' if rs_val > 0 else 'Negative'
    div_type = div_result['divergence_type']
    div_date = div_result.get('last_date')
    div_label = f"{div_type}" if div_date is None else f"{div_type} (Last: {div_date})"

    # === 1. Close Price Swings Summary ==============================
    def _close_summary():
        s0 = "This chart plots algorithmically detected swing highs and lows from closing prices, along with validated trendlines and support/resistance zones — a higher high and higher low sequence confirms an uptrend, while the reverse confirms a downtrend."
        last_sh_val = f"₹{float(close_highs['Value'].iloc[-1]):,.2f}" if not close_highs.empty else None
        last_sl_val = f"₹{float(close_lows['Value'].iloc[-1]):,.2f}" if not close_lows.empty else None

        # Trendline context
        d_reset = df.reset_index(drop=True)
        sp_for_tl = identify_swing_points(d_reset, 6, 4, 'Close')
        atr_vals_summary = d_reset['ATR14'].values if 'ATR14' in d_reset.columns else None
        sup_lines = find_trendlines(d_reset, sp_for_tl, 'support', 3, atr_values=atr_vals_summary, atr_multiplier=0.5)
        res_lines = find_trendlines(d_reset, sp_for_tl, 'resistance', 3, atr_values=atr_vals_summary, atr_multiplier=0.5)

        # Build sentence 1: Trend
        if pt_close == 'Uptrend':
            if beh_close.get('HH') and beh_close.get('HL'):
                s1 = f"The stock is in a clear uptrend — close prices are making higher highs and higher lows, a classic sign of bullish momentum."
            else:
                s1 = f"The stock is trending upward based on its closing price structure."
        elif pt_close == 'Downtrend':
            if beh_close.get('LH') and beh_close.get('LL'):
                s1 = f"The stock is in a downtrend — close prices are printing lower highs and lower lows, reflecting sustained selling pressure."
            else:
                s1 = f"The stock is trending lower based on its closing price structure."
        else:  # Sideways
            if beh_close.get('HH') and beh_close.get('LL'):
                s1 = "The price action is showing mixed signals — a higher high but a lower low suggests an expanding range with no clear directional bias."
            elif beh_close.get('LH') and beh_close.get('HL'):
                s1 = "The stock is consolidating in a narrowing range — lower highs and higher lows hint at a breakout or breakdown ahead."
            else:
                s1 = "The stock is trading sideways, with no clear trend emerging from recent swing points."

        # Build sentence 2: Recent pivots
        parts = []
        if last_sh_val:
            parts.append(f"the last swing high was at {last_sh_val}")
        if last_sl_val:
            parts.append(f"the last swing low was at {last_sl_val}")
        s2 = f"At ₹{current_close:,.2f}, {' and '.join(parts)}." if parts else ""

        # Build sentence 3: Trendlines
        tl_parts = []
        if sup_lines:
            best_sup = sup_lines[0]
            tl_parts.append(f"a support trendline with {best_sup['touches']} touches {'(rising)' if best_sup['slope'] > 0 else '(falling)'}")
        if res_lines:
            best_res = res_lines[0]
            tl_parts.append(f"a resistance trendline with {best_res['touches']} touches {'(rising)' if best_res['slope'] > 0 else '(falling)'}")
        if tl_parts:
            s3 = f"The chart shows {' and '.join(tl_parts)}."
        else:
            s3 = "No significant trendlines with 3 or more touches were detected in the current range."

        # Build sentence 4: S/R zones
        if sup and res:
            s4 = f"Immediate support sits at ₹{sup[0]:,.2f}–₹{sup[1]:,.2f} and resistance at ₹{res[0]:,.2f}–₹{res[1]:,.2f}."
        elif sup:
            s4 = f"Support is near ₹{sup[0]:,.2f}–₹{sup[1]:,.2f}, with no clear overhead resistance from recent pivots."
        elif res:
            s4 = f"Resistance is near ₹{res[0]:,.2f}–₹{res[1]:,.2f}, with no clear support floor from recent pivots."
        else:
            s4 = ""

        header = f"<strong>Price Action Trend: {pt_close} ({', '.join(tk_close)})</strong><br>"
        return header + " ".join(filter(None, [s0, s1, s2, s3, s4]))

    # === 2. High/Low Swings Summary =================================
    def _hl_summary():
        s0 = "Unlike close-price swings, this chart uses intraday highs and lows to detect pivots — capturing wicks and intraday extremes that closing prices miss, revealing hidden buying or selling pressure."
        # H/L trend
        if pt_hl == 'Uptrend':
            s1 = "Intraday highs and lows confirm the uptrend — the stock is making higher highs on its daily highs and higher lows on its daily lows."
        elif pt_hl == 'Downtrend':
            s1 = "The high/low pivot structure is bearish — daily highs are getting lower and daily lows keep dropping, which shows sellers are firmly in control."
        else:
            if beh_hl.get('HH') and beh_hl.get('LL'):
                s1 = "Intraday extremes are expanding — higher highs but lower lows suggest increased volatility without a clear trend."
            elif beh_hl.get('LH') and beh_hl.get('HL'):
                s1 = "The high/low range is compressing — lower highs and higher lows form a wedge, often a precursor to a sharp move."
            else:
                s1 = "High/low pivots are not showing a clear directional trend at the moment."

        # Sentence 2: Confirmation or divergence vs Close trend
        if pt_close == pt_hl:
            if pt_close == 'Uptrend':
                s2 = "This aligns with the close-price trend, adding conviction to the bullish case."
            elif pt_close == 'Downtrend':
                s2 = "This confirms the close-price downtrend — both perspectives agree the bears have the upper hand."
            else:
                s2 = "Both close-based and high/low-based analysis agree the stock is rangebound."
        else:
            if pt_close == 'Uptrend' and pt_hl != 'Uptrend':
                s2 = f"Interestingly, while closing prices suggest an uptrend, the intraday highs and lows tell a different story ({pt_hl.lower()}) — watch for potential exhaustion."
            elif pt_close == 'Downtrend' and pt_hl != 'Downtrend':
                s2 = f"The close-price structure looks bearish, but intraday pivots show {pt_hl.lower()} behavior — this divergence may signal a reversal brewing."
            elif pt_hl == 'Uptrend' and pt_close != 'Uptrend':
                s2 = f"The high/low pivots are bullish, even though closing prices appear {pt_close.lower()} — intraday buying pressure is quietly building."
            elif pt_hl == 'Downtrend' and pt_close != 'Downtrend':
                s2 = f"Despite the close-price trend looking {pt_close.lower()}, intraday highs and lows are trending down — a subtle warning sign."
            else:
                s2 = f"There is a slight divergence between close-based ({pt_close}) and high/low-based ({pt_hl}) analysis — worth monitoring."

        # Sentence 3: Key levels
        last_sh = f"₹{float(sh['Value'].iloc[-1]):,.2f}" if not sh.empty else None
        last_sl = f"₹{float(sl['Value'].iloc[-1]):,.2f}" if not sl.empty else None
        if last_sh and last_sl:
            s3 = f"The most recent intraday swing high is {last_sh} and the swing low is {last_sl}."
        elif last_sh:
            s3 = f"The most recent intraday swing high is at {last_sh}."
        elif last_sl:
            s3 = f"The most recent intraday swing low is at {last_sl}."
        else:
            s3 = ""

        header = f"<strong>Price Action Trend: {pt_hl} ({', '.join(tk_hl)})</strong><br>"
        return header + " ".join(filter(None, [s0, s1, s2, s3]))

    # === 3. EMA Stack Summary =======================================
    def _ema_summary():
        s0 = "The EMA Stack overlays three Exponential Moving Averages (13, 55, and 144-period) to reveal the multi-timeframe trend structure — when faster EMAs are stacked above slower ones, trend-followers are in control; the spacing between them signals momentum strength."
        price_vs_emas = ""
        if current_close > e13 > e55 > e144:
            price_vs_emas = "above all three EMAs with a perfectly stacked bullish alignment"
        elif current_close > e13 and current_close > e55 and current_close < e144:
            price_vs_emas = "above the short and mid-term EMAs but still below the long-term EMA-144 — a recovery in progress"
        elif current_close > e13 and current_close < e55:
            price_vs_emas = "above the fast EMA-13 but below the medium EMA-55, suggesting early-stage recovery"
        elif current_close < e13 < e55 < e144:
            price_vs_emas = "below all three EMAs with a fully bearish stack — sellers dominate at every timeframe"
        elif current_close < e13 and current_close < e55 and current_close > e144:
            price_vs_emas = "below the short and mid-term EMAs but holding above EMA-144 — the long-term trend is still intact"
        elif current_close < e13 and current_close > e55:
            price_vs_emas = "slipping below the fast EMA-13 but holding above EMA-55, which could mean a short-term pullback within a larger trend"
        else:
            price_vs_emas = f"in a mixed position relative to its EMAs"

        trend_flavor = {
            'Uptrend': "the EMAs are stacked bullishly (EMA-13 > EMA-55 > EMA-144), signaling a healthy uptrend",
            'Downtrend': "the EMAs are in bearish order (EMA-13 < EMA-55 < EMA-144), confirming a sustained downtrend",
            'Mild Uptrend': "the short-term EMA-13 has crossed above EMA-55 but EMA-55 is still below EMA-144 — an early bullish shift",
            'Mild Downtrend': "the short-term EMA-13 has slipped below EMA-55 but EMA-55 is still above EMA-144 — early weakness is creeping in",
            'Sideways': "the EMAs are tangled with no clear ordering, which typically means the stock is in a choppy, directionless phase"
        }


        s1 = f"The price is trading {price_vs_emas}. Currently, {trend_flavor.get(mstr, 'the EMA structure is mixed')}."

        # Sentence 2: EMA spacing (trend strength)
        spread_13_55 = abs(e13 - e55) / e55 * 100
        spread_55_144 = abs(e55 - e144) / e144 * 100

        if spread_13_55 > 5 and spread_55_144 > 5:
            s2 = f"The EMAs are well-separated ({spread_13_55:.1f}% between EMA-13 and EMA-55), indicating strong trend momentum."
        elif spread_13_55 < 1.5 and spread_55_144 < 2:
            s2 = "The EMAs are tightly bunched together, which often precedes a significant breakout or breakdown."
        elif spread_13_55 < 2:
            s2 = "EMA-13 and EMA-55 are converging, suggesting weakening short-term trend momentum."
        else:
            s2 = f"EMA spacing is moderate ({spread_13_55:.1f}% between EMA-13 and EMA-55), suggesting a steady but not overheated trend."

        # Sentence 3: EMA crosses
        crosses = detect_ema_crosses(df)
        if crosses:
            last_cross = crosses[-1]
            cross_type = "Golden Cross (EMA-13 crossed above EMA-144)" if last_cross.startswith("GC") else "Death Cross (EMA-13 crossed below EMA-144)"
            cross_bar = int(last_cross.split("@")[1])
            bars_ago = len(df) - 1 - cross_bar
            if bars_ago <= 10:
                s3 = f"A recent {cross_type} occurred just {bars_ago} sessions ago — this is a fresh signal worth watching closely."
            elif bars_ago <= 30:
                s3 = f"A {cross_type} occurred {bars_ago} sessions ago and the trend has been developing since."
            else:
                s3 = f"The last major EMA cross was a {cross_type} about {bars_ago} sessions ago."
        else:
            s3 = "No Golden Cross or Death Cross has occurred in the visible range."

        header = f"<strong>Market Structure: {mstr} ({estack})</strong><br>"
        return header + " ".join([s0, s1, s2, s3])

    # === 4. RSI Summary =============================================
    def _rsi_summary():
        s0 = "The RSI (Relative Strength Index, 14-period) measures the speed and magnitude of recent price changes on a 0–100 scale — readings above 70 flag overbought conditions, below 30 flag oversold, and the 13-period RSI EMA smooths out noise to reveal the underlying momentum trend."
        if rsi > 80:
            s1 = f"RSI is at {rsi:.1f}, deep in overbought territory. The stock has rallied hard and is statistically due for a breather — though in strong trends, RSI can stay elevated for extended periods."
        elif rsi > 70:
            s1 = f"RSI at {rsi:.1f} has entered the overbought zone (above 70). Momentum is strong, but caution is warranted as the stock may be stretched."
        elif rsi > 55:
            s1 = f"RSI at {rsi:.1f} sits in bullish territory. Buying momentum is healthy without being overextended — this is the sweet spot for trending stocks."
        elif rsi > 45:
            s1 = f"RSI at {rsi:.1f} is in neutral territory, reflecting a balance between buyers and sellers. The stock could go either way from here."
        elif rsi > 30:
            s1 = f"RSI at {rsi:.1f} is in bearish territory. Selling momentum has the upper hand, though the stock isn't yet oversold."
        elif rsi > 20:
            s1 = f"RSI at {rsi:.1f} has entered oversold territory (below 30). The stock has been heavily sold and may be setting up for a bounce."
        else:
            s1 = f"RSI at {rsi:.1f} is deeply oversold. Selling has been extreme — historically, such low RSI readings often precede sharp reversals."

        # Sentence 2: RSI vs RSI EMA (momentum direction)
        if rsi > rsi_ema:
            diff = rsi - rsi_ema
            if diff > 8:
                s2 = "RSI is sharply above its 13-period EMA, indicating a strong burst of momentum that may not be sustainable."
            elif diff > 3:
                s2 = "RSI is comfortably above its EMA, confirming that short-term momentum is expanding in favor of the bulls."
            else:
                s2 = "RSI is slightly above its EMA-13, showing marginal bullish momentum."
        else:
            diff = rsi_ema - rsi
            if diff > 8:
                s2 = "RSI has fallen well below its 13-period EMA, signaling an aggressive shift in momentum toward the bears."
            elif diff > 3:
                s2 = "RSI is below its EMA, suggesting momentum is fading and sellers are stepping in."
            else:
                s2 = "RSI is slightly below its EMA-13, hinting at mild short-term weakness."

        # Sentence 3: Zone transitions
        recent = df['RSI14'].iloc[-5:]
        crossed_below_80 = ((recent.shift(1) > 80) & (recent <= 80)).any()
        crossed_above_20 = ((recent.shift(1) < 20) & (recent >= 20)).any()
        crossed_below_70 = ((recent.shift(1) > 70) & (recent <= 70)).any()
        crossed_above_30 = ((recent.shift(1) < 30) & (recent >= 30)).any()

        if crossed_below_80:
            s3 = "Notably, RSI just dropped below 80 — often an early signal that the rally is losing steam."
        elif crossed_above_20:
            s3 = "RSI has just climbed back above 20 after being deeply oversold — this could be the first sign of a bottoming process."
        elif crossed_below_70:
            s3 = "RSI recently fell below 70, exiting the overbought zone — early profit-taking may be underway."
        elif crossed_above_30:
            s3 = "RSI has recovered above 30, leaving the oversold zone — buyers may be stepping back in."
        else:
            # Hidden momentum observation
            if pt_close == 'Uptrend' and rsi > 40 and rsi < 55:
                s3 = "In an uptrend, RSI holding above 40 (even while neutral) is a sign of underlying strength — dips are being bought."
            elif pt_close == 'Downtrend' and rsi < 60 and rsi > 45:
                s3 = "In a downtrend, RSI failing to push above 60 is a sign of persistent weakness — rallies are being sold into."
            else:
                s3 = ""

        header = f"<strong>Market Sentiment: {rsi_sentiment} (RSI: {rsi:.1f})</strong><br>"
        return header + " ".join(filter(None, [s0, s1, s2, s3]))

    # === 5. ADL Summary =============================================
    def _adl_summary():
        s0 = "The Accumulation/Distribution Line tracks cumulative money flow using price and volume — when the stock closes near its high on strong volume, it signals accumulation (institutional buying); closes near the low signal distribution (institutional selling). The ADL EMA-55 smooths this to reveal the underlying flow trend."
        # Accumulation or Distribution
        adl_above_ema = adl_current > adl_ema_current

        if adl_slope > 0 and adl_above_ema:
            s1 = "The Accumulation/Distribution Line is rising and above its 55-period EMA — institutional money appears to be accumulating shares."
        elif adl_slope > 0 and not adl_above_ema:
            s1 = "The ADL slope has turned positive, though it's still below its EMA. Early accumulation may be starting, but it's not confirmed yet."
        elif adl_slope < 0 and not adl_above_ema:
            s1 = "The ADL is falling and below its EMA — volume-weighted flow suggests institutions are distributing (selling) shares."
        elif adl_slope < 0 and adl_above_ema:
            s1 = "The ADL slope has turned negative even while above its EMA. The pace of accumulation is slowing — a potential early warning."
        else:
            s1 = "The ADL is flat, indicating neither strong accumulation nor distribution — volume conviction is low."

        # Sentence 2: Confirmation or divergence with price
        if pt_close == 'Uptrend' and adl_slope > 0:
            s2 = "This confirms the price uptrend — both price and volume-weighted flow are moving higher together, which is healthy."
        elif pt_close == 'Uptrend' and adl_slope <= 0:
            s2 = "⚠️ Caution: Price is trending up, but the ADL is declining. This bearish divergence suggests the rally may lack volume conviction and could be fragile."
        elif pt_close == 'Downtrend' and adl_slope < 0:
            s2 = "The falling ADL confirms the downtrend — volume is following price lower, which makes a quick reversal less likely."
        elif pt_close == 'Downtrend' and adl_slope >= 0:
            s2 = "Interestingly, the ADL is rising even as price trends down. This bullish divergence could mean smart money is quietly accumulating at lower prices."
        elif pt_close == 'Sideways' and adl_slope > 0:
            s2 = "While price is going nowhere, the ADL is trending up — accumulation during a consolidation often precedes an upside breakout."
        elif pt_close == 'Sideways' and adl_slope < 0:
            s2 = "Price is consolidating but the ADL is weakening — distribution during a range can foreshadow a breakdown."
        else:
            s2 = "Volume-weighted flow is neutral, offering no additional edge in either direction."

        # Sentence 3: ADL crossover
        adl_prev = float(df['ADL'].iloc[-2]) if len(df) >= 2 else adl_current
        adl_ema_prev = float(df['ADL_EMA'].iloc[-2]) if len(df) >= 2 else adl_ema_current
        just_crossed_up = adl_prev < adl_ema_prev and adl_current >= adl_ema_current
        just_crossed_down = adl_prev > adl_ema_prev and adl_current <= adl_ema_current

        if just_crossed_up:
            s3 = "The ADL just crossed above its EMA — a fresh bullish signal from the volume side."
        elif just_crossed_down:
            s3 = "The ADL just crossed below its EMA — volume momentum has shifted bearish."
        else:
            s3 = ""

        header = f"<strong>Volume Flow: {adl_label}</strong><br>"
        return header + " ".join(filter(None, [s0, s1, s2, s3]))

    # === 6. Relative Strength Summary ===============================
    def _rs_summary():
        s0 = "Relative Strength measures the stock's excess return over Nifty 50 across a 55-day rolling window — a positive RS means the stock is outperforming the benchmark index, negative means underperforming. Institutional investors often rotate capital into stocks showing improving relative strength."
        rs_pct = rs_val * 100

        # RS direction (slope over last 10 bars)
        rs_series = df['RS'].dropna()
        if len(rs_series) >= 10:
            rs_recent = rs_series.iloc[-10:]
            rs_slope = (float(rs_recent.iloc[-1]) - float(rs_recent.iloc[0])) / 10
            rs_rising = rs_slope > 0.001
            rs_falling = rs_slope < -0.001
        else:
            rs_rising = False
            rs_falling = False

        # Sentence 1: Current RS
        if rs_val > 0:
            if rs_pct > 10:
                s1 = f"The stock is significantly outperforming Nifty by {rs_pct:.1f}% over the lookback period — it's a clear market leader."
            elif rs_pct > 3:
                s1 = f"The stock is outperforming Nifty by {rs_pct:.1f}%, showing solid relative strength."
            else:
                s1 = f"The stock is marginally outperforming Nifty (+{rs_pct:.1f}%), roughly in line with the broader market."
        else:
            if rs_pct < -10:
                s1 = f"The stock is significantly underperforming Nifty by {abs(rs_pct):.1f}% — it's lagging the broader market considerably."
            elif rs_pct < -3:
                s1 = f"The stock is underperforming Nifty by {abs(rs_pct):.1f}%, suggesting relative weakness."
            else:
                s1 = f"The stock is marginally underperforming Nifty ({rs_pct:.1f}%), roughly tracking the index."

        # Sentence 2: Regime classification
        if rs_val > 0 and rs_rising:
            s2 = "Relative strength is positive and improving — this is the best-case scenario, where the stock is a leading outperformer. Institutional flows tend to favor such stocks."
        elif rs_val > 0 and rs_falling:
            s2 = "While still outperforming, relative strength is declining. The stock's edge over the market is narrowing — if this continues, it may slip into underperformance."
        elif rs_val > 0:
            s2 = "Relative strength is positive and stable — the stock is consistently doing better than Nifty."
        elif rs_val < 0 and rs_falling:
            s2 = "Relative strength is negative and deteriorating further — the stock is falling out of favor versus the broader market. Avoid catching a falling knife."
        elif rs_val < 0 and rs_rising:
            s2 = "Although still underperforming, relative strength is improving. This could be an early sign of a turnaround — worth watching if the RS line crosses into positive territory."
        else:
            s2 = "Relative strength is negative but stable — the stock is consistently lagging Nifty."

        # Sentence 3: Consecutive positive/negative streak
        streak = 0
        streak_positive = rs_val > 0
        for i in range(len(rs_series) - 1, -1, -1):
            v = float(rs_series.iloc[i])
            if (streak_positive and v > 0) or (not streak_positive and v <= 0):
                streak += 1
            else:
                break
        if streak > 20:
            word = "outperformance" if streak_positive else "underperformance"
            s3 = f"This {word} streak has now lasted {streak} consecutive sessions — a durable trend that's unlikely to reverse quickly."
        else:
            s3 = ""

        header = f"<strong>Relative Strength vs Nifty: {rs_label}</strong><br>"
        return header + " ".join(filter(None, [s0, s1, s2, s3]))

    # === 7. RSI Divergence Summary ==================================
    def _rsi_div_summary():
        s0 = "This dual-pane chart compares price swing pivots against RSI swing pivots to detect hidden divergences — when price makes a new low but RSI doesn't, selling momentum is fading (bullish divergence); when price makes a new high but RSI doesn't, buying conviction is weakening (bearish divergence)."
        divs = div_result['divergences']
        div_type = div_result['divergence_type']
        div_date = div_result.get('last_date')

        if not divs:
            # No divergence — contextual message based on current trend
            if pt_close == 'Uptrend' and rsi > 50:
                s1 = "No RSI divergence is detected. Price and RSI are moving in sync, which is healthy — the current uptrend has genuine momentum behind it."
            elif pt_close == 'Downtrend' and rsi < 50:
                s1 = "No RSI divergence is detected. Both price and RSI are declining together, confirming the downtrend is driven by real selling pressure."
            else:
                s1 = "No RSI divergence is detected in the current range. Price and momentum are broadly aligned."
            return s1

        # Count divergences by type
        bull_divs = [d for d in divs if d['type'] == 'bullish']
        bear_divs = [d for d in divs if d['type'] == 'bearish']

        # Most recent divergence
        most_recent = sorted(divs, key=lambda x: x['end_idx'], reverse=True)[0]
        mr_type = most_recent['type']

        # Sentence 1: What was detected
        if mr_type == 'bullish':
            s1 = (
                f"A bullish RSI divergence was detected (last on {div_date}). "
                f"Price made a lower low (₹{most_recent['start_price']:,.2f} → ₹{most_recent['end_price']:,.2f}) while RSI made a higher low "
                f"({most_recent['start_rsi']:.1f} → {most_recent['end_rsi']:.1f}) — selling momentum is weakening even as prices drop."
            )
        else:
            s1 = (
                f"A bearish RSI divergence was detected (last on {div_date}). "
                f"Price made a higher high (₹{most_recent['start_price']:,.2f} → ₹{most_recent['end_price']:,.2f}) while RSI made a lower high "
                f"({most_recent['start_rsi']:.1f} → {most_recent['end_rsi']:.1f}) — buying momentum is fading despite higher prices."
            )

        # Sentence 2: Strength / multiplicity
        rsi_diff = abs(most_recent['end_rsi'] - most_recent['start_rsi'])
        total_type = len(bull_divs) if mr_type == 'bullish' else len(bear_divs)
        if total_type >= 3:
            s2 = f"Multiple {mr_type} divergences ({total_type} instances) have formed, which strengthens the conviction of this signal."
        elif rsi_diff > 10:
            s2 = f"The RSI differential is significant ({rsi_diff:.1f} points), making this a high-conviction divergence."
        elif rsi_diff > 5:
            s2 = f"The RSI gap is moderate ({rsi_diff:.1f} points), suggesting a meaningful shift in underlying momentum."
        else:
            s2 = f"The RSI gap is small ({rsi_diff:.1f} points) — this is a mild divergence that needs confirmation from price action."

        # Sentence 3: Educational context
        if mr_type == 'bullish':
            s3 = "Bullish divergences signal that sellers are losing conviction — a potential reversal or at least a strong bounce may follow, especially if price reclaims the last swing low."
        else:
            s3 = "Bearish divergences warn that the rally is running on fumes — even if prices push higher briefly, the underlying momentum doesn't support it. Watch for a break below recent support."

        header = f"<strong>Hidden Trend Divergence: {div_label}</strong><br>"
        return header + " ".join([s0, s1, s2, s3])

    # === Build and return ============================================
    return {
        'close': _close_summary(),
        'hl': _hl_summary(),
        'ema': _ema_summary(),
        'rsi': _rsi_summary(),
        'adl': _adl_summary(),
        'rs': _rs_summary(),
        'rsi_div': _rsi_div_summary()
    }