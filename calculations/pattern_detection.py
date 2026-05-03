"""
pattern_detection.py — Chart Pattern Recognition Engine

Identifies classic chart patterns using swing-point geometry
and trendline fitting. Optimized for batch screener execution.
Incorporates: multi-scale swing caching, ATR-based tolerances, 
early exits, and temporal filtering (forming vs recently completed).
"""
import pandas as pd
import numpy as np
from calculations.tech_calculations import (
    identify_swing_points_high,
    identify_swing_points_low
)

MIN_CONFIDENCE = 0.6

def _is_flat_line(prices, indices, atr_values):
    """Check if a set of prices form a flat horizontal line using ATR tolerance."""
    if len(prices) == 0: return False
    mean_price = np.mean(prices)
    for i, (price, idx) in enumerate(zip(prices, indices)):
        safe_idx = min(idx, len(atr_values) - 1)
        tolerance = atr_values[safe_idx] * 1.5
        if abs(price - mean_price) > tolerance:
            return False
    return True

def _volume_slope(volume_array):
    """Returns slope of volume trend via linear regression. Negative = volume drying up."""
    if len(volume_array) < 2: return 0.0
    vol = np.array(volume_array, dtype=float)
    if np.any(np.isnan(vol)):
        vol = vol[~np.isnan(vol)]
    if len(vol) < 2: return 0.0
    x = np.arange(len(vol))
    slope = np.polyfit(x, vol, 1)[0]
    return float(slope)

def _sanitize_nan(obj):
    """Recursively replace NaN/Inf with None for JSON-safe serialization."""
    if isinstance(obj, dict):
        return {k: _sanitize_nan(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_nan(v) for v in obj]
    elif isinstance(obj, float):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return obj
    elif isinstance(obj, (np.floating, np.integer)):
        val = float(obj)
        if np.isnan(val) or np.isinf(val):
            return None
        return val
    return obj

def _deduplicate_overlapping(patterns):
    """Keep only highest confidence pattern for overlapping regions."""
    if not patterns: return []
    patterns.sort(key=lambda x: x['confidence'], reverse=True)
    
    kept = []
    for p in patterns:
        start1, end1 = p.get('start_idx', 0), p.get('end_idx', 0)
        is_overlapping = False
        for k in kept:
            start2, end2 = k.get('start_idx', 0), k.get('end_idx', 0)
            overlap_start = max(start1, start2)
            overlap_end = min(end1, end2)
            if overlap_end > overlap_start:
                len1 = max(1, end1 - start1)
                len2 = max(1, end2 - start2)
                overlap_len = overlap_end - overlap_start
                if overlap_len / min(len1, len2) > 0.5:
                    is_overlapping = True
                    break
        if not is_overlapping:
            kept.append(p)
    return kept

def _get_date_str(dates, idx):
    """Safe extraction of date string for Plotly x-axis alignment."""
    if idx < 0 or idx >= len(dates):
        return ""
    d = dates[idx]
    if hasattr(d, 'strftime'):
        return d.strftime('%Y-%m-%d')
    return str(d).split()[0]

def detect_cup_and_handle(ctx):
    close, atr, dates, volume = ctx['close'], ctx['atr'], ctx['dates'], ctx['volume']
    macro_highs, macro_lows = ctx['macro_highs'], ctx['macro_lows']
    
    # Pre-Filter: Must be within 15% of recent high
    if len(close) == 0 or close[-1] < np.max(close) * 0.85:
        return None
        
    if len(macro_highs) < 2 or len(macro_lows) < 1:
        return None
        
    best_pattern = None
    best_conf = 0
    
    for i in range(len(macro_highs) - 1):
        left_idx = int(macro_highs.iloc[i]['Index'])
        left_val = float(macro_highs.iloc[i]['Value'])
        
        for j in range(i + 1, len(macro_highs)):
            right_idx = int(macro_highs.iloc[j]['Index'])
            right_val = float(macro_highs.iloc[j]['Value'])
            
            # Lips must be roughly aligned (within 2x ATR)
            safe_idx = min(left_idx, len(atr) - 1)
            if abs(left_val - right_val) > atr[safe_idx] * 2:
                continue
                
            # Find cup bottom
            between_lows = macro_lows[(macro_lows['Index'] > left_idx) & (macro_lows['Index'] < right_idx)]
            if len(between_lows) == 0:
                continue
                
            bottom_row = between_lows.loc[between_lows['Value'].idxmin()]
            bottom_idx = int(bottom_row['Index'])
            bottom_val = float(bottom_row['Value'])
            
            # Cup depth check (at least 10% deep)
            if bottom_val > left_val * 0.90:
                continue
                
            # Cup width check (30 to 150 bars)
            if right_idx - left_idx < 30 or right_idx - left_idx > 150:
                continue
                
            # Roundness check using polynomial R-squared proxy
            cup_prices = close[left_idx:right_idx+1]
            if len(cup_prices) < 3: continue
            x = np.arange(len(cup_prices))
            coeffs = np.polyfit(x, cup_prices, 2)
            if coeffs[0] <= 0: # Concave down (V-shape inverted)
                continue
                
            # Detect Handle
            handle_prices = close[right_idx:]
            if len(handle_prices) < 5:
                continue
                
            handle_low = np.min(handle_prices)
            handle_low_idx = right_idx + int(np.argmin(handle_prices))
            
            # Handle depth (5-15% pullback from right lip)
            pullback = (right_val - handle_low) / right_val
            if pullback < 0.02 or pullback > 0.20:
                continue
                
            # Volume confirmation
            handle_vol = volume[right_idx:handle_low_idx+1]
            v_slope = _volume_slope(handle_vol) if len(handle_vol) > 2 else 0
            
            # Base Confidence
            conf = 0.6
            if v_slope < 0: conf += 0.15 # Volume drying up
            if abs(left_val - right_val) < atr[safe_idx]: conf += 0.15 # Tighter lip alignment
            
            # Temporal Status Check
            post_handle = close[handle_low_idx:]
            breakout_idx = None
            for k, p in enumerate(post_handle):
                if p > right_val:
                    breakout_idx = handle_low_idx + k
                    break
                    
            status = 'forming'
            bars_since = 0
            if breakout_idx is not None:
                status = 'completed'
                bars_since = len(close) - 1 - breakout_idx
                if bars_since > 10:
                    continue # Skip museum patterns
                    
            if conf > best_conf:
                best_conf = conf
                
                d_left = _get_date_str(dates, left_idx)
                d_bottom = _get_date_str(dates, bottom_idx)
                d_right = _get_date_str(dates, right_idx)
                
                best_pattern = {
                    'pattern': 'Cup & Handle',
                    'signal': 'Bullish',
                    'status': status,
                    'bars_since_completion': bars_since,
                    'confidence': conf,
                    'start_idx': left_idx,
                    'end_idx': len(close) - 1,
                    'key_points': [
                        {"idx": left_idx, "price": left_val, "label": "Left Lip"},
                        {"idx": bottom_idx, "price": bottom_val, "label": "Bottom"},
                        {"idx": right_idx, "price": right_val, "label": "Right Lip"},
                        {"idx": handle_low_idx, "price": handle_low, "label": "Handle Low"}
                    ],
                    'shapes': [
                        {
                            "type": "line",
                            "x0": d_left, "y0": left_val,
                            "x1": _get_date_str(dates, len(close)-1), "y1": left_val,
                            "line": {"color": "rgba(59,130,246,0.8)", "width": 2, "dash": "dash"}
                        }
                    ],
                    'annotations': [
                        {
                            "x": d_right, "y": right_val + (atr[-1] if atr[-1] else 10),
                            "text": f"📐 {'Confirmed' if status=='completed' else 'Forming'}: Cup & Handle",
                            "showarrow": False,
                            "font": {"color": "#fff", "size": 10},
                            "bgcolor": "rgba(59,130,246,0.85)",
                            "borderpad": 4
                        }
                    ],
                    'description': f"Cup & Handle {'completed' if status=='completed' else 'forming'} with resistance near {right_val:.2f}"
                }
                
    return best_pattern

def detect_ascending_triangle(ctx):
    close, atr, dates = ctx['close'], ctx['atr'], ctx['dates']
    macro_highs, macro_lows = ctx['macro_highs'], ctx['macro_lows']
    
    if len(close) < 20: return None
    
    # Pre-filter: price must not be crashing
    sma20 = pd.Series(close).rolling(20).mean().values
    if np.isnan(sma20[-1]) or close[-1] < sma20[-1]:
        return None
        
    best_pattern = None
    best_conf = 0
    
    for i in range(len(macro_highs) - 1):
        idx1 = int(macro_highs.iloc[i]['Index'])
        val1 = float(macro_highs.iloc[i]['Value'])
        
        for j in range(i + 1, len(macro_highs)):
            idx2 = int(macro_highs.iloc[j]['Index'])
            val2 = float(macro_highs.iloc[j]['Value'])
            
            # Flat resistance zone
            safe_idx = min(idx1, len(atr) - 1)
            if abs(val1 - val2) > atr[safe_idx] * 1.5:
                continue
                
            res_level = (val1 + val2) / 2
            
            # Ascending support zone
            relevant_lows = macro_lows[macro_lows['Index'] >= idx1]
            if len(relevant_lows) < 2:
                continue
                
            low_indices = relevant_lows['Index'].values.astype(int)
            low_values = relevant_lows['Value'].values.astype(float)
            
            if len(low_indices) < 2: continue
            
            slope, intercept = np.polyfit(low_indices, low_values, 1)
            if slope <= 0: # Support must be rising
                continue
                
            # Convergence check
            support_at_res = slope * idx2 + intercept
            if support_at_res >= res_level:
                continue
                
            # Duration check
            duration = idx2 - idx1
            if duration < 15 or duration > 120:
                continue
                
            conf = 0.7
            
            # Breakout check
            breakout_idx = None
            for k in range(idx2, len(close)):
                if close[k] > res_level + atr[k]*0.2:
                    breakout_idx = k
                    break
                    
            status = 'forming'
            bars_since = 0
            if breakout_idx is not None:
                status = 'completed'
                bars_since = len(close) - 1 - breakout_idx
                if bars_since > 10:
                    continue # Skip old completed patterns
                    
            if conf > best_conf:
                best_conf = conf
                
                d_start = _get_date_str(dates, idx1)
                d_end = _get_date_str(dates, len(close)-1)
                
                sup_start_y = slope * idx1 + intercept
                sup_end_y = slope * (len(close)-1) + intercept
                
                best_pattern = {
                    'pattern': 'Ascending Triangle',
                    'signal': 'Bullish',
                    'status': status,
                    'bars_since_completion': bars_since,
                    'confidence': conf,
                    'start_idx': idx1,
                    'end_idx': len(close) - 1,
                    'key_points': [
                        {"idx": idx2, "price": res_level, "label": "Resistance"}
                    ],
                    'shapes': [
                        {
                            "type": "line",
                            "x0": d_start, "y0": res_level,
                            "x1": d_end, "y1": res_level,
                            "line": {"color": "rgba(239,68,68,0.8)", "width": 2, "dash": "dash"}
                        },
                        {
                            "type": "line",
                            "x0": d_start, "y0": sup_start_y,
                            "x1": d_end, "y1": sup_end_y,
                            "line": {"color": "rgba(34,197,94,0.8)", "width": 2, "dash": "dash"}
                        }
                    ],
                    'annotations': [
                        {
                            "x": d_end, "y": res_level + (atr[-1] if atr[-1] else 10),
                            "text": f"📐 {'Confirmed' if status=='completed' else 'Forming'}: Ascending Triangle",
                            "showarrow": False,
                            "font": {"color": "#fff", "size": 10},
                            "bgcolor": "rgba(168,85,247,0.85)",
                            "borderpad": 4
                        }
                    ],
                    'description': f"Ascending triangle {'completed' if status=='completed' else 'forming'} with flat resistance at {res_level:.2f}"
                }
                
    return best_pattern

def detect_descending_triangle(ctx):
    close, atr, dates = ctx['close'], ctx['atr'], ctx['dates']
    macro_highs, macro_lows = ctx['macro_highs'], ctx['macro_lows']
    
    if len(close) < 20: return None
    
    best_pattern = None
    best_conf = 0
    
    for i in range(len(macro_lows) - 1):
        idx1 = int(macro_lows.iloc[i]['Index'])
        val1 = float(macro_lows.iloc[i]['Value'])
        
        for j in range(i + 1, len(macro_lows)):
            idx2 = int(macro_lows.iloc[j]['Index'])
            val2 = float(macro_lows.iloc[j]['Value'])
            
            safe_idx = min(idx1, len(atr) - 1)
            if abs(val1 - val2) > atr[safe_idx] * 1.5:
                continue
                
            sup_level = (val1 + val2) / 2
            
            relevant_highs = macro_highs[macro_highs['Index'] >= idx1]
            if len(relevant_highs) < 2:
                continue
                
            high_indices = relevant_highs['Index'].values.astype(int)
            high_values = relevant_highs['Value'].values.astype(float)
            
            slope, intercept = np.polyfit(high_indices, high_values, 1)
            if slope >= 0: # Resistance must be falling
                continue
                
            res_at_sup = slope * idx2 + intercept
            if res_at_sup <= sup_level:
                continue
                
            duration = idx2 - idx1
            if duration < 15 or duration > 120:
                continue
                
            conf = 0.7
            
            breakout_idx = None
            for k in range(idx2, len(close)):
                if close[k] < sup_level - atr[k]*0.2:
                    breakout_idx = k
                    break
                    
            status = 'forming'
            bars_since = 0
            if breakout_idx is not None:
                status = 'completed'
                bars_since = len(close) - 1 - breakout_idx
                if bars_since > 10: continue
                
            if conf > best_conf:
                best_conf = conf
                d_start = _get_date_str(dates, idx1)
                d_end = _get_date_str(dates, len(close)-1)
                
                res_start_y = slope * idx1 + intercept
                res_end_y = slope * (len(close)-1) + intercept
                
                best_pattern = {
                    'pattern': 'Descending Triangle',
                    'signal': 'Bearish',
                    'status': status,
                    'bars_since_completion': bars_since,
                    'confidence': conf,
                    'start_idx': idx1,
                    'end_idx': len(close) - 1,
                    'key_points': [{"idx": idx2, "price": sup_level, "label": "Support"}],
                    'shapes': [
                        {"type": "line", "x0": d_start, "y0": sup_level, "x1": d_end, "y1": sup_level, "line": {"color": "rgba(34,197,94,0.8)", "width": 2, "dash": "dash"}},
                        {"type": "line", "x0": d_start, "y0": res_start_y, "x1": d_end, "y1": res_end_y, "line": {"color": "rgba(239,68,68,0.8)", "width": 2, "dash": "dash"}}
                    ],
                    'annotations': [{"x": d_end, "y": sup_level - (atr[-1] if atr[-1] else 10), "text": f"📐 {'Confirmed' if status=='completed' else 'Forming'}: Descending Triangle", "showarrow": False, "font": {"color": "#fff", "size": 10}, "bgcolor": "rgba(239,68,68,0.85)", "borderpad": 4}],
                    'description': f"Descending triangle {'completed' if status=='completed' else 'forming'} with flat support at {sup_level:.2f}"
                }
    return best_pattern

def detect_symmetrical_triangle(ctx):
    close, atr, dates = ctx['close'], ctx['atr'], ctx['dates']
    macro_highs, macro_lows = ctx['macro_highs'], ctx['macro_lows']
    
    if len(close) < 20: return None
    
    best_pattern = None
    best_conf = 0
    
    for i in range(len(macro_highs) - 1):
        h_idx1 = int(macro_highs.iloc[i]['Index'])
        h_relevant = macro_highs[macro_highs['Index'] >= h_idx1].head(4)
        if len(h_relevant) < 2: continue
        
        h_idx = h_relevant['Index'].values.astype(int)
        h_val = h_relevant['Value'].values.astype(float)
        h_slope, h_int = np.polyfit(h_idx, h_val, 1)
        if h_slope >= 0: continue 
        
        l_relevant = macro_lows[(macro_lows['Index'] >= h_idx1) & (macro_lows['Index'] <= h_idx[-1] + 20)]
        if len(l_relevant) < 2: continue
        
        l_idx = l_relevant['Index'].values.astype(int)
        l_val = l_relevant['Value'].values.astype(float)
        l_slope, l_int = np.polyfit(l_idx, l_val, 1)
        if l_slope <= 0: continue 
        
        end_idx = max(h_idx[-1], l_idx[-1])
        h_at_end = h_slope * end_idx + h_int
        l_at_end = l_slope * end_idx + l_int
        if l_at_end >= h_at_end: continue 
        
        duration = end_idx - h_idx1
        if duration < 15 or duration > 120: continue
        
        conf = 0.65
        
        breakout_idx = None
        breakout_dir = None
        for k in range(end_idx, len(close)):
            h_line = h_slope * k + h_int
            l_line = l_slope * k + l_int
            if close[k] > h_line + atr[k]*0.2:
                breakout_idx = k
                breakout_dir = 'Bullish'
                break
            elif close[k] < l_line - atr[k]*0.2:
                breakout_idx = k
                breakout_dir = 'Bearish'
                break
                
        status = 'forming'
        bars_since = 0
        if breakout_idx is not None:
            status = 'completed'
            bars_since = len(close) - 1 - breakout_idx
            if bars_since > 10: continue
            
        if conf > best_conf:
            best_conf = conf
            d_start = _get_date_str(dates, h_idx1)
            d_end = _get_date_str(dates, len(close)-1)
            
            res_start_y = h_slope * h_idx1 + h_int
            res_end_y = h_slope * (len(close)-1) + h_int
            sup_start_y = l_slope * h_idx1 + l_int
            sup_end_y = l_slope * (len(close)-1) + l_int
            
            sig = breakout_dir if breakout_dir else 'Neutral'
            
            best_pattern = {
                'pattern': 'Symmetrical Triangle',
                'signal': sig,
                'status': status,
                'bars_since_completion': bars_since,
                'confidence': conf,
                'start_idx': h_idx1,
                'end_idx': len(close) - 1,
                'key_points': [],
                'shapes': [
                    {"type": "line", "x0": d_start, "y0": res_start_y, "x1": d_end, "y1": res_end_y, "line": {"color": "rgba(239,68,68,0.8)", "width": 2, "dash": "dash"}},
                    {"type": "line", "x0": d_start, "y0": sup_start_y, "x1": d_end, "y1": sup_end_y, "line": {"color": "rgba(34,197,94,0.8)", "width": 2, "dash": "dash"}}
                ],
                'annotations': [{"x": d_end, "y": res_end_y, "text": f"📐 {'Confirmed' if status=='completed' else 'Forming'}: Symmetrical Triangle", "showarrow": False, "font": {"color": "#fff", "size": 10}, "bgcolor": "rgba(245,158,11,0.85)", "borderpad": 4}],
                'description': f"Symmetrical triangle {'completed' if status=='completed' else 'forming'}"
            }
    return best_pattern

def detect_rectangle(ctx):
    close, atr, dates = ctx['close'], ctx['atr'], ctx['dates']
    macro_highs, macro_lows = ctx['macro_highs'], ctx['macro_lows']
    
    if len(close) < 30: return None
    if (np.max(close) - np.min(close)) / np.mean(close) > 0.30: return None
    
    best_pattern = None
    best_conf = 0
    
    for i in range(len(macro_highs) - 1):
        idx1 = int(macro_highs.iloc[i]['Index'])
        val1 = float(macro_highs.iloc[i]['Value'])
        
        for j in range(i + 1, len(macro_highs)):
            idx2 = int(macro_highs.iloc[j]['Index'])
            val2 = float(macro_highs.iloc[j]['Value'])
            
            safe_idx = min(idx1, len(atr) - 1)
            if abs(val1 - val2) > atr[safe_idx] * 1.5: continue
            
            res_level = (val1 + val2) / 2
            
            relevant_lows = macro_lows[(macro_lows['Index'] >= idx1 - 10) & (macro_lows['Index'] <= idx2 + 10)]
            if len(relevant_lows) < 2: continue
            
            l_val1 = float(relevant_lows.iloc[0]['Value'])
            l_val2 = float(relevant_lows.iloc[1]['Value'])
            
            if abs(l_val1 - l_val2) > atr[safe_idx] * 1.5: continue
            
            sup_level = (l_val1 + l_val2) / 2
            height = res_level - sup_level
            if height < atr[safe_idx] * 1.5: continue 
            
            duration = idx2 - idx1
            if duration < 20 or duration > 150: continue
            
            conf = 0.65
            
            breakout_idx = None
            breakout_dir = None
            for k in range(idx2, len(close)):
                if close[k] > res_level + atr[k]*0.2:
                    breakout_idx = k
                    breakout_dir = 'Bullish'
                    break
                elif close[k] < sup_level - atr[k]*0.2:
                    breakout_idx = k
                    breakout_dir = 'Bearish'
                    break
                    
            status = 'forming'
            bars_since = 0
            if breakout_idx is not None:
                status = 'completed'
                bars_since = len(close) - 1 - breakout_idx
                if bars_since > 10: continue
                
            if conf > best_conf:
                best_conf = conf
                d_start = _get_date_str(dates, idx1)
                d_end = _get_date_str(dates, len(close)-1)
                
                sig = breakout_dir if breakout_dir else 'Neutral'
                
                best_pattern = {
                    'pattern': 'Rectangle',
                    'signal': sig,
                    'status': status,
                    'bars_since_completion': bars_since,
                    'confidence': conf,
                    'start_idx': idx1,
                    'end_idx': len(close) - 1,
                    'key_points': [
                        {"idx": idx2, "price": res_level, "label": "Resistance"},
                        {"idx": idx2, "price": sup_level, "label": "Support"}
                    ],
                    'shapes': [
                        {"type": "rect", "x0": d_start, "y0": sup_level, "x1": d_end, "y1": res_level, "fillcolor": "rgba(168,85,247,0.08)", "line": {"width": 1, "color": "rgba(168,85,247,0.5)", "dash": "dash"}}
                    ],
                    'annotations': [{"x": d_end, "y": res_level, "text": f"📐 {'Confirmed' if status=='completed' else 'Forming'}: Rectangle", "showarrow": False, "font": {"color": "#fff", "size": 10}, "bgcolor": "rgba(168,85,247,0.85)", "borderpad": 4}],
                    'description': f"Rectangle {'completed' if status=='completed' else 'forming'} between {sup_level:.2f} and {res_level:.2f}"
                }
    return best_pattern

def detect_double_bottom(ctx):
    close, atr, dates, volume = ctx['close'], ctx['atr'], ctx['dates'], ctx['volume']
    macro_highs, macro_lows = ctx['macro_highs'], ctx['macro_lows']
    
    if len(close) < 20: return None
    if close[-1] > np.percentile(close, 70): return None
    
    best_pattern = None
    best_conf = 0
    
    for i in range(len(macro_lows) - 1):
        idx1 = int(macro_lows.iloc[i]['Index'])
        val1 = float(macro_lows.iloc[i]['Value'])
        
        for j in range(i + 1, len(macro_lows)):
            idx2 = int(macro_lows.iloc[j]['Index'])
            val2 = float(macro_lows.iloc[j]['Value'])
            
            safe_idx = min(idx1, len(atr) - 1)
            if abs(val1 - val2) > atr[safe_idx] * 1.5: continue
            if idx2 - idx1 < 15 or idx2 - idx1 > 90: continue
            
            between_highs = macro_highs[(macro_highs['Index'] > idx1) & (macro_highs['Index'] < idx2)]
            if len(between_highs) == 0: continue
            
            neck_row = between_highs.loc[between_highs['Value'].idxmax()]
            neck_idx = int(neck_row['Index'])
            neck_val = float(neck_row['Value'])
            
            depth = neck_val - ((val1 + val2)/2)
            if depth < atr[safe_idx] * 2: continue
            
            conf = 0.7
            
            breakout_idx = None
            for k in range(idx2, len(close)):
                if close[k] > neck_val:
                    breakout_idx = k
                    break
                    
            status = 'forming'
            bars_since = 0
            if breakout_idx is not None:
                status = 'completed'
                bars_since = len(close) - 1 - breakout_idx
                if bars_since > 10: continue
                
            if conf > best_conf:
                best_conf = conf
                d_start = _get_date_str(dates, idx1)
                d_neck = _get_date_str(dates, neck_idx)
                d_end = _get_date_str(dates, idx2)
                d_current = _get_date_str(dates, len(close)-1)
                
                best_pattern = {
                    'pattern': 'Double Bottom',
                    'signal': 'Bullish',
                    'status': status,
                    'bars_since_completion': bars_since,
                    'confidence': conf,
                    'start_idx': idx1,
                    'end_idx': len(close) - 1,
                    'key_points': [
                        {"idx": idx1, "price": val1, "label": "First Bottom"},
                        {"idx": neck_idx, "price": neck_val, "label": "Neckline"},
                        {"idx": idx2, "price": val2, "label": "Second Bottom"}
                    ],
                    'shapes': [
                        {"type": "line", "x0": d_start, "y0": val1, "x1": d_neck, "y1": neck_val, "line": {"color": "rgba(59,130,246,0.8)", "width": 2}},
                        {"type": "line", "x0": d_neck, "y0": neck_val, "x1": d_end, "y1": val2, "line": {"color": "rgba(59,130,246,0.8)", "width": 2}},
                        {"type": "line", "x0": d_start, "y0": neck_val, "x1": d_current, "y1": neck_val, "line": {"color": "rgba(239,68,68,0.5)", "width": 2, "dash": "dash"}}
                    ],
                    'annotations': [{"x": d_current, "y": neck_val, "text": f"📐 {'Confirmed' if status=='completed' else 'Forming'}: Double Bottom", "showarrow": False, "font": {"color": "#fff", "size": 10}, "bgcolor": "rgba(59,130,246,0.85)", "borderpad": 4}],
                    'description': f"Double Bottom {'completed' if status=='completed' else 'forming'} with neckline at {neck_val:.2f}"
                }
    return best_pattern

def detect_bull_flag(ctx):
    close, atr, dates, volume = ctx['close'], ctx['atr'], ctx['dates'], ctx['volume']
    micro_highs, micro_lows = ctx['micro_highs'], ctx['micro_lows']
    
    if len(close) < 20: return None
    
    pct_changes = [(close[i] - close[i-15])/close[i-15] for i in range(15, len(close))]
    if len(pct_changes) == 0 or max(pct_changes) < 0.12: return None 
    
    best_pattern = None
    best_conf = 0
    
    for i in range(max(0, len(micro_highs) - 5), len(micro_highs)):
        idx_top = int(micro_highs.iloc[i]['Index'])
        val_top = float(micro_highs.iloc[i]['Value'])
        
        idx_base = max(0, idx_top - 20)
        pole_base_val = np.min(close[idx_base:idx_top]) if idx_top > idx_base else val_top
        if pole_base_val == 0 or (val_top - pole_base_val) / pole_base_val < 0.12:
            continue
            
        idx_end = len(close) - 1
        flag_duration = idx_end - idx_top
        if flag_duration < 3 or flag_duration > 25:
            continue
            
        flag_prices = close[idx_top:]
        flag_low = np.min(flag_prices)
        
        pole_height = val_top - pole_base_val
        pullback = val_top - flag_low
        if pullback > pole_height * 0.5:
            continue
            
        flag_vol = volume[idx_top:]
        v_slope = _volume_slope(flag_vol) if len(flag_vol) > 2 else 0
        
        conf = 0.65
        if v_slope < 0: conf += 0.1
        
        breakout_idx = None
        for k in range(idx_top + 1, len(close)):
            if close[k] > val_top:
                breakout_idx = k
                break
                
        status = 'forming'
        bars_since = 0
        if breakout_idx is not None:
            status = 'completed'
            bars_since = len(close) - 1 - breakout_idx
            if bars_since > 5: continue
            
        if conf > best_conf:
            best_conf = conf
            d_base = _get_date_str(dates, idx_base)
            d_top = _get_date_str(dates, idx_top)
            d_end = _get_date_str(dates, len(close)-1)
            
            best_pattern = {
                'pattern': 'Bull Flag',
                'signal': 'Bullish',
                'status': status,
                'bars_since_completion': bars_since,
                'confidence': conf,
                'start_idx': idx_base,
                'end_idx': len(close) - 1,
                'key_points': [
                    {"idx": idx_top, "price": val_top, "label": "Flag Top"}
                ],
                'shapes': [
                    {"type": "line", "x0": d_base, "y0": pole_base_val, "x1": d_top, "y1": val_top, "line": {"color": "rgba(34,197,94,0.8)", "width": 3}},
                    {"type": "rect", "x0": d_top, "y0": flag_low, "x1": d_end, "y1": val_top, "fillcolor": "rgba(59,130,246,0.1)", "line": {"width": 1, "color": "rgba(59,130,246,0.5)", "dash": "dot"}}
                ],
                'annotations': [{"x": d_end, "y": val_top, "text": f"📐 {'Confirmed' if status=='completed' else 'Forming'}: Bull Flag", "showarrow": False, "font": {"color": "#fff", "size": 10}, "bgcolor": "rgba(16,185,129,0.85)", "borderpad": 4}],
                'description': f"Bull Flag {'completed' if status=='completed' else 'forming'} with pole top at {val_top:.2f}"
            }
            
    return best_pattern

def detect_all_patterns(df, ticker):
    """Master dispatcher with multi-scale swing caching + early exits."""
    if df is None or len(df) < 60:
        return []

    # 1 year slice — drop NaN rows to prevent numpy NaN propagation
    df_1y = df.tail(252).copy()
    df_1y = df_1y.dropna(subset=['Close', 'High', 'Low', 'Volume'])
    if len(df_1y) < 60:
        return []
    df_reset = df_1y.reset_index(drop=True)
    
    close = df_reset['Close'].values.astype(float)
    high = df_reset['High'].values.astype(float)
    low = df_reset['Low'].values.astype(float)
    volume = df_reset['Volume'].values.astype(float)

    # Pre-compute ATR(14)
    tr = np.maximum(high - low,
         np.maximum(np.abs(high - np.roll(close, 1)),
                    np.abs(low - np.roll(close, 1))))
    tr[0] = high[0] - low[0]
    atr_14 = pd.Series(tr).rolling(14).mean().bfill().values

    # Multi-scale swing caching
    macro_highs = identify_swing_points_high(df_reset, 20, 10)
    macro_lows  = identify_swing_points_low(df_reset, 20, 10)
    micro_highs = identify_swing_points_high(df_reset, 5, 3)
    micro_lows  = identify_swing_points_low(df_reset, 5, 3)

    ctx = {
        'df': df_reset, 'close': close, 'high': high, 'low': low,
        'volume': volume, 'atr': atr_14,
        'macro_highs': macro_highs, 'macro_lows': macro_lows,
        'micro_highs': micro_highs, 'micro_lows': micro_lows,
        'dates': df_1y.index.tolist()  # datetime index for Plotly alignment
    }

    detectors = [
        detect_cup_and_handle,
        detect_ascending_triangle,
        detect_descending_triangle,
        detect_symmetrical_triangle,
        detect_rectangle,
        detect_double_bottom,
        detect_bull_flag
    ]

    patterns = []
    for detector in detectors:
        try:
            result = detector(ctx)
            if result and result['confidence'] >= MIN_CONFIDENCE:
                # Temporal filter
                if result.get('status') == 'completed' and result.get('bars_since_completion', 0) > 10:
                    continue
                patterns.append(result)
        except Exception as e:
            print(f"WARN: detector {detector.__name__} failed for {ticker}: {e}")
            continue

    # Deduplicate overlapping patterns
    patterns = _deduplicate_overlapping(patterns)
    patterns.sort(key=lambda p: p['confidence'], reverse=True)
    
    # Sanitize all numeric values to prevent NaN/Inf in JSON serialization
    return [_sanitize_nan(p) for p in patterns[:3]]
