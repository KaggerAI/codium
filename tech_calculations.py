# helpers.py

import time
from datetime import datetime
import pandas as pd
import numpy as np
import yfinance as yf
import talib
from tvDatafeed import TvDatafeed, Interval
import plotly.graph_objects as go
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
import matplotlib.pyplot as plt

# -------------------------------------------------------------------
# 1) Schema‐and‐RAG / ChatGPT‐integration helpers
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


def get_swing_tokens(sw):
    highs = sw[sw['Type'] == 'High']
    lows  = sw[sw['Type'] == 'Low']
    toks = []
    if len(highs) >= 2:
        toks.append('HH' if highs['Value'].iloc[-1] > highs['Value'].iloc[-2] else 'LH')
    if len(lows) >= 2:
        toks.append('HL' if lows['Value'].iloc[-1] > lows['Value'].iloc[-2] else 'LL')
    return toks


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


def compute_price_action_trend(sw, df):
    beh = analyze_swing_behaviour(sw)
    if beh['HH'] and beh['HL']:
        return 'Uptrend'
    if beh['LH'] and beh['LL']:
        return 'Downtrend'
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
        # support
        if ((typ == 'Low' and val < cp) or (typ == 'High' and val <= cp)) and gap < min_sup_gap:
            min_sup_gap = gap
            support = (df['Low'].iat[idx], df['High'].iat[idx])
        # resistance
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
