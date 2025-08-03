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

# Initialize TradingView datafeed here, as it's used by fetch_histogram
tv = TvDatafeed()

# -------------------------------------------------------------------
# 1) Data Fetching and Technical Calculation Helpers
# -------------------------------------------------------------------

def fetch_histogram(symbol, exchange, start_date, end_date, max_retries=3):
    n_bars = max(1100, (end_date - start_date).days + 5)
    raw = None
    for _ in range(max_retries):
        try:
            raw = tv.get_hist(symbol=symbol, exchange=exchange,
                              interval=Interval.in_daily, n_bars=n_bars)
            if raw is not None and not raw.empty:
                break
        except Exception:
            time.sleep(1)
    if raw is None or raw.empty:
        yf_sym = '^NSEI' if symbol.upper()=='NIFTY' and exchange=='NSE' else f"{symbol}.NS"
        try:
            raw = yf.download(yf_sym,
                              start=start_date.strftime('%Y-%m-%d'),
                              end=end_date.strftime('%Y-%m-%d'),
                              interval='1d', auto_adjust=False)
        except Exception:
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
# 2) Chart Generation Functions
# -------------------------------------------------------------------

def build_close_figure(df, ticker):
    d = df[df.index >= df.index.max() - pd.DateOffset(years=1)]
    sp = identify_swing_points(d, 6, 4, 'Close')
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=list(d.index), y=[float(v) for v in d['Close']], mode='lines', name='Close', line=dict(color='black')))
    highs = sp[sp['Type']=='High']
    fig.add_trace(go.Scatter(x=d.index[highs['Index']], y=[float(v) for v in highs['Value']], mode='markers', name='Swing High', marker=dict(symbol='triangle-up', size=12, color='red')))
    lows = sp[sp['Type']=='Low']
    fig.add_trace(go.Scatter(x=d.index[lows['Index']], y=[float(v) for v in lows['Value']], mode='markers', name='Swing Low', marker=dict(symbol='triangle-down', size=12, color='green')))
    fig.update_layout(title=f"{ticker} Price Pivots", height=450, legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.2), hovermode='x unified', xaxis=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikedash='dot', spikecolor='lightgrey'))
    return fig


def build_hl_figure(df, ticker):
    d = df[df.index >= df.index.max() - pd.DateOffset(years=1)]
    sh = identify_swing_points_high(d, 6, 4)
    sl = identify_swing_points_low(d, 6, 4)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['Close']], mode='lines', name='Close', line=dict(color='black')))
    fig.add_trace(go.Scatter(x=d.index[sh['Index']], y=[float(v) for v in sh['Value']], mode='markers', name='Swing High', marker=dict(symbol='triangle-up', size=12, color='red')))
    fig.add_trace(go.Scatter(x=d.index[sl['Index']], y=[float(v) for v in sl['Value']], mode='markers', name='Swing Low', marker=dict(symbol='triangle-down', size=12, color='green')))
    fig.update_layout(title=f"{ticker} High/Low Pivots", height=450, legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.2), hovermode='x unified', xaxis=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikedash='dot', spikecolor='lightgrey'))
    return fig


def build_ema_figure(df, ticker):
    d = df[df.index >= df.index.max() - pd.DateOffset(years=1)]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['Close']], mode='lines', name='Close', line=dict(color='black')))
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['EMA13']], mode='lines', name='EMA13', line=dict(dash='dot', width=1.25, color='blue')))
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['EMA55']], mode='lines', name='EMA55', line=dict(dash='dot', width=1.25, color='red')))
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['EMA144']], mode='lines', name='EMA144', line=dict(dash='dot', width=1.25, color='green')))
    fig.update_layout(title=f"{ticker} EMA Stack", height=450, legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.2), hovermode='x unified', xaxis=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikedash='dot', spikecolor='lightgrey'))
    return fig


def build_rsi_figure(df,ticker):
    d=df[df.index>=df.index.max()-pd.DateOffset(years=1)]
    fig=go.Figure()
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['RSI14']], mode='lines', name='RSI'))
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['RSI_EMA13']], mode='lines', name='RSI EMA-13', line=dict(dash='dot', width=1)))
    fig.update_layout(title=f'{ticker} RSI', height=450, hovermode='x unified', xaxis=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikedash='dot', spikecolor='lightgrey'))
    return fig


def build_adl_figure(df,ticker):
    d=df[df.index>=df.index.max()-pd.DateOffset(years=1)]
    fig=go.Figure()
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['ADL']], mode='lines', name='ADL'))
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['ADL_EMA']], mode='lines', name='ADL EMA', line=dict(dash='dot', width=1.25, color='orange')))
    fig.update_layout(title=f'{ticker} ADL', height=450, hovermode='x unified', xaxis=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikedash='dot', spikecolor='lightgrey'))
    return fig


def build_rs_figure(df, ticker):
    d = df[df.index >= df.index.max() - pd.DateOffset(years=1)]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=d.index, y=[float(v) for v in d['RS']], mode='lines', name='Relative Strength'))
    fig.update_layout(title=f"{ticker} Relative Strength vs Nifty", height=450, legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.2), yaxis=dict(tickformat='.2%'), hovermode='x unified', xaxis=dict(type='date', showspikes=True, spikemode='across', spikesnap='cursor', spikethickness=1, spikedash='dot', spikecolor='lightgrey'))
    return fig

# -------------------------------------------------------------------
# 3) Main Orchestration and Summary Functions
# -------------------------------------------------------------------

def evaluate_ticker_signal(ticker, in_position=False, price_pattern="", left_price=6, right_price=4, recency_candles=3):
    end = datetime.today()
    start = end - pd.DateOffset(years=5)
    sym = ticker.replace('.NS','')
    df = fetch_histogram(sym, 'NSE', start, end)
    if df.empty:
        return {"Ticker": ticker, "Signal": "NO DATA", "Data": df}
    idx_df = fetch_histogram('NIFTY', 'NSE', start, end)
    df, idx_df = align_data_indices(df, idx_df)
    if len(df) < left_price + right_price + 1:
        return {"Ticker": ticker, "Signal": "INSUFFICIENT DATA", "Data": df}
    
    # Indicators
    df['RS']     = calculate_relative_strength(df, idx_df)
    df['ATR14']  = talib.ATR(df.High, df.Low, df.Close, timeperiod=14)
    df['EMA13']  = talib.EMA(df.Close, timeperiod=13)
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
    
    rows = [
        ("Current Price", f"{current_close:.2f}"),
        ("Price-Action Trend (based on Close prices)", f"{pt_close} ({', '.join(tk_close)})"),
        ("Price-Action Trend (based on High/Low prices)", f"{pt_hl} ({', '.join(tk_hl)})"),
        ("Trend Strength (based on Fibonacci retracement)", fs),
        ("Market Structure (based on EMA Stack)", f"{mstr} ({estack})"),
        ("Market Sentiment (based on RSI)", f"{sentiment} (RSI: {rsi:.2f})"),
        ("Relative Strength vs Nifty", f"{'Positive' if rs_positive else 'Negative'}"),
        ("Accumulating or Distributing (based on Volume)", f"{'Accumulating' if adl_condition else 'Distributing'}")
    ]
    if sup: rows.append(("Support Zone", f"{sup[0]:.2f} – {sup[1]:.2f}"))
    if res: rows.append(("Resistance Zone", f"{res[0]:.2f} – {res[1]:.2f}"))
    return [{"key": k, "value": v} for k, v in rows]