#!/usr/bin/env python3

"""
handler.py — Technical analysis web-server

Before running, ensure you have installed system TA-Lib and Python packages:

1. System-level TA-Lib (Linux):
   sudo bash -c "curl -L https://anaconda.org/conda-forge/libta-lib/0.4.0/download/linux-64/libta-lib-0.4.0-h166bdaf_1.tar.bz2 \
     | tar xj -C /usr/lib/x86_64-linux-gnu/ lib --strip-components=1"

2. Python packages:
   pip install flask flask-cors tradingview-datafeed yfinance pandas numpy matplotlib openai yfinance pdfplumber plotly matplotlib scikit-learn feedparser openai google-generativeai python-dotenv
"""
import sys, os, socket, re

# Load environment variables from .env file for local development
try:
    from dotenv import load_dotenv
    load_dotenv()  # This will load variables from .env file if it exists
    print("INFO: Loaded environment variables from .env file (if present)")
except ImportError:
    print("INFO: python-dotenv not installed. Using system environment variables only.")

sys.path.append(os.path.dirname(__file__))
from prompts import (
    get_central_brain_prompt, 
    get_planning_system_prompt, 
    get_answering_system_prompt,
    get_unanswerable_expansion_prompt
)

import traceback
import time
import io
import base64
from datetime import datetime
import asyncio
import httpx
import uuid
import zlib
import pickle
import threading

from flask import send_from_directory

from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_caching import Cache
from flask_compress import Compress

import redis
import urllib.parse

# from a2wsgi import ASGIMiddleware

from flask_cors import CORS
import pandas as pd
import numpy as np
import talib
import yfinance as yf
from tvDatafeed import TvDatafeed, Interval
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas
import matplotlib.pyplot as plt
import json
import re

# Import the calculation registry from calculations.py
from fund_calculations import CALCULATION_REGISTRY, _get_metric_from_table # _get_metric_from_table might be useful here too

import requests
from bs4 import BeautifulSoup
import pdfplumber
from io import BytesIO

from progress_logger import progress_queue, log_progress, log_final_message

from scores.AIScores import AIScores
from analyst_reports.trendlyne_fetcher import fetch_analyst_reports_async
# Use cookie-based authentication (easier than programmatic login)
from analyst_reports.pdf_summarizer_cookies import summarize_analyst_pdf_async, download_analyst_pdf_with_cookies

from tech_calculations import (
    evaluate_ticker_signal,
    generate_summary,
    generate_per_chart_summaries,
    build_close_figure,
    build_hl_figure,
    build_ema_figure,
    build_rsi_figure,
    build_adl_figure,
    build_rs_figure,
    build_rsi_divergence_figure
)

# --- Parallel API Integration ---
PARALLEL_API_KEY = os.getenv("PARALLEL_API_KEY")
parallel_client = None
if PARALLEL_API_KEY:
    try:
        from parallel import Parallel
        parallel_client = Parallel(api_key=PARALLEL_API_KEY)
        print("INFO: Parallel API client initialized.")
    except ImportError:
        print("WARNING: parallel-sdk not installed. Deep research feature will be disabled.")
else:
    print("WARNING: PARALLEL_API_KEY environment variable is not set. Deep research feature will be disabled.")

# last_analysis: dict = {}
last_analysis = None

def is_na(val):
    """Helper for N/A checks across different metric sources."""
    if val is None: return True
    v = str(val).strip().lower()
    return v in ('', 'n/a', 'nan')

def sanitize_for_json(obj):
    """Recursively replace NaN and Infinity float values with None for valid JSON serialization."""
    import math
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [sanitize_for_json(item) for item in obj]
    elif isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    elif isinstance(obj, (np.floating,)):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return float(obj)
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, np.ndarray):
        return sanitize_for_json(obj.tolist())
    elif isinstance(obj, pd.DataFrame):
        return sanitize_for_json(obj.to_dict(orient='records'))
    elif isinstance(obj, pd.Series):
        return sanitize_for_json(obj.to_dict())
    return obj

# Initialize Flask app and enable CORS
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)

# Initialize authentication module
from auth.routes import init_auth
init_auth(app)
app.config["COMPRESS_MIMETYPES"] = [
    'text/html', 
    'text/css', 
    'text/xml', 
    'application/json', 
    'application/javascript'
]
app.config["COMPRESS_LEVEL"] = 6  # Balance between speed and CPU usage
app.config["COMPRESS_MIN_SIZE"] = 500 # Don't bother compressing tiny responses
Compress(app)

# =====================================================================
# FLASK-SOCKETIO FOR REAL-TIME BUDGET TRANSCRIPTION
# =====================================================================
from flask_socketio import SocketIO, emit
socketio = SocketIO(
    app, 
    cors_allowed_origins="*", 
    async_mode='threading',  # Changed from eventlet to threading for compatibility with httpx/asyncio
    ping_timeout=60,       # Increase to 60s to handle long Gemini processing
    ping_interval=25,      # Ping every 25s
    max_http_buffer_size=10000000 # 10MB to accommodate large audio chunks
)

# =====================================================================
# INDUSTRY RESEARCH JOB STORAGE (Redis-backed for multi-instance support)
# =====================================================================

INDUSTRY_JOB_PREFIX = "industry_job:"
INDUSTRY_JOB_TTL = 7200  # 2 hours TTL for job status (increased for long-running jobs)

# =====================================================================
# LOCAL IN-MEMORY CACHE (Fallback when Redis is unavailable)
# =====================================================================
# This stores analysis data in Python memory as ultimate fallback
# Structure: { ticker: { "data": {...}, "timestamp": time.time() } }
LOCAL_ANALYSIS_CACHE = {}
LOCAL_INDUSTRY_JOBS = {}  # For industry research jobs: { job_id: { "data": {...}, "timestamp": time.time() } }
LOCAL_CACHE_TTL = 21600  # 6 hours in seconds
LOCAL_INDUSTRY_JOB_TTL = 7200  # 2 hours in seconds for industry research jobs

# Forward declaration - actual client initialized after Flask app setup
DIRECT_REDIS_CLIENT = None

def get_local_cache(ticker):
    """Get analysis data from local memory cache."""
    ticker_upper = ticker.upper() if ticker else None
    if not ticker_upper or ticker_upper not in LOCAL_ANALYSIS_CACHE:
        return None
    
    entry = LOCAL_ANALYSIS_CACHE[ticker_upper]
    age = time.time() - entry.get("timestamp", 0)
    
    if age > LOCAL_CACHE_TTL:
        # Expired, remove it
        del LOCAL_ANALYSIS_CACHE[ticker_upper]
        print(f"DEBUG: Local cache expired for {ticker_upper} (age: {age:.0f}s)", file=sys.stderr)
        return None
    
    print(f"DEBUG: Local cache HIT for {ticker_upper} (age: {age:.0f}s)", file=sys.stderr)
    return entry.get("data")

def set_local_cache(ticker, data):
    """Save analysis data to local memory cache."""
    if not ticker or not data:
        return
    ticker_upper = ticker.upper()
    LOCAL_ANALYSIS_CACHE[ticker_upper] = {
        "data": data,
        "timestamp": time.time()
    }
    print(f"DEBUG: Saved to local cache: {ticker_upper}", file=sys.stderr)

def get_any_cache(ticker):
    """
    Get analysis data from cache, checking both local memory and Redis.
    This is used by Forensic Agent to ensure data access in distributed environments.
    """
    if not ticker:
        return None
    
    ticker_upper = ticker.upper().strip()
    
    # 1. Try Local Cache first
    local_data = get_local_cache(ticker_upper)
    if local_data:
        # print(f"CACHE_DEBUG: Local memory HIT for {ticker_upper}", file=sys.stderr)
        return local_data
    
    # 2. Try Redis Cache (if available)
    if DIRECT_REDIS_CLIENT:
        try:
            # Custom stock analysis key format
            stock_cache_key = f"stock_analysis_{ticker_upper}"
            raw_data = DIRECT_REDIS_CLIENT.get(stock_cache_key)
            
            if not raw_data:
                # Also try the standard Flask-Caching key format just in case
                flask_key = f"flask_cache_stock_analysis_{ticker_upper}"
                raw_data = DIRECT_REDIS_CLIENT.get(flask_key)
            
            if raw_data:
                # Decompress and deserialize
                try:
                    data = pickle.loads(zlib.decompress(raw_data))
                    # print(f"CACHE_DEBUG: Redis HIT for {ticker_upper}", file=sys.stderr)
                    
                    # Optional: back-fill local cache for faster subsequent access
                    set_local_cache(ticker_upper, data)
                    
                    return data
                except (zlib.error, pickle.UnpicklingError) as e:
                    # Try plain pickle if zlib fails (some older keys might not be compressed)
                    try:
                        data = pickle.loads(raw_data)
                        set_local_cache(ticker_upper, data)
                        return data
                    except:
                        print(f"CACHE_DEBUG: Multi-stage deserialization failed for {ticker_upper}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"CACHE_DEBUG: Redis access error for {ticker_upper}: {e}", file=sys.stderr)
            
    return None


def get_industry_job(job_id):
    """Get industry research job status using Direct Redis with local fallback."""
    # Try Direct Redis first (bypasses Flask-Caching worker isolation issues)
    if DIRECT_REDIS_CLIENT:
        try:
            cached = DIRECT_REDIS_CLIENT.get(f"{INDUSTRY_JOB_PREFIX}{job_id}")
            if cached:
                result = pickle.loads(zlib.decompress(cached))
                print(f"DEBUG: Direct Redis HIT for job {job_id}", file=sys.stderr)
                return result
            else:
                print(f"DEBUG: Direct Redis returned None for job {job_id}", file=sys.stderr)
        except Exception as e:
            print(f"WARN: Direct Redis get failed for job {job_id}: {e}", file=sys.stderr)
    else:
        print(f"DEBUG: No Direct Redis client, checking local cache for job {job_id}", file=sys.stderr)
    
    # Fallback to local memory
    if job_id in LOCAL_INDUSTRY_JOBS:
        entry = LOCAL_INDUSTRY_JOBS[job_id]
        age = time.time() - entry.get("timestamp", 0)
        
        if age > LOCAL_INDUSTRY_JOB_TTL:
            del LOCAL_INDUSTRY_JOBS[job_id]
            print(f"DEBUG: Local job {job_id} expired (age: {age:.0f}s)", file=sys.stderr)
            return None
        
        print(f"DEBUG: Local job cache HIT for {job_id} (age: {age:.0f}s)", file=sys.stderr)
        return entry.get("data")
    
    return None

def set_industry_job(job_id, job_data):
    """Save industry research job status using Direct Redis AND local memory."""
    # Save to local memory first (always succeeds)
    LOCAL_INDUSTRY_JOBS[job_id] = {
        "data": job_data,
        "timestamp": time.time()
    }
    
    # Then try Direct Redis (bypasses Flask-Caching)
    if DIRECT_REDIS_CLIENT:
        try:
            compressed = zlib.compress(pickle.dumps(job_data))
            DIRECT_REDIS_CLIENT.setex(
                f"{INDUSTRY_JOB_PREFIX}{job_id}",
                INDUSTRY_JOB_TTL,
                compressed
            )
            status = job_data.get('status', 'unknown')
            print(f"DEBUG: Industry job {job_id} saved to DirectRedis+Local (status={status})", file=sys.stderr)
        except Exception as e:
            print(f"WARN: Direct Redis save failed for job {job_id}: {e}", file=sys.stderr)
    else:
        print(f"DEBUG: No Direct Redis client, job {job_id} saved to local only", file=sys.stderr)

def update_industry_job(job_id, updates):
    """Update specific fields of an industry research job."""
    job = get_industry_job(job_id)
    if job:
        job.update(updates)
        set_industry_job(job_id, job)
        print(f"DEBUG: Updated job {job_id} fields: {list(updates.keys())}", file=sys.stderr)
    else:
        # CRITICAL FIX: If job not found during update, this is an error condition
        # The job should already exist from the initial creation
        print(f"ERROR: Cannot update non-existent job {job_id}. Updates lost: {list(updates.keys())}", file=sys.stderr)
        # Don't create a new job with incomplete data - this masks the real issue

from urllib.parse import urlparse

def log_redis_target(app, cache=None, context=""):
    cache_type = app.config.get("CACHE_TYPE")

    # Prefer URL if present (most reliable)
    redis_url = app.config.get("CACHE_REDIS_URL")

    host = app.config.get("CACHE_REDIS_HOST")
    port = app.config.get("CACHE_REDIS_PORT")
    ssl_enabled = app.config.get("CACHE_REDIS_SSL")

    if redis_url:
        u = urlparse(redis_url)
        host = u.hostname or host
        port = u.port or port
        ssl_enabled = (u.scheme == "rediss")

    # DNS check (helps detect Private Endpoint vs Public)
    dns_ip = None
    try:
        if host and port:
            dns_ip = socket.getaddrinfo(host, int(port))[0][4][0]
    except Exception as e:
        dns_ip = f"DNS_ERROR:{e}"

    backend_name = None
    try:
        backend_name = getattr(cache, "cache", None).__class__.__name__ if cache else None
    except Exception:
        backend_name = None

    print(
        "REDIS_DEBUG "
        f"ctx={context} "
        f"pid={os.getpid()} "
        f"instance={socket.gethostname()} "
        f"cache_type={cache_type} "
        f"backend={backend_name} "
        f"target={host}:{port} "
        f"ssl={ssl_enabled} "
        f"dns_ip={dns_ip}"
    )

# --- FINAL, ROBUST CACHE CONFIGURATION ---

# Get the raw connection string from the environment variable set in Azure
azure_redis_conn_string = os.getenv("CACHE_REDIS_URL")

# Debug: Log first/last chars of connection string to verify it's read correctly
if azure_redis_conn_string:
    safe_preview = azure_redis_conn_string[:10] + "..." + azure_redis_conn_string[-10:] if len(azure_redis_conn_string) > 30 else "[too short]"
    print(f"REDIS_ENV_DEBUG: CACHE_REDIS_URL length={len(azure_redis_conn_string)}, preview={safe_preview}", file=sys.stderr)

if azure_redis_conn_string:
    print("INFO: Found Azure Redis connection string. Parsing for Python redis library.", file=sys.stderr)
    
    # DEBUG: Log the connection string format (mask password for security)
    parts_debug = azure_redis_conn_string.split(',')
    masked_parts = []
    for p in parts_debug:
        if 'password' in p.lower() and '=' in p:
            key, val = p.split('=', 1)
            masked_parts.append(f"{key}=***MASKED***")
        else:
            masked_parts.append(p)
    print(f"REDIS_DEBUG: Connection string parts: {masked_parts}", file=sys.stderr)
    
    # This new parser is much safer and handles different connection string formats.

    try:
        redis_host = None
        redis_port = 6380
        redis_password = None
        redis_ssl = False
        redis_db = 0
        
        # Check if it's already a URL (redis:// or rediss://)
        if azure_redis_conn_string.startswith("redis://") or azure_redis_conn_string.startswith("rediss://"):
            print("INFO: Redis connection string is a URL. Parsing components.", file=sys.stderr)
            from urllib.parse import urlparse, unquote
            parsed = urlparse(azure_redis_conn_string)
            redis_host = parsed.hostname
            redis_port = parsed.port or 6380
            # Password in URL is percent-encoded, urlparse does NOT decode it - must use unquote()
            redis_password = unquote(parsed.password) if parsed.password else None
            redis_ssl = (parsed.scheme == "rediss")
            # Extract database from path (e.g., /0)
            # just for doing a push
            if parsed.path and parsed.path.startswith('/'):
                try:
                    redis_db = int(parsed.path[1:])
                except ValueError:
                    redis_db = 0
            print(f"INFO: Parsed Redis URL - host={redis_host}, port={redis_port}, ssl={redis_ssl}, db={redis_db}", file=sys.stderr)
        else:
            # Assume Azure Redis format (comma separated: hostname:port,password=...,ssl=...)
            print("INFO: Redis connection string is comma-separated format.", file=sys.stderr)
            parts = azure_redis_conn_string.split(',')
            host_part = parts[0]
            
            for part in parts[1:]:
                if '=' in part:
                    key, value = part.split('=', 1)
                    if key.lower() == 'password':
                        redis_password = value
                    elif key.lower() == 'ssl':
                        redis_ssl = value.lower() == 'true'
    
            if not redis_password:
                raise ValueError("Password not found in Redis connection string (Azure format)")
            
            # Parse host and port
            host_port_parts = host_part.split(':')
            redis_host = host_port_parts[0]
            redis_port = int(host_port_parts[1]) if len(host_port_parts) > 1 else 6380
        
        if redis_host and redis_password:
            # Use explicit parameters instead of URL to avoid parsing issues
            config = {
                "CACHE_TYPE": "RedisCache",
                "CACHE_DEFAULT_TIMEOUT": 21600,  # 6 hours
                "CACHE_REDIS_HOST": redis_host,
                "CACHE_REDIS_PORT": redis_port,
                "CACHE_REDIS_PASSWORD": redis_password,
                "CACHE_REDIS_DB": redis_db,
                "CACHE_OPTIONS": {
                    "ssl": redis_ssl,
                    "socket_timeout": 30,
                    "socket_connect_timeout": 30,
                    "retry_on_timeout": True,
                    "health_check_interval": 10
                }
            }
            print(f"INFO: Configuring cache for PRODUCTION (Redis) - host={redis_host}:{redis_port}, ssl={redis_ssl}", file=sys.stderr)
        else:
            raise ValueError("Could not extract Redis host or password")

    except Exception as e:
        print(f"CRITICAL ERROR: Failed to parse Redis connection string. Error: {e}", file=sys.stderr)
        print("FALLBACK: Using SimpleCache (no persistence)", file=sys.stderr)
        config = {"CACHE_TYPE": "SimpleCache"}


else:
    # Fallback for local development
    print("INFO: CACHE_REDIS_URL not found. Configuring cache for DEVELOPMENT (SimpleCache)")
    config = {
        "CACHE_TYPE": "SimpleCache",
        "CACHE_DEFAULT_TIMEOUT": 3600
    }

app.config.from_mapping(config)
cache = Cache(app)
log_redis_target(app, cache, context="startup_after_cache_init")

# =====================================================================
# DIRECT REDIS CLIENT FOR INDUSTRY RESEARCH (bypasses Flask-Caching)
# This ensures cross-worker consistency for background job status
# =====================================================================
import redis as redis_lib

DIRECT_REDIS_CLIENT = None
if azure_redis_conn_string:
    print(f"DIRECT_REDIS_INIT: Starting with conn_string type={type(azure_redis_conn_string).__name__}, starts_with_redis={azure_redis_conn_string.startswith('redis')}", file=sys.stderr)
    try:
        redis_host = None
        redis_port = 6380
        redis_password = None
        redis_ssl_enabled = False

        if azure_redis_conn_string.startswith("redis://") or azure_redis_conn_string.startswith("rediss://"):
            print("DIRECT_REDIS_INIT: Detected URL format, parsing components", file=sys.stderr)
            from urllib.parse import urlparse, unquote
            parsed = urlparse(azure_redis_conn_string)
            redis_host = parsed.hostname
            redis_port = parsed.port or 6380
            # Password needs explicit unquote() - urlparse doesn't auto-decode
            redis_password = unquote(parsed.password) if parsed.password else None
            redis_ssl_enabled = (parsed.scheme == "rediss")
            # Extract database from path (e.g., /0)
            redis_db = 0
            if parsed.path and parsed.path.startswith('/'):
                try:
                    redis_db = int(parsed.path[1:])
                except ValueError:
                    redis_db = 0
            
            if redis_host and redis_password:
                DIRECT_REDIS_CLIENT = redis_lib.Redis(
                    host=redis_host,
                    port=redis_port,
                    password=redis_password,
                    db=redis_db,
                    ssl=redis_ssl_enabled,
                    decode_responses=False,
                    socket_connect_timeout=30,
                    socket_timeout=30,
                    retry_on_timeout=True,
                    health_check_interval=10
                )
                DIRECT_REDIS_CLIENT.ping()
                print(f"INFO: Direct Redis client created - host={redis_host}:{redis_port}, ssl={redis_ssl_enabled}, db={redis_db}", file=sys.stderr)
            else:
                print(f"WARN: Could not parse Redis URL credentials", file=sys.stderr)
                DIRECT_REDIS_CLIENT = None
                
        else:
            # Parse Azure comma-separated format manually (legacy)
            parts = azure_redis_conn_string.split(',')
            host_part = parts[0]
            
            for part in parts[1:]:
                if '=' in part:
                    key, value = part.split('=', 1)
                    if key.lower() == 'password':
                        redis_password = value
                    elif key.lower() == 'ssl':
                        redis_ssl_enabled = value.lower() == 'true'
            
            if redis_password:
                host_port_parts = host_part.split(':')
                redis_host = host_port_parts[0]
                redis_port = int(host_port_parts[1]) if len(host_port_parts) > 1 else 6380
        
                # Create direct Redis connection pool (shared across all workers)
                DIRECT_REDIS_CLIENT = redis_lib.Redis(
                    host=redis_host,
                    port=redis_port,
                    password=redis_password,
                    ssl=redis_ssl_enabled,
                    decode_responses=False,  # Keep as bytes for pickle
                    socket_connect_timeout=30,
                    socket_timeout=30,
                    retry_on_timeout=True
                )
                # Test connection
                DIRECT_REDIS_CLIENT.ping()
                print(f"INFO: Direct Redis client created for industry research (host={redis_host})", file=sys.stderr)
            else:
                print("WARN: Could not extract Redis credentials (host/password), direct client not created", file=sys.stderr)
            
    except Exception as e:
        print(f"WARN: Failed to create direct Redis client: {e}", file=sys.stderr)
        DIRECT_REDIS_CLIENT = None
else:
    print("INFO: No Azure Redis configured, using local-only storage for industry jobs")

from flask import jsonify

@app.route('/debug/env', methods=['GET'])
def debug_env():
    # Admin-only: check authentication
    from flask import session as flask_session
    if 'user_id' not in flask_session:
        return jsonify({'error': 'Authentication required'}), 401
    from auth.database import User
    user = User.get_by_id(flask_session['user_id'])
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    keys = [
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT",
        "PERPLEXITY_API_KEY",
        "GOOGLE_API_KEY",
        "TRENDLYNE_USERNAME",
        "TRENDLYNE_PASSWORD",
        "PARALLEL_API_KEY",
    ]
    # returns True/False (no secrets leaked)
    return jsonify({k: bool(os.getenv(k)) for k in keys})

# =====================================================================
# STOCK AUTOCOMPLETE API
# =====================================================================
import csv

# Load stocks from CSV on startup
STOCKS_LIST = []
try:
    with open('trendlyne_all_stocks_master.csv', 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            STOCKS_LIST.append({
                'name': row.get('Stock Name', ''),
                'ticker': row.get('Ticker', ''),
            })
    print(f"INFO: Loaded {len(STOCKS_LIST)} stocks for autocomplete")
except Exception as e:
    print(f"WARNING: Could not load stocks CSV: {e}")

@app.route('/api/stocks')
def api_stocks():
    """Return all stocks for client-side autocomplete filtering"""
    return jsonify(STOCKS_LIST)

@app.route('/api/ask-unanswerable', methods=['POST'])
def api_ask_unanswerable():
    """Start a Parallel Deep Research task"""
    if not PARALLEL_API_KEY:
        return jsonify({"error": "Parallel API key not configured."}), 500
    
    data = request.json
    question = data.get("question")
    processor = data.get("processor", "ultra-fast")
    
    # Sanity check for processor
    if processor not in ["pro-fast", "ultra-fast", "ultra"]:
        processor = "ultra-fast"

    if not question:
        return jsonify({"error": "Question is required."}), 400
    
    try:
        # STEP 1: Skip Expansion - use original question
        print(f"INFO: Using original question for Parallel: {question[:100]}...", file=sys.stderr)
        # expansion_prompt = get_unanswerable_expansion_prompt(question)
        # expanded_question = call_generative_ai_model(
        #     model="gpt-5-mini",
        #     messages=[{"role": "user", "content": expansion_prompt}],
        #     temperature=1
        # )
        expanded_question = question
        print(f"INFO: Research Question: {expanded_question}", file=sys.stderr)

        # Using pure httpx/requests to ensure beta headers are included for SSE
        headers = {
            "x-api-key": PARALLEL_API_KEY,
            "parallel-beta": "events-sse-2025-07-24",
            "Content-Type": "application/json"
        }
        payload = {
            "input": question,  # Send original question to Parallel
            "processor": processor,
            "enable_events": True
        }
        
        response = requests.post("https://api.parallel.ai/v1/tasks/runs", headers=headers, json=payload)
        response.raise_for_status()
        run_data = response.json()
        
        return jsonify({
            "run_id": run_data.get("run_id"),
            "status": "started",
            "expanded_question": expanded_question
        })
    except Exception as e:
        print(f"ERROR starting Parallel task: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/ask-unanswerable/stream/<run_id>')
def api_ask_unanswerable_stream(run_id):
    """Stream progress events from Parallel API via httpx for better SSE stability"""
    if not PARALLEL_API_KEY:
        return Response("data: {\"error\": \"Parallel API key not configured.\"}\n\n", mimetype='text/event-stream')

    headers = {
        "x-api-key": PARALLEL_API_KEY,
        "Accept": "text/event-stream",
        "parallel-beta": "events-sse-2025-07-24"
    }
    
    def generate():
        try:
            print(f"INFO: Connecting to Parallel stream for run_id: {run_id}", file=sys.stderr)
            current_event = None
            with httpx.stream(
                "GET",
                f"https://api.parallel.ai/v1beta/tasks/runs/{run_id}/events",
                headers=headers,
                timeout=None
            ) as r:
                if r.status_code != 200:
                    error_text = r.read().decode()
                    print(f"ERROR: Parallel API returned status {r.status_code}: {error_text}", file=sys.stderr)
                    yield f"data: {json.dumps({'error': f'Upstream error {r.status_code}'})}\n\n"
                    return

                print("INFO: Connected to Parallel Stream. Flattening events...", file=sys.stderr)
                for line in r.iter_lines():
                    if line.startswith("event:"):
                        current_event = line.replace("event:", "").strip()
                    elif line.startswith("data:"):
                        data_str = line.replace("data:", "").strip()
                        try:
                            # Flattening: Merge the event type into the data JSON
                            data_json = json.loads(data_str)
                            if current_event:
                                data_json["event_type"] = current_event
                                current_event = None
                            
                            # Add status if not present (for robustness)
                            if "status" not in data_json and "task_run.status" in (data_json.get("event_type", ""), ""):
                                # If it's a state event, it usually has the status inside
                                pass

                            yield f"data: {json.dumps(data_json)}\n\n"
                        except json.JSONDecodeError:
                            # Fallback if it's not JSON
                            yield f"{line}\n\n"
                    elif line == "":
                        # Standard SSE separator
                        pass
        except Exception as e:
            print(f"ERROR streaming Parallel events: {e}", file=sys.stderr)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/api/ask-unanswerable/status/<run_id>')
def api_ask_unanswerable_status(run_id):
    """Check status of a research task and return result if completed."""
    if not PARALLEL_API_KEY:
        return jsonify({"error": "Parallel API key not configured"}), 401

    try:
        headers = {
            "x-api-key": PARALLEL_API_KEY,
            "parallel-beta": "events-sse-2025-07-24"
        }
        response = requests.get(f"https://api.parallel.ai/v1/tasks/runs/{run_id}", headers=headers)
        response.raise_for_status()
        data = response.json()
        
        # Normalize response for frontend
        return jsonify({
            "status": data.get("status"),
            "completed": data.get("status") == "completed",
            "output": data.get("output", {}),
            "error": data.get("error")
        })
    except Exception as e:
        print(f"ERROR checking Parallel status: {e}", file=sys.stderr)
        return jsonify({"error": str(e)}), 500

@app.route('/debug/cookies-source')
def debug_cookies_source():
    """Debug endpoint to check which source Trendlyne cookies are loaded from (admin only)"""
    from flask import session as flask_session
    import json as json_lib

    # Admin-only: check authentication
    if 'user_id' not in flask_session:
        return jsonify({'error': 'Authentication required'}), 401
    from auth.database import User
    user = User.get_by_id(flask_session['user_id'])
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    result = {
        'env_var_exists': bool(os.getenv('TRENDLYNE_COOKIES')),
        'env_var_length': len(os.getenv('TRENDLYNE_COOKIES', '')),
        'file_exists': os.path.exists('analyst_reports/trendlyne_cookies.json'),
        'loaded_cookies_count': 0,
        'cookie_keys': []
    }
    
    # Try to parse env var
    if result['env_var_exists']:
        try:
            cookies = json_lib.loads(os.getenv('TRENDLYNE_COOKIES'))
            result['env_var_valid'] = True
            result['env_var_cookie_count'] = len(cookies)
            result['env_var_keys'] = list(cookies.keys())
        except:
            result['env_var_valid'] = False
    
    # Check what the module actually loaded
    from analyst_reports.pdf_summarizer_cookies import TRENDLYNE_COOKIES
    result['loaded_cookies_count'] = len(TRENDLYNE_COOKIES)
    result['cookie_keys'] = list(TRENDLYNE_COOKIES.keys())
    
    return jsonify(result)

# Serve front-end HTML - Landing page (public)
@app.route('/')
def index():
    return send_from_directory('.', 'landing.html')

# =====================================================================
# CONTACT FORM ENDPOINT (Landing Page Access Requests)
# =====================================================================

CONTACT_REQUESTS_FILE = os.path.join(os.path.dirname(__file__), 'contact_requests.json')

def load_contact_requests():
    """Load contact requests from JSON file"""
    try:
        if os.path.exists(CONTACT_REQUESTS_FILE):
            with open(CONTACT_REQUESTS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"WARN: Failed to load contact requests: {e}")
    return []

def save_contact_requests(requests):
    """Save contact requests to JSON file"""
    try:
        with open(CONTACT_REQUESTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(requests, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"ERROR: Failed to save contact requests: {e}")
        raise

@app.route('/contact-form', methods=['POST'])
def contact_form():
    """
    Handle landing page contact form submissions.
    Stores to contact_requests.json for persistence across deployments.
    """
    try:
        data = request.get_json(force=True)
        name = data.get('name', '').strip()
        email = data.get('email', '').strip()
        phone = data.get('phone', '').strip()
        message = data.get('message', '').strip()
        
        # Validate required fields
        if not name or not email:
            return jsonify({'error': 'Name and email are required'}), 400
        
        # Basic email validation
        import re
        if not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', email):
            return jsonify({'error': 'Invalid email format'}), 400
        
        # Create request entry
        from datetime import datetime
        contact_request = {
            'id': str(uuid.uuid4())[:8],
            'name': name,
            'email': email,
            'phone': phone if phone else None,
            'message': message if message else None,
            'submitted_at': datetime.now().isoformat(),
            'status': 'new'  # new, contacted, converted, declined
        }
        
        # Load existing requests, add new one, save
        requests = load_contact_requests()
        requests.insert(0, contact_request)  # Newest first
        save_contact_requests(requests)
        
        print(f"INFO: New contact request from {name} <{email}>")
        
        return jsonify({
            'success': True,
            'message': 'Your request has been received. We will be in touch soon.'
        })
        
    except Exception as e:
        print(f"ERROR: Contact form submission failed: {e}")
        traceback.print_exc()
        return jsonify({'error': 'Failed to submit request. Please try again.'}), 500

@app.route('/api/admin/contact-requests', methods=['GET'])
def get_contact_requests():
    """Admin endpoint to view all contact requests"""
    from flask import session
    
    # Check authentication
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    # Check admin role
    from auth.database import User
    user = User.get_by_id(session['user_id'])
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    requests = load_contact_requests()
    return jsonify({'requests': requests, 'count': len(requests)})

@app.route('/api/admin/contact-requests/<request_id>/status', methods=['PUT'])
def update_contact_request_status(request_id):
    """Admin endpoint to update contact request status"""
    from flask import session
    
    # Check authentication
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    # Check admin role
    from auth.database import User
    user = User.get_by_id(session['user_id'])
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    data = request.get_json(force=True)
    new_status = data.get('status', '').strip()
    
    if new_status not in ['new', 'contacted', 'converted', 'declined']:
        return jsonify({'error': 'Invalid status'}), 400
    
    requests = load_contact_requests()
    for req in requests:
        if req.get('id') == request_id:
            req['status'] = new_status
            save_contact_requests(requests)
            return jsonify({'success': True})
    
    return jsonify({'error': 'Request not found'}), 404

@app.route('/api/admin/contact-requests/<request_id>', methods=['DELETE'])
def delete_contact_request(request_id):
    """Admin endpoint to delete a contact request"""
    from flask import session
    
    # Check authentication
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    # Check admin role
    from auth.database import User
    user = User.get_by_id(session['user_id'])
    if not user or not user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    requests = load_contact_requests()
    original_count = len(requests)
    requests = [req for req in requests if req.get('id') != request_id]
    
    if len(requests) == original_count:
        return jsonify({'error': 'Request not found'}), 404
    
    save_contact_requests(requests)
    return jsonify({'success': True})


# =====================================================================
# BLOG SECTION - AI-Generated Blog Posts
# =====================================================================

# Blog data persistence (same pattern as contact requests)
if os.path.exists('/home'):
    BLOGS_FILE = '/home/blogs.json'
else:
    BLOGS_FILE = os.path.join(os.path.dirname(__file__), 'blogs.json')

def load_blogs():
    """Load blog posts from JSON file"""
    try:
        if os.path.exists(BLOGS_FILE):
            with open(BLOGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"WARN: Failed to load blogs: {e}")
    return []

def save_blogs(blogs):
    """Save blog posts to JSON file"""
    try:
        with open(BLOGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(blogs, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"ERROR: Failed to save blogs: {e}")
        raise

def generate_slug(title):
    """Generate a URL-friendly slug from a title"""
    import re
    slug = title.lower().strip()
    slug = re.sub(r'[^\w\s-]', '', slug)
    slug = re.sub(r'[\s_]+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    slug = slug.strip('-')
    return slug[:80]  # Limit length

def call_openai_responses_api(instructions, user_input, use_web_search=True, model="gpt-5.2-pro"):
    """
    Call OpenAI Responses API with optional web search.
    Uses streaming to prevent Azure SNAT TCP idle timeout (4 min).
    """
    import openai as _openai
    if not _openai.api_key and not os.environ.get('OPENAI_API_KEY'):
        raise ValueError("OpenAI API key is not configured.")
    
    try:
        client = _openai.OpenAI(timeout=600.0)
        
        tools = []
        if use_web_search:
            tools.append({"type": "web_search_preview"})
        
        create_params = {
            "model": model,
            "instructions": instructions,
            "input": user_input,
            "reasoning": {"effort": "high"},
            "stream": True
        }
        if tools:
            create_params["tools"] = tools
        
        print(f"INFO: Calling OpenAI Responses API (streaming) with model={model}, web_search={use_web_search}", file=sys.stderr)
        sys.stderr.flush()
        
        stream = client.responses.create(**create_params)
        
        # Collect output text from stream events
        output_parts = []
        event_count = 0
        for event in stream:
            event_count += 1
            # Log periodic progress so Azure sees activity
            if event_count % 50 == 0:
                print(f"INFO: OpenAI stream progress — {event_count} events received", file=sys.stderr)
                sys.stderr.flush()
            
            # Collect text output events
            if hasattr(event, 'type'):
                if event.type == 'response.output_text.delta':
                    if hasattr(event, 'delta'):
                        output_parts.append(event.delta)
                elif event.type == 'response.completed':
                    # Final event — extract full output_text from response
                    if hasattr(event, 'response') and hasattr(event.response, 'output_text'):
                        output_text = event.response.output_text
                        print(f"INFO: OpenAI Responses API streaming complete. Total events: {event_count}", file=sys.stderr)
                        sys.stderr.flush()
                        return output_text
        
        # Fallback: if we didn't get a response.completed event, use collected parts
        result = ''.join(output_parts)
        if result:
            print(f"INFO: OpenAI Responses API streaming complete (from deltas). Total events: {event_count}", file=sys.stderr)
            sys.stderr.flush()
            return result
        
        raise RuntimeError(f"OpenAI streaming completed with {event_count} events but no output text was captured.")
        
    except Exception as e:
        print(f"ERROR in call_openai_responses_api: {e}", file=sys.stderr)
        sys.stderr.flush()
        traceback.print_exc()
        raise

# --- Admin Blog API Endpoints ---

def check_admin_auth():
    """Helper to check admin authentication. Returns (user, error_response) tuple."""
    from flask import session
    if 'user_id' not in session:
        return None, (jsonify({'error': 'Not authenticated'}), 401)
    from auth.database import User
    user = User.get_by_id(session['user_id'])
    if not user or not user.is_admin:
        return None, (jsonify({'error': 'Admin access required'}), 403)
    return user, None

@app.route('/api/admin/blogs', methods=['GET'])
def admin_list_blogs():
    """Admin endpoint to list all blog posts (drafts + published)"""
    user, err = check_admin_auth()
    if err:
        return err
    
    blogs = load_blogs()
    return jsonify({'blogs': blogs, 'count': len(blogs)})

import threading

# In-memory store for async blog generation jobs
BLOG_GENERATION_JOBS = {}

@app.route('/api/admin/blogs/generate/status/<job_id>', methods=['GET'])
def admin_blog_generate_status(job_id):
    """Poll the status of an async blog generation job."""
    user, err = check_admin_auth()
    if err:
        return err
    
    job = BLOG_GENERATION_JOBS.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    
    return jsonify(job)


def _run_blog_generation(job_id, topic, article_type):
    """Background worker: runs Maker + Checker pipeline and saves the blog."""
    try:
        BLOG_GENERATION_JOBS[job_id]['status'] = 'running'
        BLOG_GENERATION_JOBS[job_id]['phase'] = 'maker'
        
        # --- SHARED EDITORIAL DNA ---
        shared_style = """Act as a seasoned, witty, and investigative senior feature writer for a premium, new-age business publication (think a blend of 'The Ken', 'Bloomberg Businessweek', and 'YourStory'). You also have access to real-time web search capabilities.
1. NARRATIVE STYLE & TONE:
- Write with a TIGHT STORYLINE. Every paragraph must advance the core narrative. The reader should feel pulled through the article by a clear logical thread: What happened → Why it matters → Who wins/loses → What comes next.
- Start with a compelling hook: a surprising data point, a concrete real-world event, or a counterintuitive observation. NOT an idiom or proverb.
- The tone should be conversational, witty, and authoritative — like a sharp analyst who also happens to be great at a dinner party. Write as if you are explaining an important business story to a smart friend over coffee.
- BRING PERSONALITY: Sprinkle in 1-2 offhand jokes, dry witty remarks, or clever asides per article. These should feel natural and observational (e.g., 'At this rate, India's data centre operators are consuming more concrete than the entire housing ministry — and probably getting more done with it'). But do NOT use idioms, proverbs, or stock phrases. Your humor should come from sharp observation, irony, or absurdity in the data itself.
- CRITICAL: Write so that a layperson with NO domain expertise can understand the story. If you must use a technical term or industry jargon, IMMEDIATELY explain it in simple, plain language (e.g., 'EBITDA — essentially, how much cash the business actually generates before accounting tricks').
- Avoid sounding like a dry newspaper article, an academic paper, a PR press release, or a generic AI. This is a blog by a witty, analytical reporter — not a wire service dispatch."""

        shared_data = """DATA, QUOTES & EXPERTISE (THIS IS THE BACKBONE OF THE ARTICLE):
- The article's credibility comes from DATA, not from clever wordplay. Every major claim MUST be backed by a specific number, a named source, or a verifiable fact.
- Actively search for and cite data from: company annual reports/filings, earnings call transcripts, analyst reports (Goldman Sachs, Morgan Stanley, Jefferies, etc.), industry research (McKinsey, Gartner, BCG, Bain, CRISIL, ICRA), government data/policy documents, and credible news sources.
- Interweave direct quotes from CEOs, CFOs, industry analysts, or domain experts. Name the person and their role (e.g., 'As Satya Nadella told investors on Microsoft's Q3 earnings call...').
- Make data tell the story. Instead of vague claims like 'the market is growing rapidly', write 'India's data centre capacity hit 1,100 MW in 2025, up from just 450 MW in 2020, according to JLL's latest India Data Centre Report.'
- Include at least 5-8 specific data points from reputed sources throughout the article."""

        shared_sourcing = """SOURCING RULES:
- DO NOT use in-text citations, footnotes, [1], or hyperlink placeholders anywhere in the main body. 
- Weave source attribution naturally into the narrative when naming a specific report or expert (e.g., 'A recent Gartner study pegged the market at $4.2 billion' — NOT 'According to [1], the market is...').
- At the very bottom of the article, create a distinct section titled "Sources & References:" where you list the URLs and names of the real-world articles, reports, or data sources you fetched during your web search. Ensure the piece is entirely truthful."""

        shared_anti_ai = """ANTI-AI & CLICHE DIRECTIVES (CRITICAL):
- DO NOT use the overused AI words and phrases such as: delve, tapestry, testament, bustling, landscape, navigating the complexities, a symphony of, beacon, robust, dynamic, paramount, revolutionize, foster, or 'in conclusion'.
- ABSOLUTELY NO idioms, proverbs, metaphorical cliches, or figurative expressions. Examples of what to AVOID: 'tip of the iceberg', 'silver bullet', 'elephant in the room', 'double-edged sword', 'perfect storm', 'game changer', 'the writing is on the wall', 'hit the ground running'. Use PLAIN, DIRECT language instead.
- DO NOT wrap technical concepts in idiomatic language. Instead of 'the company is walking a tightrope between growth and profitability', write 'the company's operating margins fell from 18% to 11% as it prioritized revenue growth over profitability.'
- DO NOT give in to too much imagination or 'flights of fantasy.' Keep the story grounded in verifiable reality.
- Vary your sentence lengths. Use short, punchy sentences for impact. Use active voice and strong verbs.
- EXPLAIN, don't decorate. The reader should finish the article feeling INFORMED, not impressed by your vocabulary."""

        shared_output = """OUTPUT FORMAT:
- Start your response with a JSON line containing the article title: {"title": "Your Article Title Here"}
- Then a line with just ---
- Then the full article as clean HTML using <h2>, <h3>, <p>, <ul>/<li>, <strong>, <em>, <blockquote> tags.
- Do NOT include <html>, <head>, <body>, or <h1> tags — just the article body content.
- Place the "Sources & References:" section at the very end as an <h2> with a list of source URLs.

Now, execute your search and write the article."""

        # --- ARTICLE TYPE SPECIFIC PROMPTS ---
        if article_type == 'needle':
            instructions = f"""Act as a seasoned, witty, and investigative senior feature writer AND stock-picking analyst for a premium, new-age business publication (think a blend of 'The Ken', 'Bloomberg Businessweek', and 'YourStory'). You also have access to real-time web search capabilities.

Your task is to write a highly engaging, long-form "Needle in a Haystack" article. The admin has provided a topic describing a regulatory, economic, demographic, or structural change. Your job is to:
1. Research the change/event/trend deeply using web search.
2. Identify the RARE company (or rare few companies) — the "needle" — that stands to benefit the MOST from this change. These should NOT be the obvious large-cap names everyone already knows. Dig deeper. Find the under-the-radar, mid-cap, or small-cap plays that have a structural edge.
3. Build a compelling narrative around WHY these specific companies are uniquely positioned.

STEP 1: AUTONOMOUS RESEARCH
Before you begin writing, use your web search capabilities to fetch the most recent and relevant news, financial data, policy documents, and deep-dive analyses on this topic. Research which companies (especially lesser-known ones) have the most exposure to this change.

STEP 2: DRAFT THE ARTICLE
Using the real-world data you just fetched, write the article following these strict editorial guidelines:

{shared_style}

2. STRUCTURE & PACING:
- Start with a hook: Pull the reader in with a surprising fact, a counterintuitive observation, or a cinematic/relatable opening that frames the structural change.
- Move on to the core idea: Establish the big change and why it creates an asymmetric opportunity.
- Describe the current landscape: Briefly paint the landscape of the sector/industry affected.
- The Needle(s): Reveal the specific company/companies that are uniquely positioned. This is the CORE of the article. Explain exactly WHY — moats, supply chain position, regulatory advantage, capacity, management vision, financial metrics.
- Back up with Data: Back up the thesis with hard numbers — revenue exposure, capacity utilization, order books, margin profiles, valuations, etc. Give whatever data is relevant, required and helps bring value to the article.
- End with conclusion: End on a sharp, thought-provoking note. Do NOT give buy/sell recommendations. Frame it as "this is where the smart money should be looking."

{shared_data}

{shared_sourcing}

{shared_anti_ai}

{shared_output}"""

        elif article_type == 'informative':
            instructions = f"""Act as a seasoned, authoritative, and deeply knowledgeable senior feature writer for a premium, new-age business publication (think a blend of 'The Ken', 'Bloomberg Businessweek', and 'YourStory'). You also have access to real-time web search capabilities.

Your task is to write a highly informative, comprehensive, and data-dense long-form article about the topic provided. This is an INFORMATIVE article — no fluff, no flights of fantasy, no speculative scenarios. Every paragraph must add tangible information or insight.


STEP 1: AUTONOMOUS RESEARCH
Before you begin writing, use your web search capabilities to fetch the most recent and relevant news, financial data, regulatory filings, expert opinions, and deep-dive analyses on this topic. Be exhaustive.

STEP 2: DRAFT THE ARTICLE
Using the real-world data you just fetched, write the article following these strict editorial guidelines:

{shared_style}

2. STRUCTURE & PACING:
- Start with a hook: Pull the reader in with a sharp, relevant opening — a surprising statistic, a recent event, or a key question.
- Proceed with context: Provide the full background — history, stakeholders, regulatory framework, market dynamics. Assume the reader is intelligent but may not know the domain.
- Deep Dive: Break down the mechanics, the data, the key players, and the implications. Cover every important dimension: financial, regulatory, competitive, and strategic.
- The "So What": Explain the real-world implications clearly. What does this mean for businesses, investors, or the industry?
- End with conclusion: End with a crisp conclusion that crystallizes the key takeaway — no generic summaries, just the sharpest insight.

IMPORTANT: Do NOT include "flights of fantasy" or hypothetical scenarios. Keep everything grounded in verified facts and real data. Be dense with information, not prose.

{shared_data}

{shared_sourcing}

{shared_anti_ai}

{shared_output}"""

        else:  # 'general' — the original investigative blog
            instructions = f"""Act as a seasoned, witty, and investigative senior feature writer for a premium, new-age business publication (think a blend of the analytical, slightly cynical, and deep-dive style of 'The Ken' and the founder-centric, narrative-driven storytelling of 'YourStory'). You also have access to real-time web search capabilities.

Your task is to write a highly engaging, long-form business/tech blog post about the topic provided by the user.
THINK DEEPLY: You must use the highest level of reasoning effort to critically analyze the topic and web search results before drafting.

STEP 1: AUTONOMOUS RESEARCH
Before you begin writing, use your web search capabilities to fetch the most recent and relevant news, financial data, and deep-dive analyses on this topic. You do not need to print out your research notes—simply use the data you find to construct the narrative. 

STEP 2: DRAFT THE ARTICLE
Using the real-world data you just fetched, write the article following these strict editorial guidelines:

{shared_style}

1. STRUCTURE & PACING:
- Start with a hook: Pull the reader in with a surprising fact, counterintuitive observation, or cinematic or relatable opening that flows naturally into the core story.
- Proceed with thesis (The "Why Should I Care"): Establish the core conflict or the big revelation early on.
- Do deep dive: Break down the business model, the strategy, or the history. Explain complex mechanics simply but smartly. Interweave your expert quotes and research data here.
- The future outlook: Predict the natural trajectory of this topic. Do NOT use overly imaginative or fantasized scenarios; whatever predictions you make must flow logically and naturally as part of the overarching story you've woven from the data.
- The kicker (Conclusion): End on a sharp, thought-provoking note—not a neat, summarizing bow. Leave the reader pondering the implications.

{shared_data}

{shared_sourcing}

{shared_anti_ai}

{shared_output}"""

        user_input = f"{topic}"
        
        print(f"INFO: Generating blog [{article_type}] for topic: {topic}", file=sys.stderr)
        BLOG_GENERATION_JOBS[job_id]['phase'] = 'maker'
        raw_response = call_openai_responses_api(instructions, user_input, use_web_search=True)
        
        # Check if the AI declined the topic
        first_line = raw_response.strip().split('\n')[0].strip()
        if first_line.startswith('{') and '"decline"' in first_line:
            try:
                decline_data = json.loads(first_line)
                if decline_data.get('decline'):
                    reason = decline_data.get('reason', 'This topic does not have enough depth for a quality blog post.')
                    BLOG_GENERATION_JOBS[job_id].update({
                        'status': 'declined',
                        'error': f"🤔 The AI chose not to write this blog: {reason}"
                    })
                    return
            except json.JSONDecodeError:
                pass
        
        # Parse the response — extract title from first JSON line
        title = f"Blog: {topic}"  # fallback title
        content_html = raw_response
        short_topic = ''  # will be set by checker
        
        # --- MAKER-CHECKER: CHECKER PHASE ---
        print(f"INFO: [Checker Phase] Sending draft to Gemini 3.1 Pro for editorial review...", file=sys.stderr)
        BLOG_GENERATION_JOBS[job_id]['phase'] = 'checker'
        
        checker_prompt = f"""You are the sharp, rigorous, and brilliant Investigative Head Editor for our premium business publication (think the absolute best editors at 'The Ken' or 'Bloomberg Businessweek'). 
You have Google Search grounding enabled.

One of your senior reporters has just submitted a draft blog post on the topic: "{topic}".

Here are the exact editorial guidelines the reporter was given to write this piece. You must understand the spirit, tone, narrative styling, and structure of what we want, and enforce it:
---
{instructions}
---

YOUR JOB:
1. Act as the checker/editor. You have the total power to edit, update, append, rewrite, or change any part of this blog.
2. IDIOM & PROVERB PURGE: Scan the ENTIRE draft and REMOVE or REWRITE every single idiom, proverb, metaphor, or figurative cliche. Replace each one with a plain, direct statement backed by data. HOWEVER, PRESERVE any witty remarks, dry humor, offhand jokes, or clever observational asides — these add personality and are DIFFERENT from cliches. The article should read like a witty analyst's blog, not a dry newspaper report.
3. DATA DENSITY CHECK: The article MUST contain at least 5-8 specific, sourced data points from reputed sources (company filings, analyst reports, industry research by McKinsey/Gartner/CRISIL/etc., government data). If the draft is light on data, USE YOUR SEARCH GROUNDING to find and ADD real numbers, real quotes from named executives or analysts, and real research findings.
4. LAYPERSON CLARITY CHECK: Read every paragraph as if you are a reader with NO business/finance background. If any concept, term, or mechanism is not immediately clear, add a brief plain-language explanation. The article should be understandable to anyone.
5. STORYLINE TIGHTNESS: The article must have a clear narrative thread that a reader can follow from start to finish. Every paragraph must connect to the next. If sections feel disconnected, rewrite the transitions.
6. VERIFY facts, timeline, quotes, and research excerpts using your search grounding. If anything is wrong, outdated, or weak, FIX IT.
7. Final Word Count: Limit the total word count of the final article to a maximum of 2000 words. Be concise and impactful.
8. Ensure structural rules:
   - NO in-text citations or footnotes (no [1] or hyperlink placeholders).
   - "Sources & References:" section at the very bottom with a list of URLs used.
   - ZERO banned AI words: delve, tapestry, testament, bustling, landscape, navigating the complexities, a symphony of, beacon, robust, dynamic, paramount, revolutionize, foster, 'in conclusion'.
   - ZERO idioms, proverbs, or figurative cliches.

OUTPUT FORMAT:
- First line MUST be a JSON object containing the finalized title AND a short topic label (max 10 words, tactical, concise, highlighting the key theme — NOT the full topic): {{"title": "The Final Masterpiece Title", "short_topic": "India's IT Services Crisis"}}
- Second line MUST be exactly three dashes: ---
- Then provide the incredibly clean, polished HTML (<h2>, <h3>, <p>, <ul>/<li>, <strong>, <em>, <blockquote>). Ensure the semantic HTML supports the narrative flow.
- DO NOT wrap the output in ```html blocks or include <html>, <head>, or <body> tags. Just the raw HTML content.

Here is the reporter's draft:
=========================================
{raw_response}
=========================================

Now, do your job as the Head Editor and output the finalized, publication-ready piece (under 2000 words)."""

        try:
            checker_response = call_gemini_api(
                messages=[{"role": "user", "content": checker_prompt}],
                model="gemini-3.1-pro-preview",
                temperature=1.0,
                use_google_search=True,
                thinking_level="HIGH"
            )
            print(f"INFO: [Checker Phase] Editorial review complete.", file=sys.stderr)
            
            # Clean potential markdown wrapping from Gemini
            if checker_response.startswith("```html"):
                checker_response = checker_response[7:]
            if checker_response.startswith("```"):
                checker_response = checker_response[3:]
            if checker_response.endswith("```"):
                checker_response = checker_response[:-3]
            
            checker_response = checker_response.strip()
            
            lines = checker_response.split('\n')
            for i, line in enumerate(lines):
                line_stripped = line.strip()
                if line_stripped.startswith('{') and '"title"' in line_stripped:
                    try:
                        title_data = json.loads(line_stripped)
                        title = title_data.get('title', title)
                        short_topic = title_data.get('short_topic', short_topic)
                        # Find the separator and get content after it
                        remaining = '\n'.join(lines[i+1:])
                        if '---' in remaining:
                            content_html = remaining.split('---', 1)[1].strip()
                        else:
                            content_html = remaining.strip()
                        break
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            print(f"ERROR: Checker phase failed: {e}. Falling back to Maker draft.", file=sys.stderr)
            lines = raw_response.split('\n')
            for i, line in enumerate(lines):
                line_stripped = line.strip()
                if line_stripped.startswith('{') and '"title"' in line_stripped:
                    try:
                        title_data = json.loads(line_stripped)
                        title = title_data.get('title', title)
                        short_topic = title_data.get('short_topic', short_topic)
                        # Find the separator and get content after it
                        remaining = '\n'.join(lines[i+1:])
                        if '---' in remaining:
                            content_html = remaining.split('---', 1)[1].strip()
                        else:
                            content_html = remaining.strip()
                        break
                    except json.JSONDecodeError:
                        pass
        
        # If no JSON header found, use full response as content
        if content_html == raw_response:
            # Try to extract a title from the first heading
            import re
            h_match = re.search(r'<h[12][^>]*>(.*?)</h[12]>', content_html)
            if h_match:
                title = re.sub(r'<[^>]+>', '', h_match.group(1)).strip()
        
        BLOG_GENERATION_JOBS[job_id]['phase'] = 'saving'
        
        from datetime import datetime
        blog_id = str(uuid.uuid4())[:8]
        slug = generate_slug(title)
        
        # Ensure short_topic is set
        if not short_topic:
            # Fallback: truncate topic to max 10 words
            words = topic.split()
            short_topic = ' '.join(words[:10])
        
        # Ensure slug is unique
        blogs = load_blogs()
        existing_slugs = {b['slug'] for b in blogs}
        original_slug = slug
        counter = 1
        while slug in existing_slugs:
            slug = f"{original_slug}-{counter}"
            counter += 1
        
        blog = {
            'id': blog_id,
            'title': title,
            'slug': slug,
            'topic': topic,
            'article_type': article_type,
            'short_topic': short_topic,
            'content_html': content_html,
            'status': 'draft',
            'created_at': datetime.now().isoformat(),
            'published_at': None,
            'updated_at': datetime.now().isoformat(),
            'suggestions': []
        }
        
        blogs.insert(0, blog)
        save_blogs(blogs)
        
        print(f"INFO: Blog generated — id={blog_id}, title={title}", file=sys.stderr)
        
        BLOG_GENERATION_JOBS[job_id].update({
            'status': 'complete',
            'phase': 'done',
            'blog': blog
        })
    
    except Exception as e:
        print(f"ERROR: Blog generation failed: {e}", file=sys.stderr)
        traceback.print_exc()
        BLOG_GENERATION_JOBS[job_id].update({
            'status': 'error',
            'error': f'Blog generation failed: {str(e)}'
        })


@app.route('/api/admin/blogs/generate', methods=['POST'])
def admin_generate_blog():
    """Admin endpoint to generate a blog post — kicks off async job and returns job_id."""
    user, err = check_admin_auth()
    if err:
        return err
    
    try:
        data = request.get_json(force=True)
        topic = data.get('topic', '').strip()
        article_type = data.get('article_type', 'general').strip()
        
        if not topic:
            return jsonify({'error': 'Topic is required'}), 400
        
        job_id = str(uuid.uuid4())[:8]
        BLOG_GENERATION_JOBS[job_id] = {
            'status': 'starting',
            'phase': 'queued',
            'topic': topic,
            'article_type': article_type
        }
        
        # Start background thread
        t = threading.Thread(target=_run_blog_generation, args=(job_id, topic, article_type), daemon=True)
        t.start()
        
        print(f"INFO: Blog generation job started — job_id={job_id}, topic={topic}", file=sys.stderr)
        return jsonify({'success': True, 'job_id': job_id})
    
    except Exception as e:
        print(f"ERROR: Failed to start blog generation: {e}", file=sys.stderr)
        traceback.print_exc()
        return jsonify({'error': f'Failed to start generation: {str(e)}'}), 500

@app.route('/api/admin/blogs/<blog_id>', methods=['PUT'])
def admin_update_blog(blog_id):
    """Admin endpoint to update a blog post"""
    user, err = check_admin_auth()
    if err:
        return err
    
    try:
        data = request.get_json(force=True)
        blogs = load_blogs()
        
        for blog in blogs:
            if blog['id'] == blog_id:
                if 'title' in data:
                    blog['title'] = data['title'].strip()
                    blog['slug'] = generate_slug(blog['title'])
                if 'content_html' in data:
                    blog['content_html'] = data['content_html']
                from datetime import datetime
                blog['updated_at'] = datetime.now().isoformat()
                save_blogs(blogs)
                return jsonify({'success': True, 'blog': blog})
        
        return jsonify({'error': 'Blog not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/blogs/<blog_id>/publish', methods=['POST'])
def admin_publish_blog(blog_id):
    """Admin endpoint to publish a blog post"""
    user, err = check_admin_auth()
    if err:
        return err
    
    blogs = load_blogs()
    for blog in blogs:
        if blog['id'] == blog_id:
            from datetime import datetime
            blog['status'] = 'published'
            blog['published_at'] = datetime.now().isoformat()
            blog['updated_at'] = datetime.now().isoformat()
            save_blogs(blogs)
            return jsonify({'success': True, 'blog': blog})
    
    return jsonify({'error': 'Blog not found'}), 404

@app.route('/api/admin/blogs/<blog_id>/unpublish', methods=['POST'])
def admin_unpublish_blog(blog_id):
    """Admin endpoint to unpublish a blog post (revert to draft)"""
    user, err = check_admin_auth()
    if err:
        return err
    
    blogs = load_blogs()
    for blog in blogs:
        if blog['id'] == blog_id:
            from datetime import datetime
            blog['status'] = 'draft'
            blog['published_at'] = None
            blog['updated_at'] = datetime.now().isoformat()
            save_blogs(blogs)
            return jsonify({'success': True, 'blog': blog})
    
    return jsonify({'error': 'Blog not found'}), 404

@app.route('/api/admin/blogs/<blog_id>', methods=['DELETE'])
def admin_delete_blog(blog_id):
    """Admin endpoint to delete a blog post"""
    user, err = check_admin_auth()
    if err:
        return err
    
    blogs = load_blogs()
    original_count = len(blogs)
    blogs = [b for b in blogs if b['id'] != blog_id]
    
    if len(blogs) == original_count:
        return jsonify({'error': 'Blog not found'}), 404
    
    save_blogs(blogs)
    return jsonify({'success': True})

def _run_blog_regeneration(job_id, blog_id, suggestion):
    """Background worker: runs Maker + Checker pipeline for blog regeneration."""
    try:
        BLOG_GENERATION_JOBS[job_id]['status'] = 'running'
        BLOG_GENERATION_JOBS[job_id]['phase'] = 'loading'
        
        blogs = load_blogs()
        target_blog = None
        for blog in blogs:
            if blog['id'] == blog_id:
                target_blog = blog
                break
        
        if not target_blog:
            BLOG_GENERATION_JOBS[job_id].update({'status': 'error', 'error': 'Blog not found'})
            return
        
        instructions = """Act as a seasoned, clear-headed, and investigative senior feature writer for a premium, new-age business publication (blend of 'The Ken' and 'YourStory'). You have access to real-time web search.

You previously wrote a blog post on the topic below. The reader has provided feedback. Your job is to REWRITE and IMPROVE the article, incorporating the feedback while maintaining a data-driven, narrative-driven editorial style.

Follow these editorial guidelines:
- Write with a TIGHT STORYLINE. Every paragraph must advance the narrative. The reader should feel pulled through: What happened → Why it matters → Who wins/loses → What comes next.
- Write so a LAYPERSON can understand. If you use any technical term or jargon, immediately explain it in plain language.
- Back every major claim with specific data from reputed sources: company filings, analyst reports (Goldman Sachs, Morgan Stanley, etc.), industry research (McKinsey, Gartner, CRISIL), government data.
- Include direct quotes from named CEOs, analysts, or domain experts.
- Include at least 5-8 specific data points from reputed sources.
- ZERO idioms, proverbs, or figurative cliches. Use PLAIN, DIRECT language.
- NO in-text citations or [1] markers — weave source attribution naturally.
- "Sources & References:" section at the very bottom with URLs.
- ZERO banned AI words: delve, tapestry, testament, landscape, beacon, robust, paramount, revolutionize, foster, "in conclusion".
- Vary sentence lengths. Use active voice and strong verbs.

OUTPUT FORMAT:
- Start with: {"title": "Updated Article Title"}
- Then ---
- Then clean HTML (h2, h3, p, ul/li, strong, em, blockquote — no html/head/body/h1)
- Sources & References section at the end."""

        user_input = f"Original topic: {target_blog['topic']}\n"
        if suggestion:
            user_input += f"\nReader feedback to incorporate: {suggestion}\n"
        user_input += f"\nPrevious article (first 800 chars for context): {target_blog['content_html'][:800]}..."
        user_input += "\n\nRewrite the article with these improvements."
        
        print(f"INFO: [Maker Phase] Regenerating blog draft for topic: {target_blog['topic']}", file=sys.stderr)
        BLOG_GENERATION_JOBS[job_id]['phase'] = 'maker'
        raw_response = call_openai_responses_api(instructions, user_input, use_web_search=True)
        
        # --- MAKER-CHECKER: CHECKER PHASE ---
        print(f"INFO: [Checker Phase] Sending regenerated draft to Gemini 3.1 Pro for editorial review...", file=sys.stderr)
        BLOG_GENERATION_JOBS[job_id]['phase'] = 'checker'
        
        checker_prompt = f"""You are the sharp, rigorous, and brilliant Investigative Head Editor for our premium business publication (think the absolute best editors at 'The Ken' or 'Bloomberg Businessweek'). 
You have Google Search grounding enabled. Use it to enhance or plug any gaps in the draft you receive.

One of your senior reporters has just submitted a REWRITTEN draft blog post on the topic: "{target_blog['topic']}".
The reader provided this specific feedback for the rewrite: "{suggestion}"

Here are the exact editorial guidelines the reporter was given to write this piece. You must understand the spirit, tone, narrative styling, and structure of what we want, and enforce it:
---
{instructions}
---

YOUR JOB:
1. Act as the checker/editor. You have the total power to edit, update, append, rewrite, or change any part of this blog.
2. Ensure the reader's feedback has been adequately addressed.
3. IDIOM & PROVERB PURGE: Scan the ENTIRE draft and REMOVE or REWRITE every single idiom, proverb, metaphor, or figurative cliche. Replace each one with a plain, direct statement backed by data. HOWEVER, PRESERVE any witty remarks, dry humor, offhand jokes, or clever observational asides — these add personality and are DIFFERENT from cliches.
4. DATA DENSITY CHECK: The article MUST contain at least 5-8 specific, sourced data points from reputed sources. If the draft is light on data, USE YOUR SEARCH GROUNDING to find and ADD real numbers, real quotes from named executives or analysts, and real research findings.
5. LAYPERSON CLARITY CHECK: Ensure every concept, term, or mechanism is immediately clear to a reader with NO business background. Add brief explanations where needed.
6. STORYLINE TIGHTNESS: The article must have a clear narrative thread. Every paragraph must connect to the next.
7. VERIFY facts, timeline, quotes, and research data using your search grounding. If anything is wrong, outdated, or weak, FIX IT.
8. Final Word Count: Limit the total word count of the final article to a maximum of 1800 words. Be concise and impactful.
9. Ensure structural rules:
   - NO in-text citations or footnotes (no [1] or hyperlink placeholders).
   - "Sources & References:" section at the very bottom with URLs.
   - ZERO banned AI words: delve, tapestry, testament, bustling, landscape, navigating the complexities, a symphony of, beacon, robust, dynamic, paramount, revolutionize, foster, 'in conclusion'.
   - ZERO idioms, proverbs, or figurative cliches.

OUTPUT FORMAT:
- First line MUST be a JSON object containing the finalized title AND a short topic label (max 10 words, tactical, concise, highlighting the key theme — NOT the full topic): {{"title": "The Final Masterpiece Title", "short_topic": "India's IT Services Crisis"}}
- Second line MUST be exactly three dashes: ---
- Then provide the incredibly clean, polished HTML (<h2>, <h3>, <p>, <ul>/<li>, <strong>, <em>, <blockquote>). Ensure the semantic HTML supports the narrative flow.
- DO NOT wrap the output in ```html blocks or include <html>, <head>, or <body> tags. Just the raw HTML content.

Here is the reporter's rewritten draft:
=========================================
{raw_response}
=========================================

Now, do your job as the Head Editor and output the finalized, publication-ready piece (under 2000 words)."""

        title = target_blog['title']
        content_html = raw_response
        short_topic = target_blog.get('short_topic', '')

        try:
            checker_response = call_gemini_api(
                messages=[{"role": "user", "content": checker_prompt}],
                model="gemini-3.1-pro-preview",
                temperature=1.0,
                use_google_search=True,
                thinking_level="HIGH"
            )
            print(f"INFO: [Checker Phase] Editorial review complete.", file=sys.stderr)
            
            # Clean potential markdown wrapping from Gemini
            if checker_response.startswith("```html"):
                checker_response = checker_response[7:]
            if checker_response.startswith("```"):
                checker_response = checker_response[3:]
            if checker_response.endswith("```"):
                checker_response = checker_response[:-3]
            
            checker_response = checker_response.strip()
            
            lines = checker_response.split('\n')
            for i, line in enumerate(lines):
                line_stripped = line.strip()
                if line_stripped.startswith('{') and '"title"' in line_stripped:
                    try:
                        title_data = json.loads(line_stripped)
                        title = title_data.get('title', title)
                        short_topic = title_data.get('short_topic', short_topic)
                        remaining = '\n'.join(lines[i+1:])
                        if '---' in remaining:
                            content_html = remaining.split('---', 1)[1].strip()
                        else:
                            content_html = remaining.strip()
                        break
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            print(f"ERROR: Checker phase failed: {e}. Falling back to Maker draft.", file=sys.stderr)
            lines = raw_response.split('\n')
            for i, line in enumerate(lines):
                line_stripped = line.strip()
                if line_stripped.startswith('{') and '"title"' in line_stripped:
                    try:
                        title_data = json.loads(line_stripped)
                        title = title_data.get('title', title)
                        short_topic = title_data.get('short_topic', short_topic)
                        remaining = '\n'.join(lines[i+1:])
                        if '---' in remaining:
                            content_html = remaining.split('---', 1)[1].strip()
                        else:
                            content_html = remaining.strip()
                        break
                    except json.JSONDecodeError:
                        pass
                        
        if content_html == raw_response:
            import re
            h_match = re.search(r'<h[12][^>]*>(.*?)</h[12]>', content_html)
            if h_match:
                title = re.sub(r'<[^>]+>', '', h_match.group(1)).strip()
                content_html = content_html.replace(h_match.group(0), '', 1).strip()
        
        BLOG_GENERATION_JOBS[job_id]['phase'] = 'saving'
        
        from datetime import datetime
        target_blog['title'] = title
        target_blog['content_html'] = content_html
        target_blog['short_topic'] = short_topic
        target_blog['updated_at'] = datetime.now().isoformat()
        
        save_blogs(blogs)
        
        print(f"INFO: Blog regenerated — id={blog_id}, title={title}", file=sys.stderr)
        
        BLOG_GENERATION_JOBS[job_id].update({
            'status': 'complete',
            'phase': 'done',
            'blog': target_blog
        })
    
    except Exception as e:
        print(f"ERROR: Blog regeneration failed: {e}", file=sys.stderr)
        traceback.print_exc()
        BLOG_GENERATION_JOBS[job_id].update({
            'status': 'error',
            'error': f'Regeneration failed: {str(e)}'
        })


@app.route('/api/admin/blogs/<blog_id>/regenerate', methods=['POST'])
def admin_regenerate_blog(blog_id):
    """Admin endpoint to regenerate a blog — kicks off async job and returns job_id."""
    user, err = check_admin_auth()
    if err:
        return err
    
    try:
        data = request.get_json(force=True)
        suggestion = data.get('suggestion', '').strip()
        
        # Quick validation: blog must exist
        blogs = load_blogs()
        target_blog = None
        for blog in blogs:
            if blog['id'] == blog_id:
                target_blog = blog
                break
        
        if not target_blog:
            return jsonify({'error': 'Blog not found'}), 404
        
        job_id = str(uuid.uuid4())[:8]
        BLOG_GENERATION_JOBS[job_id] = {
            'status': 'starting',
            'phase': 'queued',
            'topic': target_blog.get('topic', ''),
            'blog_id': blog_id,
            'type': 'regenerate'
        }
        
        # Start background thread
        t = threading.Thread(target=_run_blog_regeneration, args=(job_id, blog_id, suggestion), daemon=True)
        t.start()
        
        print(f"INFO: Blog regeneration job started — job_id={job_id}, blog_id={blog_id}", file=sys.stderr)
        return jsonify({'success': True, 'job_id': job_id})
    
    except Exception as e:
        print(f"ERROR: Failed to start blog regeneration: {e}", file=sys.stderr)
        traceback.print_exc()
        return jsonify({'error': f'Failed to start regeneration: {str(e)}'}), 500

# --- Public Blog API Endpoints ---

@app.route('/api/blogs', methods=['GET'])
def public_list_blogs():
    """Public endpoint to list published blog posts"""
    blogs = load_blogs()
    published = [
        {
            'id': b['id'],
            'title': b['title'],
            'slug': b['slug'],
            'topic': b['topic'],
            'article_type': b.get('article_type', 'general'),
            'short_topic': b.get('short_topic', ''),
            'excerpt': b['content_html'][:300].replace('<', ' ').replace('>', ' ').strip()[:200] + '...',
            'published_at': b['published_at'],
        }
        for b in blogs if b.get('status') == 'published'
    ]
    return jsonify({'blogs': published, 'count': len(published)})

@app.route('/api/blogs/<slug>', methods=['GET'])
def public_get_blog(slug):
    """Public endpoint to get a single published blog post"""
    blogs = load_blogs()
    for blog in blogs:
        if blog['slug'] == slug and blog.get('status') == 'published':
            return jsonify({
                'blog': {
                    'id': blog['id'],
                    'title': blog['title'],
                    'slug': blog['slug'],
                    'topic': blog['topic'],
                    'content_html': blog['content_html'],
                    'published_at': blog['published_at'],
                    'suggestions_count': len(blog.get('suggestions', []))
                }
            })
    return jsonify({'error': 'Blog not found'}), 404

@app.route('/api/blogs/<slug>/suggest', methods=['POST'])
def public_suggest_blog(slug):
    """Public endpoint to submit a suggestion for a blog post"""
    try:
        data = request.get_json(force=True)
        suggestion_text = data.get('suggestion', '').strip()
        
        if not suggestion_text:
            return jsonify({'error': 'Suggestion text is required'}), 400
        
        if len(suggestion_text) > 2000:
            return jsonify({'error': 'Suggestion too long (max 2000 characters)'}), 400
        
        blogs = load_blogs()
        for blog in blogs:
            if blog['slug'] == slug and blog.get('status') == 'published':
                from datetime import datetime
                if 'suggestions' not in blog:
                    blog['suggestions'] = []
                blog['suggestions'].append({
                    'text': suggestion_text,
                    'submitted_at': datetime.now().isoformat()
                })
                save_blogs(blogs)
                return jsonify({'success': True, 'message': 'Thank you for your suggestion!'})
        
        return jsonify({'error': 'Blog not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Blog page routes (public - no auth required)
@app.route('/blog')
def blog_listing_page():
    return send_from_directory('.', 'blog.html')

@app.route('/blog/<slug>')
def blog_detail_page(slug):
    return send_from_directory('.', 'blog.html')

# Login page (public)
@app.route('/login')
def login_page():
    return send_from_directory('.', 'login.html')

# Set password page (public)
@app.route('/set-password/<token>')
def set_password_page(token):
    return send_from_directory('.', 'set_password.html')

# Main app (protected - will be handled by session check in frontend)
@app.route('/app')
def app_page():
    from flask import session, redirect
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory('.', 'app.html')

# Admin panel (protected)
@app.route('/admin')
def admin_page():
    from flask import session, redirect
    if 'user_id' not in session:
        return redirect('/login')
    # Check if admin
    from auth.database import User
    user = User.get_by_id(session['user_id'])
    if not user or not user.is_admin:
        return redirect('/app')
    return send_from_directory('.', 'admin.html')

# Serve AI-Enhanced News page (protected)
@app.route('/news')
def news_page():
    from flask import session, redirect
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory('.', 'news.html')

# =====================================================================
# PORTFOLIO: Page Route + API Endpoints
# =====================================================================

from auth.database import Portfolio

@app.route('/portfolio')
def portfolio_page():
    """Serve portfolio page (protected)."""
    from flask import session as flask_session, redirect
    if 'user_id' not in flask_session:
        return redirect('/login')
    return send_from_directory('.', 'portfolio.html')


@app.route('/api/portfolio', methods=['GET'])
def api_portfolio_get():
    """Return all holdings for the logged-in user."""
    from flask import session as flask_session
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401
    holdings = Portfolio.get_by_user(user_id)
    return jsonify({'holdings': [h.to_dict() for h in holdings]})


@app.route('/api/portfolio/add', methods=['POST'])
def api_portfolio_add():
    """Add a single holding. Auto-fetches sector/industry from yfinance."""
    from flask import session as flask_session
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    data = request.get_json(force=True)
    ticker = (data.get('ticker') or '').strip().upper()
    stock_name = (data.get('stock_name') or '').strip()
    quantity = data.get('quantity')
    avg_buy_price = data.get('avg_buy_price')
    buy_date = (data.get('buy_date') or '').strip()

    if not ticker or quantity is None or avg_buy_price is None:
        return jsonify({'error': 'ticker, quantity, and avg_buy_price are required'}), 400

    try:
        quantity = float(quantity)
        avg_buy_price = float(avg_buy_price)
    except (ValueError, TypeError):
        return jsonify({'error': 'quantity and avg_buy_price must be numbers'}), 400

    if quantity <= 0 or avg_buy_price <= 0:
        return jsonify({'error': 'quantity and avg_buy_price must be positive'}), 400

    # Auto-detect stock name from STOCKS_LIST if not provided
    if not stock_name:
        for s in STOCKS_LIST:
            if s['ticker'].upper() == ticker:
                stock_name = s['name']
                break
        if not stock_name:
            stock_name = ticker

    # Fetch sector/industry from yfinance (best-effort, non-blocking)
    sector = ''
    industry = ''
    try:
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        info = yf_ticker.info or {}
        sector = info.get('sector', '') or ''
        industry = info.get('industry', '') or ''
    except Exception as e:
        print(f"WARN: Could not fetch sector for {ticker}: {e}")

    Portfolio.add_holding(user_id, ticker, stock_name, quantity, avg_buy_price, sector, industry, buy_date)
    return jsonify({'success': True, 'ticker': ticker, 'stock_name': stock_name, 'sector': sector, 'industry': industry})


@app.route('/api/portfolio/update', methods=['PUT'])
def api_portfolio_update():
    """Update quantity and avg_buy_price for a holding."""
    from flask import session as flask_session
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    data = request.get_json(force=True)
    ticker = (data.get('ticker') or '').strip().upper()
    quantity = data.get('quantity')
    avg_buy_price = data.get('avg_buy_price')
    buy_date = data.get('buy_date')  # Can be None (don't update) or '' or 'YYYY-MM-DD'

    if not ticker or quantity is None or avg_buy_price is None:
        return jsonify({'error': 'ticker, quantity, and avg_buy_price are required'}), 400

    try:
        quantity = float(quantity)
        avg_buy_price = float(avg_buy_price)
    except (ValueError, TypeError):
        return jsonify({'error': 'quantity and avg_buy_price must be numbers'}), 400

    updated = Portfolio.update_holding(user_id, ticker, quantity, avg_buy_price, buy_date)
    if not updated:
        return jsonify({'error': f'{ticker} not found in portfolio'}), 404
    return jsonify({'success': True})


@app.route('/api/stock/price-on-date', methods=['GET'])
def api_stock_price_on_date():
    """Fetch the close price of a stock on a specific date."""
    ticker = request.args.get('ticker')
    date_str = request.args.get('date')
    if not ticker or not date_str:
        return jsonify({'error': 'ticker and date required'}), 400
    try:
        from datetime import datetime, timedelta
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        # fetch 5-day window to catch weekends/holidays
        end_dt = dt + timedelta(days=5)
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(start=dt.strftime('%Y-%m-%d'), end=end_dt.strftime('%Y-%m-%d'))
        if not hist.empty:
            price = float(hist['Close'].iloc[0])
            return jsonify({'price': price, 'date_used': hist.index[0].strftime('%Y-%m-%d')})
        return jsonify({'error': 'No price data found for date'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/portfolio/what-if', methods=['POST'])
def api_portfolio_what_if():
    """Simulate swapping one holding for another and show portfolio impact."""
    from flask import session as flask_session
    from datetime import datetime, timedelta
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    data = request.get_json(force=True)
    sell_ticker = (data.get('sell_ticker') or '').strip().upper()
    buy_ticker = (data.get('buy_ticker') or '').strip().upper()

    if not sell_ticker or not buy_ticker:
        return jsonify({'error': 'sell_ticker and buy_ticker are required'}), 400

    holdings = Portfolio.get_by_user(user_id)
    if not holdings:
        return jsonify({'error': 'No holdings found'}), 400

    # Find the sell holding
    sell_holding = None
    for h in holdings:
        if h.ticker == sell_ticker:
            sell_holding = h
            break

    if not sell_holding:
        return jsonify({'error': f'{sell_ticker} not in portfolio'}), 404

    try:
        # --- Fetch current prices ---
        sell_yf = yf.Ticker(f"{sell_ticker}.NS")
        sell_info = sell_yf.info or {}
        sell_price = sell_info.get('currentPrice') or sell_info.get('regularMarketPrice') or sell_info.get('previousClose')

        buy_yf = yf.Ticker(f"{buy_ticker}.NS")
        buy_info = buy_yf.info or {}
        buy_price = buy_info.get('currentPrice') or buy_info.get('regularMarketPrice') or buy_info.get('previousClose')

        if not sell_price or not buy_price:
            return jsonify({'error': 'Could not fetch current prices. Try again.'}), 400

        # --- Swap Details ---
        capital_freed = sell_holding.quantity * sell_price
        buy_quantity = int(capital_freed / buy_price)  # whole shares
        buy_industry = buy_info.get('industry', '') or ''
        buy_name = buy_info.get('shortName', buy_ticker) or buy_ticker

        # Look up buy stock name from STOCKS_LIST if available
        for s in STOCKS_LIST:
            if s['ticker'].upper() == buy_ticker:
                buy_name = s['name']
                break

        sell_details = {
            'name': sell_holding.stock_name,
            'ticker': sell_ticker,
            'quantity': sell_holding.quantity,
            'current_price': round(sell_price, 2),
            'capital_freed': round(capital_freed, 2)
        }
        buy_details = {
            'name': buy_name,
            'ticker': buy_ticker,
            'quantity': buy_quantity,
            'current_price': round(buy_price, 2),
            'industry': buy_industry
        }

        # --- Diversification Score (HHI-based) ---
        def calc_diversification(holdings_list):
            """Calculate diversification score from 0-10 using HHI."""
            # Group by industry
            industry_values = {}
            total_value = 0
            for h_item in holdings_list:
                ind = h_item.get('industry', '') or 'Unknown'
                val = h_item.get('value', 0)
                industry_values[ind] = industry_values.get(ind, 0) + val
                total_value += val

            if total_value == 0:
                return {'diversification_score': 5.0, 'hhi': 0, 'top_industry': 'N/A', 'top_weight': 0}

            # HHI = sum of squared weights (0 to 1, lower is more diversified)
            hhi = sum((v / total_value) ** 2 for v in industry_values.values())

            # Convert to 0-10 score (10 = perfectly diversified)
            # Perfect concentration = HHI of 1.0 → score 0
            # Perfect diversification = HHI of 1/n → score ~10
            n = len(industry_values)
            if n <= 1:
                score = 1.0
            else:
                min_hhi = 1.0 / n
                score = max(0, min(10, 10 * (1 - hhi) / (1 - min_hhi)))

            top_ind = max(industry_values, key=industry_values.get)
            top_weight = round(100 * industry_values[top_ind] / total_value, 1)

            return {
                'diversification_score': round(score, 1),
                'hhi': round(hhi, 4),
                'top_industry': top_ind,
                'top_weight': top_weight
            }

        # Current portfolio values
        current_holdings_vals = []
        for h in holdings:
            try:
                h_yf = yf.Ticker(f"{h.ticker}.NS")
                h_info = h_yf.info or {}
                h_price = h_info.get('currentPrice') or h_info.get('regularMarketPrice') or h_info.get('previousClose') or h.avg_buy_price
            except Exception:
                h_price = h.avg_buy_price
            current_holdings_vals.append({
                'ticker': h.ticker,
                'industry': h.industry or 'Unknown',
                'value': h.quantity * h_price
            })

        # New portfolio values (swap applied)
        new_holdings_vals = []
        for item in current_holdings_vals:
            if item['ticker'] == sell_ticker:
                # Replace with buy stock
                new_holdings_vals.append({
                    'ticker': buy_ticker,
                    'industry': buy_industry or 'Unknown',
                    'value': buy_quantity * buy_price
                })
            else:
                new_holdings_vals.append(item)

        current_concentration = calc_diversification(current_holdings_vals)
        new_concentration = calc_diversification(new_holdings_vals)

        # --- Backtest Delta ---
        backtest_delta_pct = None
        try:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=180)
            
            if sell_holding.buy_date:
                try:
                    parsed_date = datetime.strptime(sell_holding.buy_date, '%Y-%m-%d')
                    if parsed_date < end_date:
                        start_date = parsed_date
                except ValueError:
                    pass
            
            start_str = start_date.strftime('%Y-%m-%d')
            end_str = end_date.strftime('%Y-%m-%d')

            sell_hist = sell_yf.history(start=start_str, end=end_str)
            buy_hist = buy_yf.history(start=start_str, end=end_str)

            if len(sell_hist) > 0 and len(buy_hist) > 0:
                # Simulation basis: Original invested value and stock price on buying date
                initial_capital = sell_holding.quantity * sell_holding.avg_buy_price
                buy_start_price = buy_hist['Close'].iloc[0]
                
                # Simulated buy quantity at start date
                simulated_buy_qty = initial_capital / buy_start_price
                
                # Portfolio value if sold today
                sell_current_value = sell_hist['Close'].iloc[-1] * sell_holding.quantity
                # Portfolio value if swapped then
                buy_current_value = buy_hist['Close'].iloc[-1] * simulated_buy_qty
                
                # Returns based on original capital
                sell_return_pct = (sell_current_value / initial_capital - 1) * 100
                buy_return_pct = (buy_current_value / initial_capital - 1) * 100
                
                backtest_delta_pct = round(buy_return_pct - sell_return_pct, 2)
        except Exception as e:
            print(f"WHAT-IF: Backtest calc failed: {e}")

        # --- New Industry Allocation ---
        new_industry_data = {}
        for item in new_holdings_vals:
            ind = item['industry'] or 'Unknown'
            new_industry_data[ind] = new_industry_data.get(ind, 0) + item['value']
        # Round values
        new_industry_data = {k: round(v, 0) for k, v in new_industry_data.items()}

        return jsonify({
            'sell_details': sell_details,
            'buy_details': buy_details,
            'current_concentration': current_concentration,
            'new_concentration': new_concentration,
            'backtest_delta_pct': backtest_delta_pct,
            'new_industry_data': new_industry_data
        })

    except Exception as e:
        print(f"WHAT-IF ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Simulation failed: {str(e)}'}), 500

@app.route('/api/portfolio/<ticker>', methods=['DELETE'])
def api_portfolio_delete(ticker):
    """Remove a holding from the portfolio."""
    from flask import session as flask_session
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    deleted = Portfolio.delete_holding(user_id, ticker.upper())
    if not deleted:
        return jsonify({'error': f'{ticker} not found in portfolio'}), 404
    return jsonify({'success': True})


@app.route('/api/portfolio/upload-csv', methods=['POST'])
def api_portfolio_upload_csv():
    """Parse a CSV and bulk-add holdings. Expects columns: Ticker (or Stock Name), Quantity, Avg Buy Price."""
    from flask import session as flask_session
    import io
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    file = request.files.get('file')
    if not file:
        return jsonify({'error': 'No file uploaded'}), 400

    try:
        content = file.read().decode('utf-8')
        reader = csv.DictReader(io.StringIO(content))

        # Build ticker lookup from STOCKS_LIST
        ticker_lookup = {s['ticker'].upper(): s['name'] for s in STOCKS_LIST}
        name_to_ticker = {s['name'].upper(): s['ticker'].upper() for s in STOCKS_LIST}

        added = []
        errors = []

        for i, row in enumerate(reader, start=2):  # start=2 because row 1 is header
            # Normalize column names (case-insensitive)
            norm_row = {k.strip().lower(): v.strip() for k, v in row.items() if k and v}

            # Extract ticker — try 'ticker' column first, then 'stock name'
            ticker = norm_row.get('ticker', '').upper()
            stock_name_input = norm_row.get('stock name', '') or norm_row.get('stock_name', '') or norm_row.get('name', '')

            if not ticker and stock_name_input:
                # Try to resolve name to ticker
                resolved = name_to_ticker.get(stock_name_input.upper(), '')
                if resolved:
                    ticker = resolved

            if not ticker:
                errors.append(f"Row {i}: Could not determine ticker")
                continue

            # Validate ticker exists
            if ticker not in ticker_lookup:
                errors.append(f"Row {i}: Unknown ticker '{ticker}'")
                continue

            # Parse quantity and price
            qty_str = norm_row.get('quantity', '') or norm_row.get('qty', '')
            price_str = norm_row.get('avg buy price', '') or norm_row.get('avg_buy_price', '') or norm_row.get('price', '') or norm_row.get('buy price', '') or norm_row.get('average price', '')

            try:
                qty = float(qty_str.replace(',', ''))
                price = float(price_str.replace(',', ''))
            except (ValueError, TypeError):
                errors.append(f"Row {i}: Invalid quantity or price for {ticker}")
                continue

            if qty <= 0 or price <= 0:
                errors.append(f"Row {i}: Quantity and price must be positive for {ticker}")
                continue

            stock_name = ticker_lookup.get(ticker, ticker)

            # Parse buy_date (optional column)
            buy_date = norm_row.get('buy date', '') or norm_row.get('buy_date', '') or norm_row.get('purchase date', '') or ''

            # Fetch sector (best-effort)
            sector = ''
            industry = ''
            try:
                yf_ticker_obj = yf.Ticker(f"{ticker}.NS")
                info = yf_ticker_obj.info or {}
                sector = info.get('sector', '') or ''
                industry = info.get('industry', '') or ''
            except Exception:
                pass

            Portfolio.add_holding(user_id, ticker, stock_name, qty, price, sector, industry, buy_date)
            added.append(ticker)

        return jsonify({
            'success': True,
            'added_count': len(added),
            'added_tickers': added,
            'errors': errors
        })

    except Exception as e:
        print(f"ERROR: CSV upload failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'CSV parsing failed: {str(e)}'}), 500


@app.route('/api/portfolio/metrics', methods=['GET'])
def api_portfolio_metrics():
    """Compute portfolio-level PM metrics: Beta, Alpha, Diversification, NIFTY comparison."""
    from flask import session as flask_session
    from datetime import datetime, timedelta
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    holdings = Portfolio.get_by_user(user_id)
    if not holdings:
        return jsonify({'error': 'No holdings'}), 400

    try:
        # --- Gather current prices and values ---
        total_value = 0
        holding_data = []
        for h in holdings:
            try:
                h_yf = yf.Ticker(f"{h.ticker}.NS")
                h_info = h_yf.info or {}
                h_price = h_info.get('currentPrice') or h_info.get('regularMarketPrice') or h_info.get('previousClose') or h.avg_buy_price
                h_beta = h_info.get('beta', None)
            except Exception:
                h_price = h.avg_buy_price
                h_beta = None

            value = h.quantity * h_price
            total_value += value
            holding_data.append({
                'ticker': h.ticker,
                'industry': h.industry or 'Unknown',
                'value': value,
                'beta': h_beta,
                'buy_date': h.buy_date or '',
                'avg_buy_price': h.avg_buy_price,
                'current_price': h_price,
                'quantity': h.quantity
            })

        # --- Portfolio Beta (value-weighted average) ---
        portfolio_beta = None
        weighted_beta_sum = 0
        beta_value_sum = 0
        for hd in holding_data:
            if hd['beta'] is not None and total_value > 0:
                weight = hd['value'] / total_value
                weighted_beta_sum += hd['beta'] * weight
                beta_value_sum += hd['value']
        if beta_value_sum > 0:
            portfolio_beta = round(weighted_beta_sum, 2)

        # --- Diversification Score (HHI-based) ---
        industry_values = {}
        for hd in holding_data:
            ind = hd['industry']
            industry_values[ind] = industry_values.get(ind, 0) + hd['value']

        hhi = sum((v / total_value) ** 2 for v in industry_values.values()) if total_value > 0 else 1
        n_industries = len(industry_values)
        if n_industries <= 1:
            div_score = 1.0
        else:
            min_hhi = 1.0 / n_industries
            div_score = max(0, min(10, 10 * (1 - hhi) / (1 - min_hhi)))
        div_score = round(div_score, 1)

        # --- Portfolio vs NIFTY & Alpha (requires buy_dates) ---
        portfolio_return_pct = None
        nifty_return_pct = None
        alpha = None
        has_buy_dates = any(hd['buy_date'] for hd in holding_data)

        if has_buy_dates:
            try:
                # Compute weighted portfolio return based on buy dates
                total_invested = 0
                total_current = 0
                for hd in holding_data:
                    invested = hd['quantity'] * hd['avg_buy_price']
                    current = hd['value']
                    total_invested += invested
                    total_current += current

                if total_invested > 0:
                    portfolio_return_pct = round(((total_current / total_invested) - 1) * 100, 2)

                # Find earliest buy date for NIFTY comparison
                buy_dates = []
                for hd in holding_data:
                    if hd['buy_date']:
                        try:
                            buy_dates.append(datetime.strptime(hd['buy_date'], '%Y-%m-%d'))
                        except Exception:
                            pass

                if buy_dates:
                    earliest_date = min(buy_dates)
                    nifty = yf.Ticker('^NSEI')
                    nifty_hist = nifty.history(start=earliest_date.strftime('%Y-%m-%d'), end=datetime.now().strftime('%Y-%m-%d'))

                    if len(nifty_hist) > 5:
                        nifty_start = nifty_hist['Close'].iloc[0]
                        nifty_end = nifty_hist['Close'].iloc[-1]
                        nifty_return_pct = round(((nifty_end / nifty_start) - 1) * 100, 2)

                        # Alpha = Portfolio Return - (Risk Free Rate + Beta * (Market Return - Risk Free Rate))
                        # Using simple alpha: Portfolio Return - NIFTY Return (for intuitive PM use)
                        if portfolio_return_pct is not None and nifty_return_pct is not None:
                            if portfolio_beta is not None:
                                # CAPM Alpha = Rp - [Rf + β(Rm - Rf)] where Rf ≈ 7% annually (India FD rate)
                                # Annualize if needed, but for simplicity use period returns
                                risk_free = 7.0  # approximate Indian risk-free rate
                                # Scale to period
                                days_held = (datetime.now() - earliest_date).days
                                if days_held > 0:
                                    rf_period = risk_free * days_held / 365
                                    expected_return = rf_period + portfolio_beta * (nifty_return_pct - rf_period)
                                    alpha = round(portfolio_return_pct - expected_return, 2)
                            else:
                                alpha = round(portfolio_return_pct - nifty_return_pct, 2)

            except Exception as e:
                print(f"METRICS: Alpha/NIFTY calc failed: {e}")

        return jsonify({
            'portfolio_beta': portfolio_beta,
            'diversification_score': div_score,
            'hhi': round(hhi, 4),
            'n_industries': n_industries,
            'portfolio_return_pct': portfolio_return_pct,
            'nifty_return_pct': nifty_return_pct,
            'alpha': alpha,
            'has_buy_dates': has_buy_dates,
            'total_value': round(total_value, 0)
        })

    except Exception as e:
        print(f"METRICS ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Metrics failed: {str(e)}'}), 500

@app.route('/api/portfolio/performance-chart', methods=['GET'])
def api_portfolio_performance_chart():
    """Return daily portfolio cumulative returns vs NIFTY 50 for charting."""
    from flask import session as flask_session
    from datetime import datetime, timedelta
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    holdings = Portfolio.get_by_user(user_id)
    if not holdings:
        return jsonify({'dates': [], 'portfolio_returns': [], 'nifty_returns': []})

    # Check for buy dates
    has_buy_dates = any(h.buy_date for h in holdings)
    if not has_buy_dates:
        return jsonify({'dates': [], 'portfolio_returns': [], 'nifty_returns': []})

    timeframe = request.args.get('timeframe', '3M')
    end_date = datetime.now()
    tf_map = {'1W': 7, '1M': 30, '3M': 90, '6M': 180, '1Y': 365}
    days = tf_map.get(timeframe, 90)
    start_date = end_date - timedelta(days=days)

    try:
        import pandas as pd

        # 1. Fetch NIFTY 50 history
        nifty = yf.Ticker('^NSEI')
        nifty_hist = nifty.history(start=start_date.strftime('%Y-%m-%d'), end=end_date.strftime('%Y-%m-%d'))
        if nifty_hist.empty or len(nifty_hist) < 2:
            return jsonify({'dates': [], 'portfolio_returns': [], 'nifty_returns': []})

        # Create date index from NIFTY (trading days)
        trading_dates = nifty_hist.index

        # 2. Compute daily portfolio value
        # For each holding, fetch its history. If buy_date is after start_date, only include from buy_date.
        # Portfolio value on each day = sum of (quantity * close_price) for all holdings held on that day.
        portfolio_daily = pd.Series(0.0, index=trading_dates)
        initial_capital = 0

        for h in holdings:
            try:
                yf_t = yf.Ticker(f"{h.ticker}.NS")
                hist = yf_t.history(start=start_date.strftime('%Y-%m-%d'), end=end_date.strftime('%Y-%m-%d'))
                if hist.empty:
                    continue

                # Reindex to match NIFTY trading days, forward-fill missing
                hist = hist.reindex(trading_dates, method='ffill')

                # Contribution of this holding
                holding_values = hist['Close'] * h.quantity
                holding_values = holding_values.fillna(0)
                portfolio_daily = portfolio_daily.add(holding_values, fill_value=0)

                # Initial capital contribution (invested amount)
                initial_capital += h.quantity * h.avg_buy_price
            except Exception as e:
                print(f"PERF-CHART: Error fetching {h.ticker}: {e}")
                # Fallback: use avg_buy_price * quantity as a constant
                initial_capital += h.quantity * h.avg_buy_price

        if initial_capital == 0 or portfolio_daily.sum() == 0:
            return jsonify({'dates': [], 'portfolio_returns': [], 'nifty_returns': []})

        # 3. Convert to cumulative returns (%)
        # Portfolio: return vs initial capital
        portfolio_returns = ((portfolio_daily / initial_capital) - 1) * 100

        # NIFTY: return vs its starting value
        nifty_start = nifty_hist['Close'].iloc[0]
        nifty_returns = ((nifty_hist['Close'] / nifty_start) - 1) * 100

        # 4. Format output
        dates_str = [d.strftime('%Y-%m-%d') for d in portfolio_returns.index]
        port_vals = [round(float(v), 2) for v in portfolio_returns.values]
        nifty_vals = [round(float(v), 2) for v in nifty_returns.reindex(portfolio_returns.index, method='ffill').values]

        return jsonify({
            'dates': dates_str,
            'portfolio_returns': port_vals,
            'nifty_returns': nifty_vals
        })

    except Exception as e:
        print(f"PERF-CHART ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/portfolio/dashboard', methods=['GET'])
def api_portfolio_dashboard():
    """
    Return complete dashboard data: P&L, sector weights, technical health for all holdings.
    This is the main data source for the portfolio dashboard UI.
    """
    from flask import session as flask_session
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    holdings = Portfolio.get_by_user(user_id)
    if not holdings:
        return jsonify({'holdings': [], 'summary': {}, 'sector_data': [], 'technical_health': []})

    enriched_holdings = []
    industry_map = {}  # industry -> total current value
    total_invested = 0
    total_current = 0
    technical_health = []

    for h in holdings:
        current_price = None
        day_change_pct = 0

        # Fetch current price from yfinance
        try:
            yf_ticker = yf.Ticker(f"{h.ticker}.NS")
            hist = yf_ticker.history(period="2d")
            if hist is not None and not hist.empty:
                current_price = float(hist['Close'].iloc[-1])
                if len(hist) >= 2:
                    prev_close = float(hist['Close'].iloc[-2])
                    if prev_close > 0:
                        day_change_pct = round(((current_price - prev_close) / prev_close) * 100, 2)
        except Exception as e:
            print(f"WARN: Price fetch failed for {h.ticker}: {e}")

        invested = h.quantity * h.avg_buy_price
        current_val = h.quantity * current_price if current_price else invested
        pnl = current_val - invested
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0

        total_invested += invested
        total_current += current_val

        # Accumulate industry data (more granular than sector)
        ind = h.industry or 'Unknown'
        industry_map[ind] = industry_map.get(ind, 0) + current_val

        enriched_holdings.append({
            'ticker': h.ticker,
            'stock_name': h.stock_name,
            'quantity': h.quantity,
            'avg_buy_price': round(h.avg_buy_price, 2),
            'current_price': round(current_price, 2) if current_price else None,
            'invested': round(invested, 2),
            'current_value': round(current_val, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2),
            'day_change_pct': day_change_pct,
            'sector': h.sector,
            'industry': h.industry,
            'buy_date': h.buy_date
        })

        # Technical health — full analysis via generate_summary() from tech_calculations
        try:
            tech_res = evaluate_ticker_signal(h.ticker, interval='daily')
            if tech_res and tech_res.get('Signal') not in ['NO DATA', 'INSUFFICIENT DATA']:
                # Use generate_summary() to extract all FM-grade indicators in one pass
                summary_rows = generate_summary(tech_res)
                summary_dict = {row['key']: row['value'] for row in summary_rows}

                pa = summary_dict.get('Price-Action Trend (based on Close prices)', 'N/A')
                ms = summary_dict.get('Market Structure (based on EMA Stack)', 'N/A')

                # Custom Signal Logic (PM-grade):
                # HOLD if price action is Uptrend
                # HOLD if price action is Sideways AND market structure is Uptrend or Mild Uptrend
                # SELL in all other cases
                if 'Uptrend' in pa:
                    signal = 'HOLD'
                elif 'Sideways' in pa and ('Uptrend' in ms or 'Mild Uptrend' in ms):
                    signal = 'HOLD'
                else:
                    signal = 'SELL'

                health_entry = {
                    'ticker': h.ticker,
                    'stock_name': h.stock_name,
                    'signal': signal,
                    'price_action': pa,
                    'fib_strength': summary_dict.get('Trend Strength (based on Fibonacci retracement)', 'N/A'),
                    'market_structure': ms,
                    'sentiment': summary_dict.get('Market Sentiment (based on RSI)', 'N/A'),
                    'rsi_divergence': summary_dict.get('Hidden Trend Divergence (based on RSI)', 'N/A'),
                    'relative_strength': summary_dict.get('Relative Strength vs Nifty', 'N/A'),
                    'volume': summary_dict.get('Accumulating or Distributing (based on Volume)', 'N/A'),
                    'support': summary_dict.get('Support Zone', None),
                    'resistance': summary_dict.get('Resistance Zone', None)
                }
                technical_health.append(health_entry)
            else:
                technical_health.append({
                    'ticker': h.ticker, 'stock_name': h.stock_name, 'signal': 'N/A',
                    'price_action': 'N/A', 'fib_strength': 'N/A', 'market_structure': 'N/A',
                    'sentiment': 'N/A', 'rsi_divergence': 'N/A', 'relative_strength': 'N/A',
                    'volume': 'N/A', 'support': None, 'resistance': None
                })
        except Exception as e:
            print(f"WARN: Tech signal failed for {h.ticker}: {e}")
            technical_health.append({
                'ticker': h.ticker, 'stock_name': h.stock_name, 'signal': 'N/A',
                'price_action': 'N/A', 'fib_strength': 'N/A', 'market_structure': 'N/A',
                'sentiment': 'N/A', 'rsi_divergence': 'N/A', 'relative_strength': 'N/A',
                'volume': 'N/A', 'support': None, 'resistance': None
            })

    total_pnl = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    # Industry data for treemap
    industry_data = [{'industry': ind, 'value': round(val, 2)} for ind, val in sorted(industry_map.items(), key=lambda x: -x[1])]

    # Herfindahl Index for concentration risk (0 to 1, 1 = fully concentrated)
    hhi = 0
    if total_current > 0:
        for h in enriched_holdings:
            weight = h['current_value'] / total_current
            hhi += weight * weight
    hhi = round(hhi, 4)

    # Concentration interpretation
    if hhi > 0.25:
        concentration_level = 'High'
    elif hhi > 0.15:
        concentration_level = 'Moderate'
    else:
        concentration_level = 'Low'

    return jsonify(sanitize_for_json({
        'holdings': enriched_holdings,
        'summary': {
            'total_invested': round(total_invested, 2),
            'total_current': round(total_current, 2),
            'total_pnl': round(total_pnl, 2),
            'total_pnl_pct': round(total_pnl_pct, 2),
            'holding_count': len(enriched_holdings)
        },
        'industry_data': industry_data,
        'technical_health': technical_health,
        'concentration': {
            'hhi': hhi,
            'level': concentration_level,
            'diversification_score': round((1 - hhi) * 10, 1)
        }
    }))


# =====================================================================
# PORTFOLIO BRIEF ENGINE (Post-Market & Pre-Market)
# =====================================================================

LOCAL_BRIEF_CACHE = {}  # { "type:user_id:date": { "data": {...}, "timestamp": ..., "expires": ... } }
BRIEF_REDIS_PREFIX = "portfolio_brief:"

def get_brief_cache(brief_type, user_id, date_str):
    """Get cached brief from local memory or Redis."""
    cache_key = f"{brief_type}:{user_id}:{date_str}"

    if cache_key in LOCAL_BRIEF_CACHE:
        entry = LOCAL_BRIEF_CACHE[cache_key]
        if time.time() < entry.get("expires", 0):
            return entry.get("data")
        else:
            del LOCAL_BRIEF_CACHE[cache_key]

    if DIRECT_REDIS_CLIENT:
        try:
            raw = DIRECT_REDIS_CLIENT.get(f"{BRIEF_REDIS_PREFIX}{cache_key}")
            if raw:
                data = pickle.loads(zlib.decompress(raw))
                # Determine remaining TTL from Redis
                ttl = DIRECT_REDIS_CLIENT.ttl(f"{BRIEF_REDIS_PREFIX}{cache_key}")
                if ttl and ttl > 0:
                    LOCAL_BRIEF_CACHE[cache_key] = {"data": data, "timestamp": time.time(), "expires": time.time() + ttl}
                    return data
        except Exception as e:
            print(f"WARN: Redis brief cache read failed: {e}", file=sys.stderr)

    return None

def set_brief_cache(brief_type, user_id, date_str, data, ttl_seconds):
    """Save brief to local memory and Redis with specific TTL."""
    cache_key = f"{brief_type}:{user_id}:{date_str}"
    LOCAL_BRIEF_CACHE[cache_key] = {"data": data, "timestamp": time.time(), "expires": time.time() + ttl_seconds}

    if DIRECT_REDIS_CLIENT:
        try:
            compressed = zlib.compress(pickle.dumps(data))
            DIRECT_REDIS_CLIENT.setex(
                f"{BRIEF_REDIS_PREFIX}{cache_key}",
                int(ttl_seconds),
                compressed
            )
        except Exception as e:
            print(f"WARN: Redis brief cache write failed: {e}", file=sys.stderr)

def clear_brief_cache(brief_type, user_id, date_str):
    """Clear a specific brief from cache."""
    cache_key = f"{brief_type}:{user_id}:{date_str}"
    LOCAL_BRIEF_CACHE.pop(cache_key, None)
    if DIRECT_REDIS_CLIENT:
        try:
            DIRECT_REDIS_CLIENT.delete(f"{BRIEF_REDIS_PREFIX}{cache_key}")
        except Exception:
            pass


# --- Candlestick Pattern Detection ---
def detect_candlestick_patterns(df):
    """Detect candlestick patterns on the last candle using TA-Lib CDL* functions."""
    if df is None or len(df) < 5:
        return []

    o = df['Open'].values.astype(float)
    h = df['High'].values.astype(float)
    l = df['Low'].values.astype(float)
    c = df['Close'].values.astype(float)

    patterns_map = {
        'Doji': talib.CDLDOJI,
        'Hammer': talib.CDLHAMMER,
        'Inverted Hammer': talib.CDLINVERTEDHAMMER,
        'Engulfing': talib.CDLENGULFING,
        'Morning Star': talib.CDLMORNINGSTAR,
        'Evening Star': talib.CDLEVENINGSTAR,
        'Marubozu': talib.CDLMARUBOZU,
        'Spinning Top': talib.CDLSPINNINGTOP,
        'Harami': talib.CDLHARAMI,
        'Three White Soldiers': talib.CDL3WHITESOLDIERS,
        'Three Black Crows': talib.CDL3BLACKCROWS,
        'Shooting Star': talib.CDLSHOOTINGSTAR,
        'Hanging Man': talib.CDLHANGINGMAN,
    }

    detected = []
    for name, func in patterns_map.items():
        try:
            result = func(o, h, l, c)
            if len(result) > 0 and result[-1] != 0:
                direction = 'Bullish' if result[-1] > 0 else 'Bearish'
                detected.append(f"{direction} {name}")
        except Exception:
            pass

    return detected


# --- Index Data Fetching ---
def fetch_index_data():
    """
    Fetch NIFTY 50 via yfinance (reliable, no WebSocket dependency).
    Fetch GIFT Nifty via tvDatafeed (best-effort, non-blocking).
    Returns dict with close, prev_close, change_pct for both indices.
    """
    import yfinance as yf
    result = {
        'nifty_close': None, 'nifty_prev_close': None, 'nifty_change_pct': None,
        'gift_nifty_last': None, 'gift_nifty_change_pct': None
    }

    # --- NIFTY 50 via yfinance (^NSEI) — always works ---
    try:
        nifty = yf.download('^NSEI', period='5d', interval='1d', auto_adjust=False, progress=False)
        if nifty is not None and len(nifty) >= 2:
            result['nifty_close'] = round(float(nifty['Close'].iloc[-1]), 2)
            result['nifty_prev_close'] = round(float(nifty['Close'].iloc[-2]), 2)
            if result['nifty_prev_close'] > 0:
                result['nifty_change_pct'] = round(
                    ((result['nifty_close'] - result['nifty_prev_close']) / result['nifty_prev_close']) * 100, 2
                )
        print(f"BRIEF: NIFTY data fetched via yfinance — Close: {result['nifty_close']}, Chg: {result['nifty_change_pct']}%", file=sys.stderr)
    except Exception as e:
        print(f"BRIEF: NIFTY data fetch failed: {e}", file=sys.stderr)

    # --- GIFT Nifty via tvDatafeed (best-effort, non-blocking) ---
    try:
        from tech_calculations import tv as tv_global
        from tvDatafeed import Interval
        gift_df = None
        for attempt in range(2):  # Only 2 attempts, quick timeout
            try:
                gift_df = tv_global.get_hist('NIFTY1!', 'NSEIX', Interval.in_daily, n_bars=5)
                if gift_df is not None and not gift_df.empty:
                    break
            except Exception:
                time.sleep(0.5)
        if gift_df is not None and len(gift_df) >= 2:
            result['gift_nifty_last'] = round(float(gift_df['close'].iloc[-1]), 2)
            prev = float(gift_df['close'].iloc[-2])
            if prev > 0:
                result['gift_nifty_change_pct'] = round(
                    ((result['gift_nifty_last'] - prev) / prev) * 100, 2
                )
            print(f"BRIEF: GIFT Nifty fetched — Last: {result['gift_nifty_last']}", file=sys.stderr)
        else:
            print("BRIEF: GIFT Nifty unavailable (tvDatafeed down) — skipping, non-critical", file=sys.stderr)
    except Exception as e:
        print(f"BRIEF: GIFT Nifty fetch failed (non-critical): {e}", file=sys.stderr)

    return result


# --- Parallel Per-Stock Data Gathering ---
def _gather_stock_data_for_brief(holding, brief_type='post'):
    """
    Gather all data for a single stock. Designed to run in ThreadPoolExecutor.
    brief_type: 'post' for post-market (full), 'pre' for pre-market (lighter).
    """
    ticker = holding.ticker
    stock_name = holding.stock_name
    result = {
        'ticker': ticker,
        'stock_name': stock_name,
        'sector': holding.sector,
        'industry': holding.industry,
        'quantity': holding.quantity,
        'avg_buy_price': holding.avg_buy_price,
    }

    # --- Price data ---
    try:
        yf_ticker = yf.Ticker(f"{ticker}.NS")
        hist = yf_ticker.history(period="25d")
        if hist is not None and not hist.empty:
            result['close'] = round(float(hist['Close'].iloc[-1]), 2)
            result['open'] = round(float(hist['Open'].iloc[-1]), 2)
            result['high'] = round(float(hist['High'].iloc[-1]), 2)
            result['low'] = round(float(hist['Low'].iloc[-1]), 2)
            result['volume'] = int(hist['Volume'].iloc[-1])

            if len(hist) >= 2:
                result['prev_close'] = round(float(hist['Close'].iloc[-2]), 2)
                if result['prev_close'] > 0:
                    result['day_change_pct'] = round(
                        ((result['close'] - result['prev_close']) / result['prev_close']) * 100, 2
                    )
                else:
                    result['day_change_pct'] = 0
            else:
                result['prev_close'] = result['close']
                result['day_change_pct'] = 0

            # Volume analysis (last candle vs 20-day average)
            if len(hist) >= 20:
                vol_20_avg = float(hist['Volume'].iloc[-21:-1].mean())
                if vol_20_avg > 0:
                    result['volume_ratio'] = round(result['volume'] / vol_20_avg, 2)
                    if result['volume_ratio'] >= 2.0:
                        result['volume_signal'] = 'Very High Volume'
                    elif result['volume_ratio'] >= 1.3:
                        result['volume_signal'] = 'High Volume'
                    elif result['volume_ratio'] <= 0.5:
                        result['volume_signal'] = 'Low Volume'
                    else:
                        result['volume_signal'] = 'Average Volume'
                else:
                    result['volume_ratio'] = 1.0
                    result['volume_signal'] = 'Average Volume'

            # Candlestick patterns (post-market only)
            if brief_type == 'post' and len(hist) >= 5:
                result['candlestick_patterns'] = detect_candlestick_patterns(hist)
            else:
                result['candlestick_patterns'] = []

            # P&L
            invested = holding.quantity * holding.avg_buy_price
            current_val = holding.quantity * result['close']
            result['invested'] = round(invested, 2)
            result['current_value'] = round(current_val, 2)
            result['pnl'] = round(current_val - invested, 2)
            result['pnl_pct'] = round(((current_val - invested) / invested) * 100, 2) if invested > 0 else 0
    except Exception as e:
        print(f"BRIEF: Price data failed for {ticker}: {e}", file=sys.stderr)
        result['close'] = None
        result['day_change_pct'] = 0

    # --- Technical analysis (post-market only) ---
    if brief_type == 'post':
        try:
            tech_res = evaluate_ticker_signal(ticker, interval='daily')
            if tech_res and tech_res.get('Signal') not in ['NO DATA', 'INSUFFICIENT DATA']:
                summary_rows = generate_summary(tech_res)
                result['tech_signal'] = tech_res.get('Signal', 'N/A')
                result['tech_summary'] = summary_rows  # list of {key, value} dicts

                df = tech_res['Data']
                result['rsi'] = round(float(df['RSI14'].iloc[-1]), 1) if 'RSI14' in df.columns and not pd.isna(df['RSI14'].iloc[-1]) else None

                # EMA stack
                if all(col in df.columns for col in ['EMA13', 'EMA55', 'EMA144']):
                    e13 = float(df['EMA13'].iloc[-1])
                    e55 = float(df['EMA55'].iloc[-1])
                    e144 = float(df['EMA144'].iloc[-1])
                    cp = float(df['Close'].iloc[-1])
                    if cp > e13 > e55 > e144:
                        result['trend'] = 'Strong Uptrend'
                    elif cp > e13 > e55:
                        result['trend'] = 'Uptrend'
                    elif cp < e13 < e55 < e144:
                        result['trend'] = 'Strong Downtrend'
                    elif cp < e13 < e55:
                        result['trend'] = 'Downtrend'
                    else:
                        result['trend'] = 'Sideways'
                else:
                    result['trend'] = 'N/A'
            else:
                result['tech_signal'] = 'N/A'
                result['tech_summary'] = []
                result['rsi'] = None
                result['trend'] = 'N/A'
        except Exception as e:
            print(f"BRIEF: Tech analysis failed for {ticker}: {e}", file=sys.stderr)
            result['tech_signal'] = 'N/A'
            result['tech_summary'] = []
            result['rsi'] = None
            result['trend'] = 'N/A'
    else:
        result['tech_signal'] = 'N/A'
        result['tech_summary'] = []
        result['rsi'] = None
        result['trend'] = 'N/A'

    # --- News via Gemini Google Search ---
    try:
        if brief_type == 'post':
            news_query = f"What are the latest major news, developments, analyst upgrades/downgrades, and corporate announcements about {stock_name} ({ticker}) Indian stock market in the last 24 hours? Be concise and factual. If there are no significant developments, just say 'No major news today.'"
        else:
            news_query = f"What happened with {stock_name} ({ticker}) since yesterday 3:30 PM IST? Cover any corporate announcements, global news impacting the stock, analyst upgrades/downgrades. Be concise. If nothing significant, say 'No overnight developments.'"

        news_result = call_gemini_api(
            messages=[{"role": "user", "content": news_query}],
            model="gemini-2.0-flash",
            temperature=0.3,
            use_google_search=True
        )
        result['news'] = news_result.strip() if news_result else 'No news available.'
    except Exception as e:
        print(f"BRIEF: News fetch failed for {ticker}: {e}", file=sys.stderr)
        result['news'] = 'News fetch failed.'

    # --- Quarterly Results (post-market only) ---
    if brief_type == 'post':
        try:
            from screener_fetcher import fetch_consolidated_async
            loop = asyncio.new_event_loop()
            tables, desc, _, _ = loop.run_until_complete(fetch_consolidated_async(ticker))
            loop.close()

            qr = tables.get('Quarterly Results')
            if qr is not None and not qr.empty:
                # Check if latest quarter is recent (within 30 days)
                latest_col = qr.columns[-1] if len(qr.columns) > 0 else None
                if latest_col:
                    try:
                        from dateutil import parser as dateparser
                        quarter_date = dateparser.parse(str(latest_col))
                        days_ago = (datetime.now() - quarter_date).days
                        if days_ago <= 45:
                            # Extract key metrics
                            qr_data = {}
                            for idx in qr.index:
                                val = qr.at[idx, latest_col]
                                idx_lower = str(idx).lower().strip()
                                if 'sales' in idx_lower or 'revenue' in idx_lower:
                                    qr_data['revenue'] = str(val)
                                elif 'net profit' in idx_lower:
                                    qr_data['net_profit'] = str(val)
                                elif 'opm' in idx_lower or 'operating profit margin' in idx_lower:
                                    qr_data['opm'] = str(val)
                            if qr_data:
                                result['quarterly_results'] = {
                                    'quarter': str(latest_col),
                                    'data': qr_data,
                                    'days_ago': days_ago
                                }
                    except Exception:
                        pass
        except Exception as e:
            print(f"BRIEF: Quarterly results failed for {ticker}: {e}", file=sys.stderr)

    return result


# --- Post-Market Brief Generation ---
def generate_post_market_brief(user_id):
    """Generate full post-market analysis brief for a user's portfolio."""
    from concurrent.futures import ThreadPoolExecutor

    holdings = Portfolio.get_by_user(user_id)
    if not holdings:
        return None

    # Parallel data gathering
    stock_data = []
    with ThreadPoolExecutor(max_workers=min(8, len(holdings))) as executor:
        futures = {executor.submit(_gather_stock_data_for_brief, h, 'post'): h for h in holdings}
        for future in futures:
            try:
                stock_data.append(future.result())
            except Exception as e:
                print(f"BRIEF: Stock data gathering failed: {e}", file=sys.stderr)

    if not stock_data:
        return None

    # Index data
    index_data = fetch_index_data()

    # Portfolio-level calculations
    total_invested = sum(s.get('invested', 0) for s in stock_data if s.get('invested'))
    total_current = sum(s.get('current_value', 0) for s in stock_data if s.get('current_value'))
    total_pnl = total_current - total_invested
    total_pnl_pct = round((total_pnl / total_invested * 100), 2) if total_invested > 0 else 0

    # Day P&L
    day_pnl = sum(
        s.get('quantity', 0) * (s.get('close', 0) - s.get('prev_close', s.get('close', 0)))
        for s in stock_data if s.get('close')
    )
    prev_total = sum(
        s.get('quantity', 0) * s.get('prev_close', s.get('close', 0))
        for s in stock_data if s.get('close')
    )
    day_change_pct = round((day_pnl / prev_total * 100), 2) if prev_total > 0 else 0

    # Alpha vs NIFTY
    nifty_change = index_data.get('nifty_change_pct') or 0
    alpha_pct = round(day_change_pct - nifty_change, 2)

    # Build comprehensive prompt
    today_str = datetime.now().strftime("%B %d, %Y (%A)")

    stocks_detail = []
    for s in stock_data:
        detail = f"""
--- {s.get('stock_name', 'N/A')} ({s.get('ticker', 'N/A')}) ---
Price: ₹{s.get('close', 'N/A')} (Open: ₹{s.get('open', 'N/A')}, High: ₹{s.get('high', 'N/A')}, Low: ₹{s.get('low', 'N/A')})
Day Change: {s.get('day_change_pct', 0):+.2f}%
Overall P&L: ₹{s.get('pnl', 0):+,.0f} ({s.get('pnl_pct', 0):+.2f}%)
Candlestick Patterns: {', '.join(s.get('candlestick_patterns', [])) or 'No pattern detected'}
Volume: {s.get('volume_signal', 'N/A')} ({s.get('volume_ratio', 1.0):.1f}x vs 20-day avg)
Technical Signal: {s.get('tech_signal', 'N/A')} | RSI: {s.get('rsi', 'N/A')} | Trend: {s.get('trend', 'N/A')}
News/Developments: {s.get('news', 'No news')}"""

        # Add tech summary rows
        if s.get('tech_summary'):
            detail += "\nChart Analysis:"
            for row in s['tech_summary']:
                detail += f"\n  {row['key']}: {row['value']}"

        # Add quarterly results if available
        if s.get('quarterly_results'):
            qr = s['quarterly_results']
            detail += f"\nLatest Quarterly Results ({qr['quarter']}, {qr['days_ago']} days ago):"
            for k, v in qr.get('data', {}).items():
                detail += f"\n  {k}: {v}"

        stocks_detail.append(detail)

    prompt_data = f"""Date: {today_str}

MARKET CONTEXT:
- NIFTY 50: {index_data.get('nifty_close', 'N/A')} ({nifty_change:+.2f}%)
- GIFT Nifty: {index_data.get('gift_nifty_last', 'N/A')}

PORTFOLIO OVERVIEW:
- Total Invested: ₹{total_invested:,.0f}
- Current Value: ₹{total_current:,.0f}
- Overall P&L: ₹{total_pnl:,.0f} ({total_pnl_pct:+.2f}%)
- Today's P&L: ₹{day_pnl:,.0f} ({day_change_pct:+.2f}%)
- Alpha vs NIFTY: {alpha_pct:+.2f}% ({'outperformed' if alpha_pct > 0 else 'underperformed'})
- Holdings: {len(stock_data)}

PER-STOCK DETAILED DATA:
{''.join(stocks_detail)}
"""

    system_prompt = """You are a senior portfolio analyst at a ₹100 Crore+ family office AUM. Generate a comprehensive post-market brief for the portfolio manager. Output ONLY clean HTML (no wrapping <html>, <body> tags).

STRUCTURE YOUR OUTPUT AS FOLLOWS:

1. **📊 Portfolio Overview** (3-4 sentences):
   - Today's portfolio performance with exact numbers
   - Alpha vs NIFTY 50 commentary (e.g., "outperformed NIFTY by 80 bps" or "underperformed by 120 bps")
   - Overall portfolio health snapshot

2. **🔴 Action Required** (stocks needing immediate attention):
   - Stocks that moved >4%, had bearish engulfing/evening star on high volume, major breaking news, or earnings release
   - For each: explain WHY it needs attention, linking candlestick + volume context
   - Example: "Doji on 3x average volume at resistance → signals institutional distribution"

3. **🟡 Watch List** (stocks with notable developments):
   - Technical shifts (RSI divergence, EMA crossover, morning star formation)
   - Moderate news or analyst actions
   - New quarterly results published

4. **🟢 Steady Holdings** (brief, 1 line each):
   - Small moves, no news, standard inside-day action

5. **💡 PM's Take** (2-3 sentences):
   - Actionable editorial: concentration risks, sector tilts, specific recommendations

RULES:
- ALWAYS interpret candlestick patterns IN CONTEXT of volume (e.g., "Hammer on 2.5x volume = strong reversal" vs "Hammer on 0.5x volume = weak signal")
- Use <strong> for stock names and key numbers
- Use <span style="color:#22c55e"> for positive and <span style="color:#ef4444"> for negative numbers
- Keep each stock's commentary to 2-3 lines max
- Sort stocks within each tier by severity/importance
- Never fabricate data — only use what is provided
- If a section has no stocks, omit it entirely
- Format as clean HTML with <h3>, <ul><li>, <p> tags"""

    try:
        briefing_html = call_gemini_api(
            messages=[{"role": "user", "content": system_prompt + "\n\n" + prompt_data}],
            model="gemini-3-flash-preview",
            temperature=0.6
        )
    except Exception as e:
        print(f"BRIEF: Post-market Gemini synthesis failed: {e}", file=sys.stderr)
        briefing_html = "<p>Could not generate post-market brief. Please try again.</p>"

    return {
        'briefing_html': briefing_html,
        'brief_type': 'post-market',
        'portfolio_summary': {
            'total_invested': round(total_invested, 2),
            'total_current': round(total_current, 2),
            'total_pnl': round(total_pnl, 2),
            'total_pnl_pct': total_pnl_pct,
            'day_pnl': round(day_pnl, 2),
            'day_change_pct': day_change_pct,
            'alpha_pct': alpha_pct,
            'holding_count': len(stock_data)
        },
        'index_data': index_data,
        'generated_at': datetime.now().isoformat()
    }


# --- Pre-Market Brief Generation ---
def generate_pre_market_brief(user_id):
    """Generate pre-market outlook brief."""
    from concurrent.futures import ThreadPoolExecutor

    holdings = Portfolio.get_by_user(user_id)
    if not holdings:
        return None

    # Parallel data gathering (lighter)
    stock_data = []
    with ThreadPoolExecutor(max_workers=min(8, len(holdings))) as executor:
        futures = {executor.submit(_gather_stock_data_for_brief, h, 'pre'): h for h in holdings}
        for future in futures:
            try:
                stock_data.append(future.result())
            except Exception as e:
                print(f"BRIEF: Pre-market data failed: {e}", file=sys.stderr)

    if not stock_data:
        return None

    index_data = fetch_index_data()
    today_str = datetime.now().strftime("%B %d, %Y (%A)")

    stocks_detail = []
    for s in stock_data:
        detail = f"""
--- {s.get('stock_name', 'N/A')} ({s.get('ticker', 'N/A')}) ---
Previous Close: ₹{s.get('close', 'N/A')}
Overall P&L: ₹{s.get('pnl', 0):+,.0f} ({s.get('pnl_pct', 0):+.2f}%)
Overnight News: {s.get('news', 'No news')}"""
        stocks_detail.append(detail)

    prompt_data = f"""Date: {today_str} (Pre-Market)

MARKET INDICATORS:
- GIFT Nifty: {index_data.get('gift_nifty_last', 'N/A')} ({index_data.get('gift_nifty_change_pct', 'N/A')}%)
- Previous NIFTY Close: {index_data.get('nifty_close', 'N/A')}

PORTFOLIO HOLDINGS:
{''.join(stocks_detail)}
"""

    system_prompt = """You are a senior portfolio analyst preparing a pre-market brief for a ₹100 Crore+ AUM family office. Output ONLY clean HTML.

STRUCTURE:

1. **🌅 Market Outlook** (2-3 sentences):
   - GIFT Nifty directional bias (e.g., "GIFT Nifty at 23,450, up 0.6% → expect a positive opening")
   - Global cues if mentioned in news

2. **📋 Stocks to Watch Today**:
   - Only stocks with significant overnight developments
   - Corporate actions, earnings announcements, analyst changes
   - Key levels to watch (if available from previous analysis)

3. **🔇 No Overnight Action**:
   - Brief line listing stocks with no overnight developments

RULES:
- Be concise — PMs read this at 8 AM before market open
- Use <strong> for stock names
- Skip empty sections
- No fabricated data
- Format as clean HTML with <h3>, <ul><li>, <p> tags"""

    try:
        briefing_html = call_gemini_api(
            messages=[{"role": "user", "content": system_prompt + "\n\n" + prompt_data}],
            model="gemini-3-flash-preview",
            temperature=0.5
        )
    except Exception as e:
        print(f"BRIEF: Pre-market Gemini synthesis failed: {e}", file=sys.stderr)
        briefing_html = "<p>Could not generate pre-market brief. Please try again.</p>"

    return {
        'briefing_html': briefing_html,
        'brief_type': 'pre-market',
        'index_data': index_data,
        'generated_at': datetime.now().isoformat()
    }


# --- Scheduled Brief Generation ---
def _run_scheduled_briefs():
    """Background thread that checks if it's time to generate briefs."""
    from datetime import timedelta, timezone
    ist = timezone(timedelta(hours=5, minutes=30))

    while True:
        try:
            now = datetime.now(ist)
            hour, minute = now.hour, now.minute

            # Pre-market: generate at 7:30 AM, cache until 10:00 AM (2.5h = 9000s)
            if hour == 7 and minute == 30:
                print("SCHEDULER: Triggering pre-market brief generation...", file=sys.stderr)
                _generate_briefs_for_all_users('pre', ttl_seconds=9000)
                time.sleep(60)  # Prevent re-trigger

            # Post-market: generate at 4:00 PM, cache until next day 9:15 AM (~17.25h = 62100s)
            elif hour == 16 and minute == 0:
                print("SCHEDULER: Triggering post-market brief generation...", file=sys.stderr)
                _generate_briefs_for_all_users('post', ttl_seconds=62100)
                time.sleep(60)

            time.sleep(30)  # Check every 30 seconds
        except Exception as e:
            print(f"SCHEDULER: Error: {e}", file=sys.stderr)
            time.sleep(60)

def _generate_briefs_for_all_users(brief_type, ttl_seconds):
    """Generate briefs for all users with portfolios."""
    try:
        all_users = Portfolio.get_all_user_ids()
    except Exception:
        # Fallback: if no get_all_user_ids method, skip
        print("SCHEDULER: Could not retrieve user list. Skipping scheduled generation.", file=sys.stderr)
        return

    today = datetime.now().strftime('%Y-%m-%d')
    for user_id in all_users:
        try:
            if brief_type == 'pre':
                data = generate_pre_market_brief(user_id)
            else:
                data = generate_post_market_brief(user_id)

            if data:
                set_brief_cache(brief_type, user_id, today, data, ttl_seconds)
                print(f"SCHEDULER: {brief_type}-market brief generated for user {user_id}", file=sys.stderr)
        except Exception as e:
            print(f"SCHEDULER: Failed for user {user_id}: {e}", file=sys.stderr)

# Start scheduler thread
_brief_scheduler_thread = threading.Thread(target=_run_scheduled_briefs, daemon=True)
_brief_scheduler_thread.start()


# --- API Endpoints ---

@app.route('/api/portfolio/brief/post-market', methods=['GET'])
def api_portfolio_brief_post():
    """Return post-market brief. On-demand if not cached."""
    from flask import session as flask_session
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    today = datetime.now().strftime('%Y-%m-%d')

    # Check cache
    cached = get_brief_cache('post', user_id, today)
    if cached:
        return jsonify(sanitize_for_json(cached))

    # Generate on-demand with 12h TTL
    brief = generate_post_market_brief(user_id)
    if not brief:
        return jsonify({'empty': True, 'message': 'No portfolio holdings found'})

    set_brief_cache('post', user_id, today, brief, 43200)
    return jsonify(sanitize_for_json(brief))


@app.route('/api/portfolio/brief/pre-market', methods=['GET'])
def api_portfolio_brief_pre():
    """Return pre-market brief. On-demand if not cached."""
    from flask import session as flask_session
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    today = datetime.now().strftime('%Y-%m-%d')

    cached = get_brief_cache('pre', user_id, today)
    if cached:
        return jsonify(sanitize_for_json(cached))

    brief = generate_pre_market_brief(user_id)
    if not brief:
        return jsonify({'empty': True, 'message': 'No portfolio holdings found'})

    set_brief_cache('pre', user_id, today, brief, 9000)
    return jsonify(sanitize_for_json(brief))


@app.route('/api/portfolio/brief/refresh', methods=['POST'])
def api_portfolio_brief_refresh():
    """Admin force-refresh endpoint. Clears cache and regenerates."""
    from flask import session as flask_session
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    data = request.get_json() or {}
    brief_type = data.get('type', 'post')  # 'pre' or 'post'

    today = datetime.now().strftime('%Y-%m-%d')
    clear_brief_cache(brief_type, user_id, today)

    if brief_type == 'pre':
        brief = generate_pre_market_brief(user_id)
        ttl = 9000
    else:
        brief = generate_post_market_brief(user_id)
        ttl = 43200

    if not brief:
        return jsonify({'error': 'No portfolio found'}), 404

    set_brief_cache(brief_type, user_id, today, brief, ttl)
    return jsonify(sanitize_for_json(brief))


# =====================================================================
# WHAT-IF SIMULATOR
# =====================================================================

@app.route('/api/portfolio/what-if', methods=['POST'])
def api_portfolio_whatif():
    """
    Simulate swapping one holding for another.
    Input: {sell_ticker, buy_ticker}
    Returns: new industry allocation, concentration, and 6-month backtest delta.
    """
    from flask import session as flask_session
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    data = request.get_json()
    sell_ticker = (data.get('sell_ticker') or '').strip().upper()
    buy_ticker = (data.get('buy_ticker') or '').strip().upper()

    if not sell_ticker or not buy_ticker:
        return jsonify({'error': 'Both sell_ticker and buy_ticker are required'}), 400

    if sell_ticker == buy_ticker:
        return jsonify({'error': 'Cannot swap a stock with itself'}), 400

    holdings = Portfolio.get_by_user(user_id)
    if not holdings:
        return jsonify({'error': 'No portfolio found'}), 404

    # Find the holding being sold
    sell_holding = None
    for h in holdings:
        if h.ticker.upper() == sell_ticker:
            sell_holding = h
            break

    if not sell_holding:
        return jsonify({'error': f'{sell_ticker} not found in your portfolio'}), 404

    try:
        # --- 1. Get current prices ---
        sell_price = None
        buy_price = None
        buy_info = {}

        try:
            yf_sell = yf.Ticker(f"{sell_ticker}.NS")
            hist = yf_sell.history(period="1d")
            if hist is not None and not hist.empty:
                sell_price = float(hist['Close'].iloc[-1])
        except Exception:
            pass

        try:
            yf_buy = yf.Ticker(f"{buy_ticker}.NS")
            hist = yf_buy.history(period="1d")
            if hist is not None and not hist.empty:
                buy_price = float(hist['Close'].iloc[-1])
            info = yf_buy.info
            buy_info = {
                'name': info.get('shortName', buy_ticker),
                'sector': info.get('sector', 'N/A'),
                'industry': info.get('industry', 'N/A')
            }
        except Exception:
            buy_info = {'name': buy_ticker, 'sector': 'N/A', 'industry': 'N/A'}

        if not sell_price or not buy_price:
            return jsonify({'error': 'Could not fetch current prices for one or both tickers'}), 400

        # Capital freed by selling
        sell_capital = sell_holding.quantity * sell_price
        # Shares of buy_ticker we can purchase
        buy_quantity = sell_capital / buy_price

        # --- 2. Build "after" portfolio for industry allocation + HHI ---
        after_holdings = []
        total_current_after = 0

        for h in holdings:
            if h.ticker.upper() == sell_ticker:
                # Replace with buy_ticker
                current_val = buy_quantity * buy_price
                after_holdings.append({
                    'ticker': buy_ticker,
                    'industry': buy_info.get('industry', 'N/A'),
                    'current_value': current_val
                })
            else:
                cp = None
                try:
                    yf_t = yf.Ticker(f"{h.ticker}.NS")
                    th = yf_t.history(period="1d")
                    if th is not None and not th.empty:
                        cp = float(th['Close'].iloc[-1])
                except Exception:
                    pass
                current_val = h.quantity * (cp if cp else h.avg_buy_price)
                after_holdings.append({
                    'ticker': h.ticker,
                    'industry': h.industry or 'N/A',
                    'current_value': current_val
                })
            total_current_after += after_holdings[-1]['current_value']

        # Industry allocation
        new_industry_data = {}
        for ah in after_holdings:
            ind = ah['industry'] or 'Unknown'
            new_industry_data[ind] = new_industry_data.get(ind, 0) + ah['current_value']

        # Compute HHI
        weights_after = [ah['current_value'] / total_current_after for ah in after_holdings if total_current_after > 0]
        hhi_after = sum(w ** 2 for w in weights_after) if weights_after else 0

        # Also compute current HHI for comparison
        current_weights = []
        total_current_before = 0
        for h in holdings:
            cp = None
            try:
                yf_t = yf.Ticker(f"{h.ticker}.NS")
                th = yf_t.history(period="1d")
                if th is not None and not th.empty:
                    cp = float(th['Close'].iloc[-1])
            except Exception:
                pass
            val = h.quantity * (cp if cp else h.avg_buy_price)
            current_weights.append(val)
            total_current_before += val

        if total_current_before > 0:
            current_weights = [w / total_current_before for w in current_weights]
        hhi_before = sum(w ** 2 for w in current_weights) if current_weights else 0

        # --- 3. 6-month backtest ---
        backtest_delta_pct = None
        try:
            sell_hist = yf.Ticker(f"{sell_ticker}.NS").history(period="6mo")
            buy_hist = yf.Ticker(f"{buy_ticker}.NS").history(period="6mo")

            if sell_hist is not None and not sell_hist.empty and buy_hist is not None and not buy_hist.empty:
                sell_return = (sell_hist['Close'].iloc[-1] / sell_hist['Close'].iloc[0] - 1) * 100
                buy_return = (buy_hist['Close'].iloc[-1] / buy_hist['Close'].iloc[0] - 1) * 100
                backtest_delta_pct = round(float(buy_return - sell_return), 2)
        except Exception as e:
            print(f"WHAT-IF: Backtest failed: {e}", file=sys.stderr)

        # --- 4. Sell/Buy details ---
        sell_details = {
            'ticker': sell_ticker,
            'name': sell_holding.stock_name,
            'quantity': sell_holding.quantity,
            'current_price': round(sell_price, 2),
            'capital_freed': round(sell_capital, 2)
        }

        buy_details = {
            'ticker': buy_ticker,
            'name': buy_info.get('name', buy_ticker),
            'quantity': round(buy_quantity, 2),
            'current_price': round(buy_price, 2),
            'capital_used': round(sell_capital, 2),
            'industry': buy_info.get('industry', 'N/A')
        }

        return jsonify(sanitize_for_json({
            'new_industry_data': new_industry_data,
            'new_concentration': {
                'hhi': round(hhi_after, 4),
                'diversification_score': round((1 - hhi_after) * 10, 1)
            },
            'current_concentration': {
                'hhi': round(hhi_before, 4),
                'diversification_score': round((1 - hhi_before) * 10, 1)
            },
            'backtest_delta_pct': backtest_delta_pct,
            'sell_details': sell_details,
            'buy_details': buy_details
        }))

    except Exception as e:
        print(f"WHAT-IF: Error: {e}", file=sys.stderr)
        traceback.print_exc()
        return jsonify({'error': f'Simulation failed: {str(e)}'}), 500


# =====================================================================
# PEER SWAP CARDS
# =====================================================================

LOCAL_PEER_CACHE = {}  # { "ticker": { "data": {...}, "timestamp": ... } }
PEER_CACHE_TTL = 21600  # 6 hours

@app.route('/api/portfolio/peer-swaps', methods=['GET'])
def api_portfolio_peer_swaps():
    """
    For each holding, find the #1 peer alternative using Screener peer comparison.
    Returns metrics comparison for "peer swap" suggestions.
    """
    from flask import session as flask_session
    from screener_fetcher import fetch_peer_comparison_from_screener_async
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    holdings = Portfolio.get_by_user(user_id)
    if not holdings:
        return jsonify({'error': 'No portfolio found'}), 404

    peer_swaps = []

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        for h in holdings:
            ticker = h.ticker

            # Check local cache
            cache_key = ticker.upper()
            if cache_key in LOCAL_PEER_CACHE:
                entry = LOCAL_PEER_CACHE[cache_key]
                age = time.time() - entry.get("timestamp", 0)
                if age < PEER_CACHE_TTL:
                    peer_swaps.append(entry["data"])
                    continue
                else:
                    del LOCAL_PEER_CACHE[cache_key]

            try:
                peer_data = loop.run_until_complete(
                    fetch_peer_comparison_from_screener_async(ticker, h.stock_name)
                )

                if not peer_data or not peer_data.get('company'):
                    continue

                company = peer_data.get('company', {})
                peers_list = peer_data.get('peers') or []

                # --- Helper: safely parse numeric value from Screener data ---
                def _num(obj, *keys):
                    for k in keys:
                        v = obj.get(k)
                        if v is not None and v != 'N/A' and v != '':
                            try:
                                return float(str(v).replace(',', ''))
                            except (ValueError, TypeError):
                                pass
                    return None

                # --- Compute Industry Average from peers (same logic as app.html calculateIndustryAverage) ---
                def _peer_avg(*keys):
                    """Average a metric across all peers, skipping N/A."""
                    vals = []
                    for p in peers_list:
                        v = _num(p, *keys)
                        if v is not None:
                            vals.append(v)
                    if not vals:
                        return 'N/A'
                    return f"{sum(vals) / len(vals):.2f}"

                ind_metrics = {
                    'pe': _peer_avg('pe_ratio', 'pe'),
                    'roe': _peer_avg('roce', 'roe'),
                    'market_cap': _peer_avg('market_cap'),
                    'sales_growth': _peer_avg('sales_growth_yoy', 'sales_growth'),
                }

                # Company metrics (for 4-criteria comparison)
                c_pe = _num(company, 'pe_ratio', 'pe')
                c_sales_gr = _num(company, 'sales_growth_yoy', 'sales_growth')
                c_mcap = _num(company, 'market_cap')
                c_roce = _num(company, 'roce', 'roe')

                # --- 4-Criteria Smart Peer Selection ---
                best_peer = None
                for p in (peer_data.get('peers') or []):
                    peer_name_lower = p.get('name', '').lower()
                    company_name_lower = (company.get('name', '') or '').lower()
                    if peer_name_lower == company_name_lower or not peer_name_lower:
                        continue

                    p_pe = _num(p, 'pe_ratio', 'pe')
                    p_sales_gr = _num(p, 'sales_growth_yoy', 'sales_growth')
                    p_mcap = _num(p, 'market_cap')
                    p_roce = _num(p, 'roce', 'roe')

                    if None in (p_pe, p_sales_gr, p_mcap, p_roce,
                                c_pe, c_sales_gr, c_mcap, c_roce):
                        continue

                    if p_pe > c_pe:
                        continue
                    if p_sales_gr <= c_sales_gr:
                        continue
                    mcap_floor = min(2000, c_mcap * 0.5)
                    if p_mcap < mcap_floor:
                        continue
                    if p_roce <= c_roce:
                        continue

                    if best_peer is None or p_roce > _num(best_peer, 'roce', 'roe'):
                        best_peer = p

                # Build swap entry — ALWAYS, even without a qualifying peer
                swap_entry = {
                    'holding_ticker': ticker,
                    'holding_name': h.stock_name,
                    'holding_metrics': {
                        'cmp': company.get('cmp', 'N/A'),
                        'pe': company.get('pe_ratio', company.get('pe', 'N/A')),
                        'roe': company.get('roce', company.get('roe', 'N/A')),
                        'market_cap': company.get('market_cap', 'N/A'),
                        'sales_growth': company.get('sales_growth_yoy', company.get('sales_growth', 'N/A')),
                    },
                    'industry_avg': ind_metrics,
                    'peer_name': None,
                    'peer_metrics': None,
                    'swap_reason': None,
                }

                if best_peer:
                    swap_entry['peer_name'] = best_peer.get('name', 'N/A')
                    swap_entry['peer_metrics'] = {
                        'cmp': best_peer.get('cmp', 'N/A'),
                        'pe': best_peer.get('pe_ratio', best_peer.get('pe', 'N/A')),
                        'roe': best_peer.get('roce', best_peer.get('roe', 'N/A')),
                        'market_cap': best_peer.get('market_cap', 'N/A'),
                        'sales_growth': best_peer.get('sales_growth_yoy', best_peer.get('sales_growth', 'N/A')),
                    }
                    swap_entry['swap_reason'] = f"Lower P/E ({_num(best_peer, 'pe_ratio', 'pe'):.1f} vs {c_pe:.1f}), Higher Sales Growth, Better ROCE ({_num(best_peer, 'roce', 'roe'):.1f}% vs {c_roce:.1f}%)"

                peer_swaps.append(swap_entry)

                # Cache it
                LOCAL_PEER_CACHE[cache_key] = {"data": swap_entry, "timestamp": time.time()}

            except Exception as e:
                print(f"PEER-SWAP: Failed for {ticker}: {e}", file=sys.stderr)
                continue
    finally:
        loop.close()

    return jsonify(sanitize_for_json({'peer_swaps': peer_swaps}))


# =====================================================================
# AI PORTFOLIO MANAGER — Phase 1 (Chat with Full Portfolio Context)
# =====================================================================

# In-memory stores for portfolio AI chat
_PORTFOLIO_AI_HISTORY = {}      # { user_id: [ {role, content}, ... ] }
_PORTFOLIO_CONTEXT_CACHE = {}   # { user_id: { "text": str, "timestamp": float } }
PORTFOLIO_CONTEXT_TTL = 300     # 5 minutes

def _build_portfolio_context(user_id):
    """
    Assemble a comprehensive text snapshot of the user's portfolio.
    Cached for 5 minutes to avoid re-fetching on each chat message.
    Returns a string suitable for injecting into the system prompt.
    """
    # Check cache
    cached = _PORTFOLIO_CONTEXT_CACHE.get(user_id)
    if cached and (time.time() - cached["timestamp"]) < PORTFOLIO_CONTEXT_TTL:
        return cached["text"]

    holdings = Portfolio.get_by_user(user_id)
    if not holdings:
        return "The user has no holdings in their portfolio yet."

    lines = []
    lines.append("=" * 60)
    lines.append("PORTFOLIO SNAPSHOT (live data)")
    lines.append("=" * 60)

    total_invested = 0
    total_current = 0
    holding_rows = []
    industry_map = {}
    tech_signals = []

    for h in holdings:
        current_price = None
        day_change_pct = 0
        try:
            yf_ticker = yf.Ticker(f"{h.ticker}.NS")
            hist = yf_ticker.history(period="2d")
            if hist is not None and not hist.empty:
                current_price = float(hist['Close'].iloc[-1])
                if len(hist) >= 2:
                    prev_close = float(hist['Close'].iloc[-2])
                    if prev_close > 0:
                        day_change_pct = round(((current_price - prev_close) / prev_close) * 100, 2)
        except Exception:
            pass

        invested = h.quantity * h.avg_buy_price
        current_val = h.quantity * current_price if current_price else invested
        pnl = current_val - invested
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0

        total_invested += invested
        total_current += current_val

        ind = h.industry or 'Unknown'
        industry_map[ind] = industry_map.get(ind, 0) + current_val

        days_held = ''
        if h.buy_date:
            try:
                bd = datetime.strptime(h.buy_date, '%Y-%m-%d')
                days_held = str((datetime.now() - bd).days)
            except Exception:
                pass

        cmp_str = f"₹{current_price:.2f}" if current_price else "N/A"
        holding_rows.append(
            f"  {h.stock_name} ({h.ticker}) | Qty: {h.quantity} | "
            f"Avg Buy: ₹{h.avg_buy_price:.2f} | CMP: {cmp_str} | "
            f"P&L: ₹{pnl:+,.0f} ({pnl_pct:+.2f}%) | Day: {day_change_pct:+.2f}% | "
            f"Sector: {h.sector or 'N/A'} | Industry: {ind} | Days Held: {days_held or 'N/A'}"
        )

        # Technical health (best-effort, lightweight)
        try:
            tech_res = evaluate_ticker_signal(h.ticker, interval='daily')
            if tech_res and tech_res.get('Signal') not in ['NO DATA', 'INSUFFICIENT DATA']:
                summary_rows = generate_summary(tech_res)
                summary_dict = {row['key']: row['value'] for row in summary_rows}
                pa = summary_dict.get('Price-Action Trend (based on Close prices)', 'N/A')
                ms = summary_dict.get('Market Structure (based on EMA Stack)', 'N/A')
                sentiment = summary_dict.get('Market Sentiment (based on RSI)', 'N/A')
                rel_str = summary_dict.get('Relative Strength vs Nifty', 'N/A')
                vol = summary_dict.get('Accumulating or Distributing (based on Volume)', 'N/A')
                fib = summary_dict.get('Trend Strength (based on Fibonacci retracement)', 'N/A')

                # Compute signal
                if 'Uptrend' in pa:
                    signal = 'HOLD'
                elif 'Sideways' in pa and ('Uptrend' in ms or 'Mild Uptrend' in ms):
                    signal = 'HOLD'
                else:
                    signal = 'SELL'

                tech_signals.append(
                    f"  {h.ticker}: Signal={signal} | Price Action={pa} | "
                    f"Market Structure={ms} | Sentiment={sentiment} | "
                    f"Rel. Strength vs Nifty={rel_str} | Volume={vol} | Fib Strength={fib}"
                )
            else:
                tech_signals.append(f"  {h.ticker}: Technical data insufficient")
        except Exception:
            tech_signals.append(f"  {h.ticker}: Technical analysis unavailable")

    total_pnl = total_current - total_invested
    total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0

    lines.append("")
    lines.append(f"PORTFOLIO SUMMARY:")
    lines.append(f"  Total Invested: ₹{total_invested:,.0f}")
    lines.append(f"  Current Value:  ₹{total_current:,.0f}")
    lines.append(f"  Total P&L:      ₹{total_pnl:+,.0f} ({total_pnl_pct:+.2f}%)")
    lines.append(f"  Holdings Count: {len(holdings)}")

    lines.append("")
    lines.append("HOLDINGS:")
    lines.extend(holding_rows)

    # Industry allocation
    lines.append("")
    lines.append("INDUSTRY ALLOCATION:")
    for ind_name, ind_val in sorted(industry_map.items(), key=lambda x: -x[1]):
        pct = (ind_val / total_current * 100) if total_current > 0 else 0
        lines.append(f"  {ind_name}: ₹{ind_val:,.0f} ({pct:.1f}%)")

    # Concentration
    if total_current > 0:
        hhi = sum((v / total_current) ** 2 for v in industry_map.values())
        lines.append("")
        lines.append(f"CONCENTRATION (HHI): {hhi:.4f}")
        if hhi > 0.25:
            lines.append("  Level: HIGH — portfolio is concentrated in few industries")
        elif hhi > 0.15:
            lines.append("  Level: MODERATE")
        else:
            lines.append("  Level: LOW — well diversified")

    # Technical health
    lines.append("")
    lines.append("TECHNICAL HEALTH (per stock):")
    lines.extend(tech_signals)

    # Portfolio-level metrics (beta, alpha, nifty comparison) — lightweight
    try:
        beta_sum = 0
        beta_weight = 0
        for h in holdings:
            try:
                h_yf = yf.Ticker(f"{h.ticker}.NS")
                h_info = h_yf.info or {}
                h_beta = h_info.get('beta', None)
                h_price = h_info.get('currentPrice') or h_info.get('regularMarketPrice') or h.avg_buy_price
                val = h.quantity * h_price
                if h_beta is not None and total_current > 0:
                    beta_sum += h_beta * (val / total_current)
                    beta_weight += val
            except Exception:
                pass
        if beta_weight > 0:
            lines.append("")
            lines.append(f"PORTFOLIO BETA (value-weighted): {beta_sum:.2f}")
    except Exception:
        pass

    context_text = "\n".join(lines)

    # Cache it
    _PORTFOLIO_CONTEXT_CACHE[user_id] = {"text": context_text, "timestamp": time.time()}
    return context_text


@app.route('/api/portfolio/ai-chat', methods=['POST'])
def api_portfolio_ai_chat():
    """
    AI Portfolio Manager chat endpoint.
    Accepts { message, history? } and returns AI response with full portfolio context.
    Uses gemini-3.1-pro-preview with HIGH thinking and Google Search enabled.
    """
    from flask import session as flask_session
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401

    data = request.get_json(force=True)
    user_message = (data.get('message') or '').strip()
    if not user_message:
        return jsonify({'error': 'Message is required'}), 400

    # Build portfolio context
    try:
        portfolio_context = _build_portfolio_context(user_id)
    except Exception as e:
        print(f"AI-PM: Context build failed: {e}", file=sys.stderr)
        traceback.print_exc()
        portfolio_context = "Could not load portfolio data."

    # System prompt with portfolio context
    system_prompt = f"""You are Kagger AI Portfolio Manager — a senior, highly experienced portfolio analyst working exclusively for this investor.

You have FULL ACCESS to the investor's live portfolio data below. Use this data to answer questions with precision and confidence, citing exact numbers (P&L, weights, returns, etc.) from their portfolio.

YOUR CAPABILITIES:
- Deep analysis of the investor's current holdings, P&L, sector allocation, and concentration risk
- Technical analysis interpretation (price action, market structure, RSI, EMA stack, relative strength vs Nifty)
- Risk assessment and diversification recommendations
- Stock-level commentary: hold/sell signals, momentum shifts, volume anomalies
- Portfolio-level insights: beta exposure, alpha generation, sector tilts
- Market context via web search (you have Google Search access for real-time market data, news, and analyst opinions)

RESPONSE STYLE:
- Be direct and actionable — this is a real portfolio with real money
- Lead with the bottom line, then explain the reasoning
- Use numbers from the portfolio data to support your analysis
- When discussing stocks, always reference the investor's actual position size, P&L, and weight
- Format your responses in clean markdown (use **bold** for emphasis, bullet points for lists, ### for section headers)
- Keep responses concise but comprehensive — think FM-grade portfolio review, not academic essay
- Use ₹ for Indian currency. Format large numbers in Lakhs (L) or Crores (Cr) as appropriate
- If the investor asks about something outside their portfolio, use your Google Search to provide current market information

{portfolio_context}
"""

    # Get or create conversation history
    if user_id not in _PORTFOLIO_AI_HISTORY:
        _PORTFOLIO_AI_HISTORY[user_id] = []

    history = _PORTFOLIO_AI_HISTORY[user_id]

    # Build messages for Gemini
    messages = [{"role": "user", "content": system_prompt + "\n\nPlease acknowledge you have loaded the portfolio. Do not list all the holdings — just confirm you're ready."}]
    # If no history, add a synthetic assistant greeting
    if not history:
        messages.append({"role": "model", "content": "Portfolio loaded. I have full visibility into your holdings, P&L, sector allocation, technical signals, and risk metrics. How can I help you today?"})

    # Add conversation history
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})

    # Add current user message
    messages.append({"role": "user", "content": user_message})

    # Call Gemini
    try:
        ai_response = call_gemini_api(
            messages=messages,
            model="gemini-3.1-pro-preview",
            temperature=0.7,
            use_google_search=True,
            thinking_level="HIGH"
        )
    except Exception as e:
        print(f"AI-PM: Gemini call failed: {e}", file=sys.stderr)
        traceback.print_exc()
        ai_response = "I'm sorry, I encountered an issue processing your request. Please try again in a moment."

    # Store in history (keep last 20 messages = 10 turns)
    history.append({"role": "user", "content": user_message})
    history.append({"role": "model", "content": ai_response})
    if len(history) > 20:
        history[:] = history[-20:]

    return jsonify({
        'response': ai_response,
        'history_length': len(history)
    })


@app.route('/api/portfolio/ai-chat/clear', methods=['POST'])
def api_portfolio_ai_chat_clear():
    """Clear the AI Portfolio Manager conversation history."""
    from flask import session as flask_session
    user_id = flask_session.get('user_id')
    if not user_id:
        return jsonify({'error': 'Authentication required'}), 401
    _PORTFOLIO_AI_HISTORY.pop(user_id, None)
    _PORTFOLIO_CONTEXT_CACHE.pop(user_id, None)
    return jsonify({'success': True})


# AI Summarize Analyst PDF
@app.route('/summarize-analyst-pdf', methods=['POST'])
def summarize_analyst_pdf():
    """
    Endpoint to summarize analyst report PDFs from Trendlyne using Gemini AI.
    Requires Trendlyne authentication credentials to download PDFs.
    """
    try:
        data = request.get_json(force=True)
        pdf_url = data.get('pdf_url', '').strip()
        
        if not pdf_url:
            return jsonify({'error': 'No pdf_url provided'}), 400
        
        # Validate that this is a Trendlyne URL
        if 'trendlyne.com' not in pdf_url:
            return jsonify({'error': 'Invalid PDF URL.'}), 400
        
        log_progress(f"Summarizing analyst PDF: {pdf_url}")
        
        # Run the async function
        summary = asyncio.run(summarize_analyst_pdf_async(pdf_url))
        
        log_final_message("Analyst PDF summarized successfully!")
        
        return jsonify({
            'success': True,
            'summary': summary,
            'pdf_url': pdf_url
        })
        
    except Exception as e:
        error_msg = f"Failed to summarize analyst PDF: {str(e)}"
        log_final_message(error_msg)
        traceback.print_exc()
        return jsonify({'error': error_msg}), 500


# =====================================================================
# ANALYST REPORTS PDF TEXT EXTRACTION (Background Task Support)
# =====================================================================

def extract_text_from_pdf(pdf_bytes):
    """
    Extract raw text from PDF bytes using pdfplumber.
    This is used for background text extraction (no AI cost).
    """
    try:
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            text_parts = [page.extract_text() or '' for page in pdf.pages]
        return '\n'.join(text_parts)
    except Exception as e:
        print(f"WARN: PDF text extraction failed: {e}")
        return f"[PDF extraction failed: {e}]"


@app.route('/analyst-texts-status/<analysis_key>', methods=['GET'])
def analyst_texts_status(analysis_key):
    """
    Check if analyst PDF texts have been extracted and cached.
    Returns ready status and count of reports.
    """
    try:
        cached = cache.get(f"{analysis_key}_analyst_texts")
        if cached:
            if isinstance(cached, bytes):
                texts = pickle.loads(zlib.decompress(cached))
            else:
                texts = cached
            return jsonify({'ready': True, 'count': len(texts)})
    except Exception as e:
        print(f"WARN: Failed to check analyst texts status: {e}")
    return jsonify({'ready': False})


# # at the top of handler.py
# import openai

# =====================================================================
# START: API Configuration and Multi-Model Handling
# =====================================================================

import openai
#from perplexity import Perplexity 
from google import genai
from google.genai import types
# Securely load API keys from environment variables
openai.api_key = os.getenv("OPENAI_API_KEY")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# Configure Google Gemini (New v1.0 SDK)
genai_client = None
if GOOGLE_API_KEY:
    try:
        genai_client = genai.Client(api_key=GOOGLE_API_KEY)
        print("INFO: Google GenAI (v1.0+) client initialized.")
    except Exception as e:
        print(f"ERROR: Failed to initialize Google GenAI client: {e}")

# Safety check for keys at startup
if not all([openai.api_key, PERPLEXITY_API_KEY, GOOGLE_API_KEY]):
    print("CRITICAL WARNING: One or more API keys (OPENAI_API_KEY, PERPLEXITY_API_KEY, GOOGLE_API_KEY) are not set in your environment variables.")

# Helper to convert messages to Gemini format
def convert_to_gemini_format(messages):
    gemini_messages = []
    for msg in messages:
        # Gemini API requires alternating user/model roles, and can't have two 'user' roles in a row.
        # We'll simplify and merge consecutive user messages if needed, although our app structure avoids this.
        role = "user" if msg["role"] == "user" else "model"
        if gemini_messages and gemini_messages[-1]['role'] == role:
            # If last message has the same role, append content. This is a safeguard.
            gemini_messages[-1]['parts'].append(msg["content"])
        else:
            gemini_messages.append({'role': role, 'parts': [msg["content"]]})
    return gemini_messages

def call_openai_api(messages, model="gpt-5-mini", expect_json_format_flag=False, temperature=1, timeout=180):
    """
    Call OpenAI API with configurable timeout.
    Default timeout is 180 seconds (3 minutes) for slower models like gpt-5-mini.
    """
    if not openai.api_key:
        raise ValueError("OpenAI API key is not configured.")
    try:
        completion_params = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "timeout": timeout,  # Explicit timeout in seconds
        }
        # Use OpenAI's JSON mode for reliable structured output
        if expect_json_format_flag and ("-turbo" in model or "-o" in model):
            completion_params["response_format"] = {"type": "json_object"}

        response = openai.chat.completions.create(**completion_params)
        return response.choices[0].message.content
    except Exception as e:
        print(f"ERROR in call_openai_api: {e}")
        raise

def call_perplexity_api(messages, model="sonar-pro", temperature=1, timeout=120, use_streaming=False, enable_pro_search=False, progress_callback=None):
    """
    Call Perplexity API with optional streaming support and Pro Search.
    Streaming keeps the connection alive for long-running requests (Azure compatibility).
    Pro Search enables multi-step reasoning and deeper web research.
    
    Args:
        progress_callback: Optional function(elapsed_seconds, bytes_received) called every 20s during streaming
    
    Enhanced with robust error handling and debug logging for Industry Research.
    """
    if not PERPLEXITY_API_KEY:
        raise ValueError("Perplexity API key is not configured.")
    
    response = None  # Initialize for error handling
    try:
        url = "https://api.perplexity.ai/chat/completions"
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        
        # Enable Pro Search for better research capabilities
        if enable_pro_search:
            payload["web_search_options"] = {
                "search_type": "pro"  # Enables multi-step reasoning and deeper search
            }
        
        # Enable streaming for long-running models
        if use_streaming or model == "sonar-deep-research":
            payload["stream"] = True
        
        headers = {
            "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
            "Content-Type": "application/json"
        }
        
        if payload.get("stream"):
            # Streaming mode - read chunks as they come
            # MEMORY OPTIMIZATION: Use list accumulation instead of string concatenation
            # String concatenation creates new objects each time (O(n²) memory)
            # List append + join is O(n) memory efficient
            print(f"STREAM_DEBUG: Starting stream for model={model}, timeout={timeout}s", file=sys.stderr)
            start_time = time.time()
            content_chunks = []  # Memory-efficient: list accumulation
            chunk_count = 0
            total_bytes = 0
            last_log_time = start_time
            
            try:
                with requests.post(url, headers=headers, json=payload, timeout=timeout, stream=True) as response:
                    response.raise_for_status()
                    print(f"STREAM_DEBUG: HTTP {response.status_code}, headers received", file=sys.stderr)
                    
                    for line in response.iter_lines():
                        if line:
                            chunk_count += 1
                            total_bytes += len(line)
                            
                            # OPTIMIZATION: Log every 100 chunks (not 10) to reduce I/O overhead
                            current_time = time.time()
                            if chunk_count % 100 == 0 or (current_time - last_log_time) > 20:  # Changed from 60 to 20
                                elapsed = int(current_time - start_time)
                                content_len = sum(len(c) for c in content_chunks)
                                print(f"STREAM_DEBUG: Chunk #{chunk_count}, Bytes: {total_bytes}, Elapsed: {elapsed}s, Content: {content_len} chars", file=sys.stderr)
                                last_log_time = current_time
                                
                                # NEW: Call progress callback if provided
                                if progress_callback:
                                    try:
                                        progress_callback(elapsed, total_bytes)
                                    except Exception as cb_error:
                                        print(f"STREAM_WARN: Progress callback failed: {cb_error}", file=sys.stderr)
                            
                            line_str = line.decode('utf-8')
                            if line_str.startswith('data: '):
                                data_str = line_str[6:]  # Remove 'data: ' prefix
                                if data_str.strip() == '[DONE]':
                                    print(f"STREAM_DEBUG: Received [DONE] signal", file=sys.stderr)
                                    break
                                try:
                                    chunk_data = json.loads(data_str)
                                    if 'choices' in chunk_data and len(chunk_data['choices']) > 0:
                                        delta = chunk_data['choices'][0].get('delta', {})
                                        content = delta.get('content', '')
                                        if content:
                                            content_chunks.append(content)  # O(1) append
                                except json.JSONDecodeError as jde:
                                    # Only log first few JSON errors to reduce noise
                                    if chunk_count < 10:
                                        print(f"STREAM_DEBUG: JSON decode error in chunk {chunk_count}: {jde}", file=sys.stderr)
                                    continue
                    
                    # Single join operation at the end - O(n) instead of O(n²)
                    full_content = ''.join(content_chunks)
                    
                    elapsed_total = int(time.time() - start_time)
                    print(f"STREAM_DEBUG: Stream complete. Chunks: {chunk_count}, Raw bytes: {total_bytes}, Time: {elapsed_total}s, Content: {len(full_content)} chars", file=sys.stderr)
                    
                    # Memory cleanup - important for Azure's limited memory
                    del content_chunks
                    import gc
                    gc.collect()
            
            except requests.exceptions.Timeout as te:
                elapsed = int(time.time() - start_time)
                full_content = ''.join(content_chunks) if content_chunks else ""
                print(f"STREAM_ERROR: Timeout after {elapsed}s. Chunks: {chunk_count}, Partial content: {len(full_content)} chars", file=sys.stderr)
                if full_content:
                    print(f"STREAM_RECOVERY: Returning partial content ({len(full_content)} chars) after timeout", file=sys.stderr)
                    return full_content
                raise ValueError(f"Stream timeout after {elapsed}s with no recoverable content")
            
            except requests.exceptions.RequestException as re:
                elapsed = int(time.time() - start_time)
                full_content = ''.join(content_chunks) if content_chunks else ""
                print(f"STREAM_ERROR: Connection error after {elapsed}s: {re}. Partial content: {len(full_content)} chars", file=sys.stderr)
                if full_content:
                    print(f"STREAM_RECOVERY: Returning partial content ({len(full_content)} chars) after connection error", file=sys.stderr)
                    return full_content
                raise
            
            if not full_content:
                print(f"STREAM_ERROR: Empty response after {chunk_count} chunks", file=sys.stderr)
                raise ValueError("Empty streaming response from Perplexity API")
            
            return full_content
        else:
            # Non-streaming mode (original behavior)
            print(f"API_DEBUG: Calling {model} in non-streaming mode, timeout={timeout}s", file=sys.stderr)
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            response.raise_for_status()
            
            if response.content.strip():
                content = response.json()['choices'][0]['message']['content']
                print(f"API_DEBUG: Non-streaming response received, length: {len(content)}", file=sys.stderr)
                return content
            else:
                raise ValueError("Empty response from Perplexity API")
            
    except requests.exceptions.JSONDecodeError as jde:
        response_text = response.text if response else "No response"
        print(f"API_ERROR: Non-JSON response: {response_text[:500]}", file=sys.stderr)
        raise ValueError(f"Invalid JSON response from Perplexity API: {response_text[:200]}")
    except Exception as e:
        print(f"API_ERROR in call_perplexity_api (model={model}): {type(e).__name__}: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise



def call_gemini_api(messages, model="gemini-3-flash-preview", temperature=1, use_google_search=False, thinking_level=None):
    """
    Call the Gemini API with optional thinking mode for deeper reasoning.
    Includes exponential backoff retries for 503/429 errors and model fallback.
    """
    global genai_client
    if not genai_client:
        raise ValueError("Google Gemini API client is not initialized (check GOOGLE_API_KEY).")
        
    from google.genai.errors import ServerError, APIError
    import time
    
    models_to_try = [model]
    if model == "gemini-3.1-pro-preview":
        models_to_try.append("gemini-3-flash-preview")
        
    for current_model in models_to_try:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Build generation config
                config_args = {
                    "temperature": temperature,
                    "safety_settings": [
                        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
                        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
                    ]
                }
                
                # Tools (Google Search)
                if use_google_search:
                    config_args["tools"] = [types.Tool(google_search=types.GoogleSearch())]
                    
                # Thinking Config
                if thinking_level and "gemini-3" in current_model:
                    config_args["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
                    print(f"DEBUG: Gemini thinking mode enabled for {current_model}: {thinking_level}", file=sys.stderr)

                gen_config = types.GenerateContentConfig(**config_args)
                
                gemini_contents = []
                for msg in messages:
                    role = "user" if msg["role"] == "user" else "model"
                    gemini_contents.append(types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])]))

                response = genai_client.models.generate_content(
                    model=current_model,
                    contents=gemini_contents,
                    config=gen_config
                )
                
                return response.text
                
            except (ServerError, APIError) as e:
                # 503 (Unavailable) or 429 (Rate Limit)
                is_retryable = False
                if isinstance(e, ServerError) and "503" in str(e):
                    is_retryable = True
                if isinstance(e, APIError) and "429" in str(e):
                    is_retryable = True
                    
                if is_retryable and attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * 2 # 2s, 4s, 8s
                    print(f"WARN: Gemini {current_model} returned {type(e).__name__} (attempt {attempt+1}). Retrying in {wait_time}s...", file=sys.stderr)
                    time.sleep(wait_time)
                    continue
                
                if current_model != models_to_try[-1]:
                    print(f"ERROR: Gemini {current_model} failed after retries. Falling back to {models_to_try[-1]}...", file=sys.stderr)
                    break # Break inner loop, try next model in outer loop
                else:
                    print(f"ERROR in call_gemini_api (final model {current_model}): {e}")
                    traceback.print_exc()
                    raise
            except Exception as e:
                print(f"NON-RETRYABLE ERROR in call_gemini_api: {e}")
                traceback.print_exc()
                raise
                
    raise Exception("All attempted Gemini models failed.")



def call_generative_ai_model(model, messages, temperature=1, timeout=180, thinking_level=None):
    """
    Dispatcher function to call the appropriate AI model API.
    Default timeout is 180 seconds for Azure compatibility.
    
    Args:
        thinking_level: For Gemini 3 models only - 'HIGH', 'LOW', 'MINIMAL' or None
    """
    log_progress(f"Dispatching request to model: {model}")
    try:
        if model.startswith('gpt-') or model.startswith('o4-'):
            return call_openai_api(messages, model=model, temperature=temperature, timeout=timeout)
        elif model.startswith('pplx-') or model.startswith('llama-') or model.startswith('r1-') or model.startswith('pplx-') or model.startswith('sonar'):
            return call_perplexity_api(messages, model=model, temperature=temperature, timeout=timeout)
        elif model.startswith('gemini-'):
            return call_gemini_api(messages, model=model, temperature=temperature, thinking_level=thinking_level)
        else:
            # Default to a reliable, cheap model if the selection is unknown
            print(f"WARN: Unknown model '{model}', defaulting to 5-mini'.")
            return call_openai_api(messages, model='gpt-5-mini', temperature=temperature, timeout=timeout)
    except Exception as e:
        # Catch errors from any of the specific API call functions
        error_message = f"An error occurred while calling the AI model '{model}': {str(e)}"
        print(f"ERROR: {error_message}")
        # Re-raise the exception to be handled by the Flask route's error handler
        raise Exception(error_message)

# =====================================================================
# END: API Configuration and Multi-Model Handling
# =====================================================================

# =====================================================================
# START: Data Schema Description Function and RAG Function for AI Planner
# =====================================================================
# In handler.py

def get_data_schema_description(current_analysis_context):
    if not current_analysis_context or not isinstance(current_analysis_context, dict) or not current_analysis_context.get("ticker"):
        return "No analysis data currently loaded or data is malformed."

    schema_lines = ["**Dynamically Generated Data Schema Description for Current Stock Analysis:**\n"]
    schema_lines.append(f"Ticker: {current_analysis_context.get('ticker', 'N/A')}\n")

    # 1. Technical Summary
    summary_data = current_analysis_context.get("summary")
    if summary_data and isinstance(summary_data, list):
        schema_lines.append("\n**1. Technical Summary (section_name: 'summary')**")
        schema_lines.append("   - A list of key-value pairs. Available keys:")
        # Ensure s_key is handled if it could contain characters needing escaping for f-string *expressions*,
        # but here it's just being inserted as a string value, so f-string handles it.
        summary_keys = sorted(list(set(str(item.get('key', '')) for item in summary_data if isinstance(item, dict) and item.get('key') is not None)))
        for s_key in summary_keys:
            # f-string is fine here as s_key is an expression evaluating to a string.
            # The quotes around {s_key} are literal parts of the f-string.
            schema_lines.append(f"     - \"{s_key}\"")
        schema_lines.append("   - Contains current market data like 'Current Price', 'Market Cap'.")
        schema_lines.append("   - To retrieve, specify `technical_summary_keys`: [\"Key1\", \"Key2\"].")

    # 2. Fundamentals
    fundamentals_data = current_analysis_context.get("fundamentals")
    if fundamentals_data and isinstance(fundamentals_data, dict):
        schema_lines.append("\n**2. Fundamentals (section_name: 'fundamentals')**")
        schema_lines.append("   - A dictionary of tables. Each table is a list of rows (dictionaries).")
        
        sorted_table_names = sorted(list(fundamentals_data.keys()))
        # Using f'{name!r}' or json.dumps(name) is safer if table names could have quotes/backslashes
        schema_lines.append(f"   - Available Table Names: {', '.join(json.dumps(name) for name in sorted_table_names)}")

        for table_name in sorted_table_names:
            table_content = fundamentals_data.get(table_name)
            # Using f'{table_name!r}' for safety if table_name has special chars
            schema_lines.append(f"\n   **Table: {json.dumps(table_name)}**") # Use json.dumps for table_name
            if table_content and isinstance(table_content, list) and table_content:
                metric_names_in_table = sorted(list(set(str(row.get("", "")) for row in table_content if isinstance(row, dict) and row.get("") is not None)))
                # This line describes the key `""` literally.
                schema_lines.append("     - Available Metric Names (values of the literal key `\"\"` (empty string) in rows):")
                for m_name in metric_names_in_table:
                    schema_lines.append(f"       - {json.dumps(m_name)}") # Use json.dumps for metric names

                all_period_headers = set()
                for row in table_content:
                    if isinstance(row, dict):
                        all_period_headers.update(str(k) for k in row.keys() if k not in ["", "index"]) # Ensure keys are strings
                sorted_period_headers = sorted(list(all_period_headers), key=lambda x: str(x))
                schema_lines.append(f"     - Available Period Column Headers in this table: {', '.join(json.dumps(p) for p in sorted_period_headers)}")
                # Using f'{table_name!r}' for safety
                schema_lines.append(f"     - To retrieve from {json.dumps(table_name)}, specify `metrics`: [\"ExactMetricName1\", ...] and optionally `periods`: [\"ExactPeriodHeader1\", ... or integer index like -1 for latest].")
            else:
                schema_lines.append("     - (No data or not a list of rows)")
        schema_lines.append("")

    # 3. Valuation & Margin Data Series
    valuation_data = current_analysis_context.get("valuation_and_margin_data")
    if valuation_data and isinstance(valuation_data, dict):
        schema_lines.append("\n**3. Valuation & Margin Data Series (section_name: 'valuation_and_margin_data')**")
        schema_lines.append("   - A dictionary of time series. Each series is a list of data points (dictionaries).")
        sorted_series_names = sorted(list(valuation_data.keys()))
        schema_lines.append(f"   - Available Series Names: {', '.join(json.dumps(name) for name in sorted_series_names)}")

        for series_name in sorted_series_names[:3]: 
            series_content = valuation_data.get(series_name)
            schema_lines.append(f"   - Series {json.dumps(series_name)}:") # Use json.dumps
            if series_content and isinstance(series_content, list) and series_content:
                first_point = series_content[0]
                if isinstance(first_point, dict):
                    field_names = sorted(list(str(k) for k in first_point.keys())) # Ensure keys are strings
                    schema_lines.append(f"     - Data points are dicts with fields like: {', '.join(json.dumps(f) for f in field_names)}")
            # This was the line with the typo "lapythoN", now corrected to "latest N"
            schema_lines.append(f"     - To retrieve from {json.dumps(series_name)}, specify `points`: \"latest N\" or \"all\".")
        if len(sorted_series_names) > 3:
            schema_lines.append("   - (Details for other series follow similar structure)")
        schema_lines.append("")

    # 4. Documents for fetching Management Guidance
    documents_data = current_analysis_context.get("documents")
    if documents_data and isinstance(documents_data, list):
        schema_lines.append("\n**4. Documents (section_name: 'documents')**")
        schema_lines.append("   - A list of dictionaries for recent corporate documents.")
        schema_lines.append("   - Each dictionary has a `type` ('Presentation' or 'Concall') and a `content_summary`.")
        schema_lines.append("   - **The 'Presentation' summary is a detailed, AI-generated analysis** from Gemini, containing structured data on financial performance, revenue and profit breakdowns by segment/geography/customers, and key operational KPIs.")
        schema_lines.append("   - **The 'Concall' summary contains extracted text** from the Q&A session, useful for management sentiment and specific queries.")
        schema_lines.append("   - This is the **primary source for all management commentary, financial results, KPIs, and future guidance.**")
        schema_lines.append("   - To retrieve this data, specify in your plan: `\"documents\": {\"retrieve\": true}`.")

    # 5. Peer Comparison Data
    peer_comparison_data = current_analysis_context.get("peer_comparison")
    if peer_comparison_data and isinstance(peer_comparison_data, list):
        schema_lines.append("\n**5. Peer Comparison (section_name: 'peer_comparison')**")
        schema_lines.append("   - A list of peer company dictionaries with financial metrics for competitive analysis.")
        schema_lines.append("   - Each peer has: ticker, name, cmp (current price), market_cap, pe_ratio, pb_ratio, dividend_yield, roce, roe, sales_growth_yoy, ebitda_growth_yoy, npm.")
        schema_lines.append("   - **Use this for comparative questions** like 'How does the company compare to peers?', 'Which peer has best ROE?', 'Is P/E higher than peers?'")
        schema_lines.append("   - To retrieve peer data, specify in your plan: `\"peer_comparison\": {\"retrieve\": true}`.")

    schema_lines.append("\n**General Instructions for AI Planner (Detailed in System Prompt):**")
    schema_lines.append("Your primary goal is to determine what data is needed (direct or for calculation) to answer the user.")
    schema_lines.append("You MUST use the exact names for tables, metrics, summary keys, and series names AS LISTED ABOVE in this dynamically generated schema when forming your `retrieve_data` plan.")
    schema_lines.append("You will output a JSON plan with `retrieve_data` and `perform_calculations` sections.")

    return "\n".join(schema_lines)


def retrieve_data_based_on_plan(plan_retrieve_data_section, full_context):
    """
    Retrieves specific data subsets from full_context based on the AI's JSON plan's 'retrieve_data' section.
    'full_context' is expected to be the 'last_analysis' dictionary.
    The structure of plan_retrieve_data_section is the same as the old 'plan' structure.
    """
    focused_data = {"retrieval_info": "Data selected according to AI plan's retrieve_data section."}
    
    if not full_context:
        focused_data["error"] = "Full context (last_analysis) is missing."
        return focused_data

    # The plan_retrieve_data_section is what used to be the whole plan.
    # e.g., plan_retrieve_data_section = {"summary": ..., "fundamentals": ...}
    plan = plan_retrieve_data_section 
    # print(f"DEBUG: (retrieve_data_based_on_plan) Plan for retrieval: {json.dumps(plan, indent=2)}")


    retrieved_something = False

    # Retrieve from Technical Summary (section_name: 'summary')
    if "summary" in plan and isinstance(plan["summary"], dict) and \
       "technical_summary_keys" in plan["summary"] and full_context.get("summary"):
        
        requested_keys = plan["summary"]["technical_summary_keys"]
        if requested_keys == "all":
            focused_data["summary_data_direct"] = full_context["summary"] # Store under a clear key
            retrieved_something = True
        elif isinstance(requested_keys, list):
            focused_data["summary_data_direct"] = [
                item for item in full_context["summary"] if item.get("key") in requested_keys
            ]
            if focused_data["summary_data_direct"]: retrieved_something = True
        # print(f"DEBUG: Retrieved Technical Summary Direct: {focused_data.get('summary_data_direct')}")


    # Retrieve from Fundamentals (section_name: 'fundamentals')
    if "fundamentals" in plan and isinstance(plan["fundamentals"], dict) and full_context.get("fundamentals"):
        focused_data["Fundamentals"] = {} # This is where table data will go
        for table_name, spec in plan["fundamentals"].items():
            if table_name in full_context["fundamentals"]:
                table_data = full_context["fundamentals"][table_name] 
                
                selected_rows_for_table = []
                requested_metrics = spec.get("metrics")

                if requested_metrics == "all":
                    selected_rows_for_table = [row for row in table_data if isinstance(row, dict)]
                elif isinstance(requested_metrics, list) and table_data:
                    selected_rows_for_table = [
                        row for row in table_data if isinstance(row, dict) and row.get("") in requested_metrics
                    ]
                
                if not selected_rows_for_table:
                    continue

                requested_periods = spec.get("periods")
                final_rows_for_table = []

                if requested_periods == "all" or not requested_periods:
                    final_rows_for_table = selected_rows_for_table
                elif isinstance(requested_periods, list) and selected_rows_for_table:
                    for row in selected_rows_for_table:
                        if not isinstance(row, dict):
                            continue
                        filtered_row = { "": row.get("") } 
                        if "index" in row: filtered_row["index"] = row["index"]
                        
                        all_period_keys_in_row = [k for k in row.keys() if k not in ["index", ""]]
                        
                        actual_periods_to_fetch_keys = set()
                        for period_req in requested_periods:
                            if isinstance(period_req, str) and period_req.lower().startswith("latest "):
                                try:
                                    n_latest = int(period_req.split(" ")[1])
                                    actual_periods_to_fetch_keys.update(all_period_keys_in_row[-n_latest:])
                                except ValueError:
                                    if period_req in all_period_keys_in_row:
                                        actual_periods_to_fetch_keys.add(period_req)
                            elif isinstance(period_req, int): # New: handle integer index for periods
                                num_available = len(all_period_keys_in_row)
                                actual_idx = -1
                                if period_req < 0: # Negative index
                                    if abs(period_req) <= num_available:
                                        actual_idx = num_available + period_req
                                elif period_req >= 0: # Positive index
                                    if period_req < num_available:
                                        actual_idx = period_req
                                
                                if 0 <= actual_idx < num_available:
                                    actual_periods_to_fetch_keys.add(all_period_keys_in_row[actual_idx])
                            elif period_req in all_period_keys_in_row: # Direct column name
                                actual_periods_to_fetch_keys.add(period_req)
                        
                        for p_key in actual_periods_to_fetch_keys:
                            if p_key in row:
                                filtered_row[p_key] = row[p_key]
                        final_rows_for_table.append(filtered_row)
                else: 
                    final_rows_for_table = selected_rows_for_table

                if final_rows_for_table:
                    focused_data["Fundamentals"][table_name] = final_rows_for_table
                    retrieved_something = True
        # print(f"DEBUG: Retrieved Fundamentals: {focused_data.get('Fundamentals')}")

    # Retrieve from Valuation & Margin Data Series (section_name: 'valuation_and_margin_data')
    if "valuation_and_margin_data" in plan and isinstance(plan["valuation_and_margin_data"], dict) and \
       full_context.get("valuation_and_margin_data"):
        
        focused_data["ValuationMarginSeries"] = {} # New key for clarity
        for series_name, spec in plan["valuation_and_margin_data"].items():
            if series_name in full_context["valuation_and_margin_data"]:
                series_data = full_context["valuation_and_margin_data"][series_name]
                
                requested_points_spec = spec.get("points")
                if requested_points_spec == "all":
                    focused_data["ValuationMarginSeries"][series_name] = series_data
                elif isinstance(requested_points_spec, str) and requested_points_spec.lower().startswith("latest "):
                    try:
                        n_points = int(requested_points_spec.split(" ")[1])
                        focused_data["ValuationMarginSeries"][series_name] = series_data[-n_points:]
                    except ValueError: 
                        focused_data["ValuationMarginSeries"][series_name] = series_data 
                else: 
                    focused_data["ValuationMarginSeries"][series_name] = series_data
                
                if focused_data["ValuationMarginSeries"].get(series_name):
                    retrieved_something = True
        # print(f"DEBUG: Retrieved Valuation Data Series: {focused_data.get('ValuationMarginSeries')}")

    # Retrieve from Documents (section_name: 'documents')
    if "documents" in plan and plan["documents"].get("retrieve") and full_context.get("documents"):
        focused_data["documents"] = full_context.get("documents")
        if focused_data["documents"]:
            retrieved_something = True
    # print(f"DEBUG: Retrieved Documents: {focused_data.get('documents')}") 

    # Retrieve from Peer Comparison (section_name: 'peer_comparison')
    if "peer_comparison" in plan and plan["peer_comparison"].get("retrieve") and full_context.get("peer_comparison"):
        focused_data["peer_comparison"] = full_context.get("peer_comparison")
        if focused_data["peer_comparison"]:
            retrieved_something = True

    if not retrieved_something and "error" not in focused_data :
        focused_data["retrieval_info"] = "AI plan's 'retrieve_data' section did not specify any known data sections or the requested data was not found."
        # print(f"WARN: No specific data retrieved based on AI plan's retrieve_data. Plan section was: {json.dumps(plan, indent=2)}")

    return focused_data


# =====================================================================
# END: Data Schema Description Function and RAG Function for AI Planner
# =====================================================================

# Orchestrator for calculations
def perform_planned_calculations(plan_calculations_section, retrieved_data, full_last_analysis_context):
    """
    Executes calculations specified in the AI's plan.
    plan_calculations_section: The list from plan['perform_calculations'].
    retrieved_data: Data fetched by retrieve_data_based_on_plan. This is where calculation inputs are sourced.
    full_last_analysis_context: The complete 'last_analysis' for things like current price from summary.
    """
    calculated_metrics_output = {}
    if not plan_calculations_section or not isinstance(plan_calculations_section, list):
        return {"info": "No calculations specified or invalid format.", "results": calculated_metrics_output}

    # print(f"DEBUG: (perform_planned_calculations) Calculation Plan Section: {json.dumps(plan_calculations_section, indent=2)}")
    # print(f"DEBUG: (perform_planned_calculations) Retrieved Data for Calcs: {json.dumps(retrieved_data, indent=2, default=str)}")


    for calc_spec in plan_calculations_section:
        calc_name = calc_spec.get("calculation_name")
        output_key = calc_spec.get("output_key_name", calc_name) # Default output key to calc_name
        
        if not calc_name or calc_name not in CALCULATION_REGISTRY:
            print(f"WARN: Unknown or missing calculation_name: {calc_name}")
            calculated_metrics_output[output_key] = {"error": f"Unknown calculation: {calc_name}"}
            continue

        calculation_function = CALCULATION_REGISTRY[calc_name]        

        try:
            result = calculation_function(retrieved_data, calc_spec)
            calculated_metrics_output[output_key] = result
        except Exception as e:
            print(f"ERROR: Executing calculation {calc_name} failed: {e}")
            traceback.print_exc()
            calculated_metrics_output[output_key] = {"error": f"Execution failed: {str(e)}"}
            
    return {"info": "Calculations performed.", "results": calculated_metrics_output}


# =====================================================================
# AI-ENHANCED NEWS ENDPOINT
# =====================================================================

AI_NEWS_INTENT_PROMPT = """Your role is to convert the user's message into an Enhanced Research Query that will be answered by Perplexity Sonar-Pro with Pro Search.

Your job: Analyze the user's question and conversation history, and convert user's question regarding any stock/company/industry/economy/policy/theme into a clear, complete, investing-grade research prompt that:
1. Captures the explicit ask
2. Captures the timeline and when it is not mentioned or vague assume to be last 3 days
3. Adds relevant financial context the user likely wants but didn't explicitly ask for
4. Maintain conversation continuity: use the provided conversation context to resolve references like “this company”, “that sector”, “the last one”, etc.
5. Do not store or request access to older queries beyond the provided conversation context. Treat each run as self-contained.

IMPORTANT RULES:
- Output ONLY the enhanced question(s), nothing else
- Include relevant aspects like: Trump's statements/actions related to India, tariff hikes/threats, price movements, latest news, earnings, analyst views, sector trends, risks, opportunities
- For generic queries, expand to cover the most useful financial insights
- Assume geography as India, unless explicitly specified; currency as INR unless explicitly specified, and investing style as growth unless explicitly specified.

HOW TO WRITE THE ENHANCED QUERY
The enhanced_query must:
1. Be a single, detailed research request suitable for Sonar-Pro with Pro Search. The request should not be more than 100 words long. 
2. If it's a company/stock query, request: "latest material developments", "earnings/guidance", "segment drivers", "regulatory issues", "competitive landscape", "valuation context (directional, not exact unless sourced)", and "near-term catalysts/tailwind/headwind".
3. If it's an industry/market/macro/Nifty/Sensex query, request: "recent tariff hikes/threats", "drivers", "recent news", "global cues", "Trump's statements/actions", "data points", "policy/regulation", "winners/losers", "second-order effects", "leading indicators", and "implications for listed Indian companies".
4. If user asks for "quick" or "summary", still generate a thorough query but request a concise output.
5. Ensure the Sonar-Pro query mentions that answer should be maximum 500 words

CONVERSATION CONTEXT USAGE
- Use prior assistant/user messages in the current thread to resolve pronouns and continuity.
- Do not invent prior conversations. If context is missing, state an assumption or ask one clarifier.

Examples:
- "RELIANCE news" → "What are the latest news, stock movements, earnings updates, management commentary, and analyst recommendations for Reliance Industries Ltd that could impact its stock price?"
- "IT sector" → "What is the current state of the Indian IT sector including major companies' performance, hiring trends, deal wins, revenue growth outlook, and key risks?"
- "market today" → "What happened in the Indian stock market today including Nifty and Sensex movements, sector performers, FII/DII activity, and key news driving the market?"
- "Tata Motors EV" → "What is Tata Motors' electric vehicle strategy, current EV sales performance, upcoming EV launches, market share, and how it compares to competitors?"
- "Nifty/Sensex/Market volatility" → "What are the recent tariff hikes/threats, drivers, recent news, global cues, Trump's statements/actions, data points, policy/regulation, winners/losers, second-order effects, leading indicators, and implications for listed Indian companies?"
"""

AI_NEWS_RESEARCH_PROMPT = """You are the world's most advanced financial news provider, who can answer any question about a company/stock/industry/sector/economy with latest developments, news, and analysis.

⚠️ MANDATORY OUTPUT FORMAT - READ THIS FIRST ⚠️
Provide comprehensive, detailed analysis in a readable format. Aim for clarity and depth.

Required format for EVERY response:
1. Start with a 2-3 sentence executive summary
2. Use ## headings to organize major sections
3. Under each heading, write detailed explanations using:
   - **Paragraphs** for narrative flow and explanations (2-3 sentences per point)
   - **Bullet points** (- or 1.) for breaking down complex information into key takeaways
   - **Tables** for comparisons and data-heavy sections
4. Target response length: 700-1000 words (adjust based on query complexity)

Example of CORRECT formatting (only formatting, not content):
---
Reliance Industries reported strong Q3FY24 results with consolidated revenue of ₹2,35,000 crore, marking a 12% YoY growth driven by retail and digital services expansion.

## Financial Performance

Reliance's consolidated revenue for Q3FY24 stood at ₹2,35,000 crore, up 12% YoY from ₹2,10,000 crore in Q3FY23. The growth was primarily driven by the retail segment which grew 18% YoY to ₹75,000 crore, while the O2C (Oil-to-Chemicals) segment saw a modest 5% growth to ₹1,20,000 crore due to margin pressures in petrochemicals.

Net profit increased 8% YoY to ₹18,500 crore, though margins compressed slightly from 8.2% to 7.9% due to higher operating costs and increased investments in digital infrastructure. EBITDA stood at ₹40,000 crore with a margin of 17%.

Key highlights:
- Retail segment added 800 new stores, bringing total count to 18,500 stores
- Jio subscriber base grew to 48 crore with ARPU improving to ₹182 per month
- Digital services revenue crossed ₹25,000 crore for the first time

## Outlook and Catalysts

Management guided for continued momentum in retail with plans to add 2,000 stores in FY25. The recently announced partnerships in renewable energy could create new revenue streams starting FY26...
---

Your goal is to provide detailed, accurate, and easy-to-understand answers about financial markets, stocks, industries, and the economy of the Indian market, while ensuring comprehensive coverage of recent developments.

DATA SOURCE RULES:
- **CRITICAL: If "Financial Data from Screener.in" is provided below in the context, you MUST use those exact numbers for any financial metrics about the mentioned companies. Do NOT approximate, estimate, or use outdated figures for companies with provided data.**
- **When quoting a number for a mentioned company, verify: "Is this from the provided screener table?" If yes, use the exact value from the table.**
- **If screener data conflicts with news sources, PRIORITIZE the screener table data as it is the verified source of truth for financial metrics.**
- For Indian company financial data (revenue, profit, ratios, quarterly results, market cap, stock price), ALWAYS use screener.in as the primary source
- When researching Indian stocks, search: "site:screener.in [company name]" for fundamental data
- For company news: prioritize filings/press releases/transcripts, then tier-1 financial media/wires; corroborate major breaking claims with 2 credible sources when possible.
- For macro/industry numbers: prefer RBI, MOSPI/NSO, SEBI, DPIIT, government releases, IBEF, then IMF/World Bank and reputed research reports.
- For general Nifty/Sensex-related questions, prefer general internet search to get the latest updates.
- Always anchor statements with dates ("as of <date>") and avoid "current" claims unless the source is real-time.

FORMATTING RULES (CRITICAL):
- Use simple, clear English accessible to retail investors
- **Mix paragraphs and bullets** - use paragraphs for explanations and context, bullets for key takeaways
- Break content into logical sections with ## headings
- Provide sufficient detail - each point should be well-explained (2-4 sentences when needed)
- Use tables for comparisons (markdown format: | Column | Column |)
- **Target 500-1000 words** - adjust based on question complexity, but prioritize clarity over brevity
- Use **bold** for emphasis on key metrics, findings, or section titles (but don't overuse)
- **Avoid acronyms** - spell out terms on first use, then use acronym in parentheses (e.g., "Foreign Institutional Investors (FII)"). After first definition, you may use the acronym.
- **Do NOT show the word count** at the end of the response.
- Do NOT include citations, source links, references, superscript, footnotes, and exponents




"""

@app.route('/ai-news', methods=['POST'])
def ai_news_chat():
    """
    AI-Enhanced News endpoint - the world's most advanced financial research engine.
    Uses two-stage processing:
    1. Intent Enhancement: GPT-5-mini deciphers hidden intent
    2. Research: Perplexity sonar-pro with Pro Search answers the enhanced query
    """
    try:
        data = request.get_json(force=True)
        user_question = data.get('question', '').strip()
        conversation_history = data.get('conversation_history', [])
        companies = data.get('companies', [])  # List of {ticker, name} from @mentions
        
        if not user_question:
            return jsonify({'error': 'No question provided'}), 400
        
        # Build company context string from @mentions
        company_context = ""
        if companies:
            company_context = "Companies mentioned: " + ", ".join([
                f"{c.get('name', '')} ({c.get('ticker', '')})" for c in companies
            ]) + "\n\n"
        
        print(f"INFO: AI-News received question: {user_question[:100]}...")
        if company_context:
            print(f"INFO: Company context: {company_context}")
        
        # =====================================================================
        # NEW: Fetch financial tables from screener.in for @mentioned companies
        # =====================================================================
        
        async def fetch_financial_tables_for_companies(companies_list):
            """
            Fetches financial tables from screener.in for @mentioned companies.
            Returns formatted markdown string with tables.
            """
            if not companies_list:
                return ""
            
            financial_data_sections = []
            
            for company in companies_list:
                ticker = company.get('ticker', '').strip()
                name = company.get('name', '').strip()
                
                if not ticker:
                    continue
                    
                try:
                    print(f"INFO: Fetching financial data for {ticker} from screener.in...")
                    # Fetch tables, description, and top ratios
                    tables, description, top_ratios, is_consolidated_flag = await fetch_consolidated_async(ticker)
                    
                    # Format into markdown
                    markdown = format_tables_to_markdown(ticker, name, tables)
                    financial_data_sections.append(markdown)
                    print(f"INFO: Successfully fetched data for {ticker}")
                    
                except Exception as e:
                    print(f"WARN: Could not fetch financial data for {ticker}: {e}")
                    continue
            
            if not financial_data_sections:
                return ""
            
            header = "### Financial Data from Screener.in\n"
            header += "**IMPORTANT: Use the following verified financial data when quoting numbers for the mentioned companies.**\n\n"
            
            return header + "\n\n---\n\n".join(financial_data_sections)
        
        def format_tables_to_markdown(ticker, name, tables):
            """
            Converts financial tables dict to clean markdown.
            Limits to most recent periods to keep prompt concise.
            """
            sections = [f"## {name} ({ticker})"]
            
            # Priority tables to include
            priority_tables = [
                "Quarterly Results",
                "Annual Results", 
                "Balance Sheet",
                "Financial Ratios",
                "Quarterly Shareholding Pattern"
            ]
            
            for table_name in priority_tables:
                if table_name not in tables:
                    continue
                    
                df = tables[table_name].copy()
                
                # Limit columns to recent periods to reduce token usage
                if table_name == "Quarterly Results":
                    # Keep first column (metric names) + latest 4 quarters
                    if len(df.columns) > 5:
                        df = df.iloc[:, :5]
                elif table_name == "Annual Results":
                    # Keep first column + latest 3 years
                    if len(df.columns) > 4:
                        df = df.iloc[:, :4]
                elif table_name == "Quarterly Shareholding Pattern":
                    # Keep first column + latest 4 quarters
                    if len(df.columns) > 5:
                        df = df.iloc[:, :5]
                else:
                    # For Balance Sheet and Ratios, keep first column + latest 2 periods
                    if len(df.columns) > 3:
                        df = df.iloc[:, :3]
                
                # Convert to markdown
                try:
                    markdown_table = df.to_markdown(index=False)
                    sections.append(f"### {table_name}\n{markdown_table}")
                except Exception as e:
                    print(f"WARN: Could not convert {table_name} to markdown: {e}")
                    continue
            
            return "\n\n".join(sections)
        
        # Fetch financial data if companies are mentioned
        financial_data_context = ""
        if companies:
            try:
                financial_data_context = asyncio.run(fetch_financial_tables_for_companies(companies))
                if financial_data_context:
                    print(f"INFO: Fetched financial data for {len(companies)} companies")
            except Exception as e:
                print(f"WARN: Failed to fetch financial data: {e}")
                financial_data_context = ""
        
        # =====================================================================
        # END: Financial data fetching
        # =====================================================================

        
        # --- STAGE 1: Intent Enhancement ---
        # Build context from conversation history
        history_context = ""
        if conversation_history:
            recent_history = conversation_history[-6:]  # Last 3 exchanges
            history_context = "\n".join([
                f"{'User' if msg['role'] == 'user' else 'Assistant'}: {msg['content'][:200]}"
                for msg in recent_history
            ])
        
        enhancement_messages = [
            {"role": "system", "content": AI_NEWS_INTENT_PROMPT},
            {"role": "user", "content": f"{company_context}Conversation History:\n{history_context}\n\nCurrent User Question: {user_question}"}
        ]
        
        print("INFO: Stage 1 - Enhancing user intent...")
        enhanced_question = call_generative_ai_model("gpt-5-mini", enhancement_messages, temperature=1)
        enhanced_question = enhanced_question.strip().strip('"')  # Clean up quotes if any
        print(f"INFO: Enhanced question: {enhanced_question[:2000]}")
        
        # --- STAGE 2: Research via Perplexity ---
        # NOTE: Perplexity API doesn't support system messages well - combine into user message
        research_prompt = AI_NEWS_RESEARCH_PROMPT + "\n\n"
        
        # Add conversation history for context continuity
        if conversation_history:
            research_prompt += "Previous Conversation:\n"
            for msg in conversation_history[-6:]:
                role_label = "User" if msg["role"] == "user" else "Assistant"
                research_prompt += f"{role_label}: {msg['content'][:300]}\n"
            research_prompt += "\n"
        
        # Inject financial data from screener.in
        if financial_data_context:
            research_prompt += financial_data_context + "\n\n"
            print(f"INFO: Added financial data to research prompt ({len(financial_data_context)} chars)")
        
        # Add the enhanced question
        research_prompt += f"Current Question: {enhanced_question}"
        
        research_messages = [{"role": "user", "content": research_prompt}]
        
        print("INFO: Stage 2 - Researching via Perplexity...")
        response = call_perplexity_api(
            research_messages, 
            model="sonar-pro", 
            temperature=1, 
            timeout=120,
            use_streaming=False,
            enable_pro_search=True
        )
        
        # Remove citation brackets like [1], [2], [123] from response
        response = re.sub(r'\[\d+\]', '', response)
        
        # Debug: Log first 500 chars of response before processing
        print(f"DEBUG: Response BEFORE bullet processing (first 500 chars):")
        print(repr(response[:500]))
        
        # Post-process: Convert paragraph-style response to bullet format
        # Sonar Pro ignores formatting instructions, so we force bullets here
        def convert_to_bullets(text):
            lines = text.split('\n')
            print(f"DEBUG: Split into {len(lines)} lines")
            result = []
            first_content_seen = False  # Track if we've seen the first content (summary)
            for i, line in enumerate(lines):
                stripped = line.strip()
                if i < 5:  # Debug first 5 lines
                    print(f"DEBUG: Line {i}: len={len(stripped)}, starts_with={'#' if stripped.startswith('#') else 'other'}")
                if not stripped:
                    result.append('')  # Keep empty lines
                elif stripped.startswith('#') or stripped.startswith('-') or stripped.startswith('*') or stripped.startswith('|'):
                    # Already a heading, bullet, or table - keep as-is
                    result.append(line)
                elif re.match(r'^\d+\.', stripped):
                    # Already a numbered list - keep as-is
                    result.append(line)
                elif len(stripped) > 30:
                    # Content line - check if it's the first one (summary)
                    if not first_content_seen:
                        # First content paragraph is the summary - keep as plain text
                        result.append(stripped)
                        first_content_seen = True
                    else:
                        # Subsequent content lines - convert to bullet
                        result.append('- ' + stripped)
                else:
                    # Short line (likely a heading without #) - make it a heading
                    result.append('## ' + stripped)
            return '\n'.join(result)
        
        response = convert_to_bullets(response)
        
        # Debug: Log first 500 chars after processing
        print(f"DEBUG: Response AFTER bullet processing (first 500 chars):")
        print(repr(response[:500]))
        
        print(f"INFO: AI-News response generated ({len(response)} chars)")
        
        return jsonify({
            'answer': response,
            'status': 'success'
        })
        
    except Exception as e:
        print(f"ERROR: AI-News endpoint failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': f'Research failed: {str(e)}',
            'status': 'error'
        }), 500


INDUSTRY_RESEARCH_PROMPT = '''You are a buy-side equity research analyst writing an investor-grade INDIA industry report for a GROWTH stock investor.

Fixed assumptions (do not ask the user):
- Geography: India (include exports from India and imports into India where relevant)
- Primary currency: INR; Secondary currency: USD (use USD mainly for global comps, commodities, trade, and FDI context)
- Primary demonination: crores (for INR); Secondary demonination: millions (for USD)
- Listed market focus: NSE & BSE
- Company universe: ALL publicly listed entities (including conglomerates/proxies; clearly label exposure)
- Risk tolerance: Medium to High (seek growth + rerating potential; accept some cyclicality but quantify it)

User inputs (only these vary):
- Industry: {INDUSTRY}
- Time horizon: {HORIZON_YEARS} years (default 5)
- Optional: specific companies/tickers to include: {OPTIONAL_TICKERS}
- Optional: preferred depth: {DEPTH} (default STANDARD)

Hard rules:
- Be specific and numbers-driven. Prefer ranges and scenarios over vague statements.
- Separate FACTS vs ASSUMPTIONS vs OPINIONS explicitly.
- Use the most recent data available; always state "Data as of: <month year>".
- Provide citations/sources for key claims (market size, growth, shares, regulation, FDI, pricing, input costs). If you cannot verify a number, write "Unknown", give a plausible range, and list what must be checked.
- Tie every section back to revenue growth, margin trajectory, ROIC/ROCE, cash flows, and valuation/rerating potential for Indian listed companies.
- Avoid fluff. If something doesn't impact investor outcomes, drop it.

OUTPUT FORMAT: Markdown with clear headings and tables. Start with a decisive executive summary.

========================================
1) Executive Summary (Investor Verdict)
========================================
- One-line verdict: "Structurally Attractive / Mixed / Unattractive" for a 3–5 year growth investor.
- 5 bullet "So what?" takeaways linking: growth drivers → pricing power → margins/ROIC → winners → risks.
- Profit pools: where value is created/captured in the value chain (highest ROIC pockets).
- Best ways to invest (NSE/BSE):
  - Top 3-5 listed picks (Growth-style) with 1-2 line rationale each.
  - 2-3 "optionalities" (smaller caps / emerging winners) if risk appetite allows.
- "Pickaxes vs Gold" upfront call: Is the best wealth-creation likely in the core industry or adjacent layers (upstream/downstream/enablers)? State which layer and why.
- Thesis breakers (Top 5) + early warning indicators.
- 6-18 month catalysts/headwinds (policy, capacity, price cycle, demand inflection, export tailwinds, tech shifts).

========================================
2) Industry Definition & Segmentation (India-first)
========================================
- Define the industry precisely (include/exclude).
- Segment revenue pools (product/service categories, customer segments, price tiers, geography within India).
- India vs global: what is uniquely Indian vs globally driven?

========================================
3) Market Size, Penetration, and Growth (History + Forecast)
========================================
- Current market size in INR and volume units (and USD where relevant).
- Historical growth: 5-10 year CAGR + key inflection points (policy, commodity cycle, tech, demand shocks).
- Forecast next {HORIZON_YEARS} years with Base/Bull/Bear:
  - Market size, CAGR, and key assumptions.
  - Penetration runway (if applicable): current penetration vs peers/China/US.
Table:
Year | Market size (INR) | YoY% | Key driver | Confidence (H/M/L)

========================================
4) Demand Engine (What grows it?)
========================================
- Demand drivers: income, demographics, urbanization, capex cycle, exports, regulation, substitution.
- Elasticity & pricing: discretionary vs non-discretionary; replacement/upgrade cycles.
- Customer power & concentration (B2B/B2C) and impact on margins.
- Cyclicality: sensitivity to GDP, rates, INR/USD, commodity prices.

========================================
5) Supply Engine (What constrains it?)
========================================
- Capacity landscape in India: utilization, constraints (raw material, energy, logistics, permits).
- Capex pipeline: who is adding capacity, how much, when it comes online.
- Demand vs supply balance: implications for pricing and margins for participants.

========================================
6) Competitive Landscape (Shares + Winners)
========================================
Provide a table:
Company (NSE/BSE) | Exposure type (Pure/Mixed/Proxy) | Est. % revenue from industry | Market share (if applicable) | 3Y growth | Margins | ROCE | Net debt/EBITDA | Moat | Notes
Include:
- Top participants and share estimates (with method/source).
- Fastest-growing participants and WHY (distribution, capacity, product, cost, tech).
- Most profitable participants and WHY (cost position, branding, regulation, scale, integration).

========================================
7) Value Chain Map (Upstream → Midstream → Downstream)
========================================
- Upstream industries + key suppliers (India/import dependence), concentration risk, alternative sourcing.
- Midstream/core processes: where value add occurs and key bottlenecks.
- Downstream industries + key customers/end markets and their health.
- Identify who holds bargaining power and where margins structurally sit.

========================================
8) "Pickaxes vs Gold" Allocation: Where to Invest in the Value Chain
========================================
Objective: Assess whether an investor is better off investing in upstream or downstream industries/companies (NSE/BSE listed) rather than the focal industry, using a "pickaxes vs gold" framework.

Do this in 4 parts:

A) Profit Pool & Pricing Power by Layer
- For each layer (Upstream / Core industry / Downstream / Enablers), rate:
  - Pricing power (High/Med/Low)
  - Margin stability (High/Med/Low)
  - ROIC durability (High/Med/Low)
  - Cyclicality exposure (High/Med/Low)
  - Disruption risk (High/Med/Low)
- Explain, in investor terms, where the structurally better economics likely sit and why.

B) When Upstream Wins vs When Downstream Wins (Rules of Thumb)
- List conditions under which upstream is the superior wealth creator (e.g., input scarcity, strong supplier concentration, commodity upcycle with tight supply, regulated pricing downstream, high switching costs).
- List conditions under which downstream is superior (e.g., strong branding/distribution moat, customer lock-in, value-added services, fragmented suppliers, ability to pass through costs, premiumization).
- Call out the current regime for India in this industry: upstream-favoring, downstream-favoring, or balanced—and why.

C) India Listed "Alternative Bets" (Investable)
- Provide a shortlist of NSE/BSE listed companies that represent:
  - Upstream beneficiaries ("shovel sellers")
  - Downstream beneficiaries ("distribution/consumption toll booths")
  - Enablers (logistics, testing/certification, software, capital goods, staffing, financing, infrastructure)
- For each alternative bet, specify:
  - Exposure type (Pure/Mixed/Proxy) + estimated percentage linkage to the focal industry
  - Why it benefits (mechanism)
  - Key KPI to track
  - Biggest risk to the alternative thesis

D) Recommendation: Best Risk-Adjusted Exposure
- Recommend the best layer to invest in (Upstream vs Core vs Downstream vs Enablers) for a Growth investor with medium–high risk tolerance.
- Provide 1-2 portfolio constructions:
  - "Conservative Growth": higher quality/less cyclical layer mix
  - "Aggressive Growth": higher beta/optionalities
- State what would change this recommendation (trigger points).

Include a summary table:
Layer | Why it wins | Typical winners | Typical losers | Best listed routes (examples) | Key KPIs | Risk flags

========================================
9) Input Cost & Margin Sensitivity (Critical for investors)
========================================
- Break down typical cost structure: raw materials, energy, labor, logistics, S&M, depreciation.
- Top 5-10 inputs: domestic vs imported; INR/USD sensitivity; hedging practices.
- Pass-through ability: contract vs spot; reset frequency.
- Sensitivity table (ranges ok):
Input +10% | EBITDA margin impact (bps) | Who is most/least protected (companies)

========================================
10) Unit Economics & ROIC Durability
========================================
- Unit economics (where applicable): CAC/LTV, payback, contribution margin, utilization leverage.
- Working capital dynamics: inventory/receivables/payables; cash conversion cycle.
- ROIC/ROCE drivers: why returns are high/low; sustainability of returns.

========================================
11) Regulation, Policy, and Compliance (India)
========================================
- Current framework: key regulators, licenses, tariffs/duties, price controls, standards, environmental norms.
- Recent changes (3-5 years) and real impact on industry structure and profitability.
- Potential upcoming changes: policy drafts, litigation/court risk, political direction.
- Investor impact: winners/losers + probability x impact assessment.
Add "Policy Watchlist" and "Compliance Cost" discussion.

========================================
12) Trade, FX, and Global Linkages (Exports/Imports)
========================================
- Imports: dependency, key source countries, duty structure, vulnerability.
- Exports: addressable markets, competitiveness, trade barriers, currency impact (INR/USD).
- How FX and global commodity cycles flow into Indian margins/realizations.

========================================
13) Foreign Capital & Ownership (FII/FDI/PE/VC)
========================================
- FDI trends: major projects/deals, where capital is going and why.
- FII trends: sector ownership patterns, flows, and sensitivity triggers.
- PE/VC and M&A: consolidation signals, typical multiples, strategic buyers.

========================================
14) Technology, Disruption, and Substitution Risk
========================================
- Tech shifts changing cost curves/product superiority/route-to-market.
- Substitute threats (imports, new materials, new business models).
- Time-to-disruption: near/medium/far + who is best positioned.

========================================
15) ESG, Litigation, and Hidden Risk Map
========================================
- Material ESG risks (emissions, water, safety, governance, product liability).
- Regulatory/litigation tail risks.
- Company preparedness differences and potential valuation impact.

========================================
16) Scenario Analysis (Base/Bull/Bear) + What to Track
========================================
For each scenario, quantify:
- Demand growth, pricing, input costs, utilization
- Expected impact on revenue growth, EBITDA margin, ROCE, and cash flows
Then provide:
- "5 KPIs to track quarterly" (industry + company level)
- "Early warning signals" (leading indicators)

========================================
17) Investable Conclusions (Growth Investor Playbook)
========================================
- Rank listed companies into:
  A) Top Picks (best growth + quality of growth + rerating odds)
  B) Watchlist (needs trigger/price)
  C) Avoid/Underweight (structural issues)
For each Top Pick include:
- Why it wins (2-3 bullets)
- Key catalysts/tailwind/headwind (6-18 months)
- Key risks + what would change your mind
- Valuation anchors: what multiple is justified and why (relative + historical bands if available)
- Preferred entry conditions (what you'd wait for / what confirms breakout)

========================================
18) Valuation Context & Rerating Framework
========================================
- Typical sector multiples (P/E, EV/EBITDA, P/B where relevant) and what drives them.
- Historical multiple bands (if available) and cycle positioning.
- What causes rerating vs derating (ROIC inflection, margin expansion, governance, policy, cycle turn).

========================================
19) Due Diligence Checklist (Actionable)
========================================
- 10-15 questions for management/channel checks specific to this industry.
- Data sources to verify (government, regulator, trade data, tenders, industry bodies, company filings).
- Common accounting red flags and how to detect them.

========================================
20) Appendix: Assumptions, Sources, Confidence
========================================
- List key assumptions and uncertainty areas.
- Sources & links grouped by: market sizing, regulation, trade, input prices, company shares.
- Confidence score for the overall verdict: High/Med/Low + why.

End with:
"If I could only track 3 things to validate this industry thesis over the next 12 months, they are: …"
'''

# Industry Research: Concurrent request limit
MAX_CONCURRENT_INDUSTRY_RESEARCH = 5

def get_active_industry_jobs_count():
    """Count how many industry research jobs are currently processing"""
    try:
        # We track active jobs using our INDUSTRY_JOB_PREFIX pattern
        # Since Flask-Caching doesn't support scan_iter, we maintain a simple counter approach
        # When jobs complete or error, they update status - we just need to return a safe default
        # The concurrent limit is a safety net, not a hard requirement
        
        # For now, we'll use a simpler approach - check a dedicated counter key
        active_count = cache.get("industry_research_active_count")
        if active_count is not None:
            return int(active_count)
        return 0
    except Exception as e:
        print(f"ERROR counting active jobs: {e}", file=sys.stderr)
        return 0  # Fail open - allow request if we can't count

def increment_active_industry_jobs():
    """Increment the active industry research job counter"""
    try:
        current = cache.get("industry_research_active_count") or 0
        cache.set("industry_research_active_count", int(current) + 1, timeout=3600)
    except Exception as e:
        print(f"ERROR incrementing active jobs: {e}", file=sys.stderr)

def decrement_active_industry_jobs():
    """Decrement the active industry research job counter"""
    try:
        current = cache.get("industry_research_active_count") or 0
        new_count = max(0, int(current) - 1)  # Don't go negative
        cache.set("industry_research_active_count", new_count, timeout=3600)
    except Exception as e:
        print(f"ERROR decrementing active jobs: {e}", file=sys.stderr)

@app.route('/industry-research', methods=['POST'])
def industry_research():
    """
    Endpoint for generating comprehensive industry research reports using sonar-deep-research.
    Uses background processing to handle long-running API calls (Azure has 230s timeout).
    Limits concurrent requests to protect Azure P1V3 resources.
    """
    print("DEBUG: Entering /industry-research endpoint...", file=sys.stderr)
    
    try:
        data = request.get_json(force=True)
        industry = data.get('industry', '').strip()
        horizon_years = data.get('horizon_years', 5)
        depth = data.get('depth', 'STANDARD')
        optional_tickers = data.get('optional_tickers', '')
        
        if not industry:
            return jsonify({'error': 'Industry is required'}), 400
        
        # Check concurrent request limit
        active_count = get_active_industry_jobs_count()
        print(f"INDUSTRY_RESEARCH_DEBUG: Active jobs: {active_count}/{MAX_CONCURRENT_INDUSTRY_RESEARCH}", file=sys.stderr)
        
        if active_count >= MAX_CONCURRENT_INDUSTRY_RESEARCH:
            print(f"INDUSTRY_RESEARCH_LIMIT: Request rejected - limit reached ({active_count} active)", file=sys.stderr)
            return jsonify({
                'error': 'capacity_limit',
                'message': f'Maximum {MAX_CONCURRENT_INDUSTRY_RESEARCH} concurrent research requests reached. Please try again in a few minutes.',
                'active_jobs': active_count,
                'max_concurrent': MAX_CONCURRENT_INDUSTRY_RESEARCH,
                'retry_after_seconds': 300  # Suggest retry after 5 minutes
            }), 429  # 429 Too Many Requests
        
        # Generate a unique job ID
        job_id = str(uuid.uuid4())
        
        # Initialize job status
        set_industry_job(job_id, {
            'status': 'processing',
            'progress': 'Starting deep research...',
            'result': None,
            'error': None,
            'industry': industry,
            'horizon_years': horizon_years,
            'depth': depth,
            'started_at': time.time()
        })
        
        # Increment active job counter
        increment_active_industry_jobs()
        
        # Start background thread for the long-running API call
        def run_research():
            try:
                job_start_time = time.time()
                print(f"INDUSTRY_RESEARCH_DEBUG: Job {job_id} started for industry '{industry}'", file=sys.stderr)
                log_progress(f"Starting deep research for {industry} industry...")
                
                # Build the prompt with user inputs
                prompt = INDUSTRY_RESEARCH_PROMPT.format(
                    INDUSTRY=industry,
                    HORIZON_YEARS=horizon_years,
                    OPTIONAL_TICKERS=optional_tickers if optional_tickers else "None specified",
                    DEPTH=depth
                )
                
                update_industry_job(job_id, {'progress': f"Generating comprehensive {depth} report for {industry}..."})
                print(f"INDUSTRY_RESEARCH_DEBUG: Calling sonar-deep-research for industry: {industry}, horizon: {horizon_years}y, depth: {depth}", file=sys.stderr)
                print(f"INDUSTRY_RESEARCH_DEBUG: Prompt length: {len(prompt)} chars", file=sys.stderr)
                
                # Call Perplexity's sonar-deep-research with extended timeout
                messages = [{"role": "user", "content": prompt}]
                
                # Progress callback - updates job status every 20s during streaming
                def streaming_progress(elapsed_seconds, bytes_received):
                    """Called automatically by call_perplexity_api during streaming"""
                    update_industry_job(job_id, {
                        'progress': f"Receiving research data... ({elapsed_seconds}s, {bytes_received//1024}KB received)",
                        'heartbeat': time.time(),
                        'status': 'processing'  # Ensure status is preserved
                    })
                    print(f"INDUSTRY_RESEARCH_HEARTBEAT: Job {job_id} alive - {elapsed_seconds}s, {bytes_received//1024}KB", file=sys.stderr)
                
                # Call with progress callback to keep job alive during long streaming
                report = call_perplexity_api(messages, model="sonar-deep-research", timeout=900, progress_callback=streaming_progress)
                
                elapsed_total = int(time.time() - job_start_time)
                print(f"INDUSTRY_RESEARCH_DEBUG: API call complete. Total time: {elapsed_total}s, Report length: {len(report)} chars", file=sys.stderr)
                
                # Store the result
                update_industry_job(job_id, {
                    'status': 'complete',
                    'progress': 'Industry report generation complete!',
                    'result': {
                        'report': report,
                        'industry': industry,
                        'horizon_years': horizon_years,
                        'depth': depth
                    },
                    'completed_at': time.time(),
                    'total_time': elapsed_total
                })
                print(f"INDUSTRY_RESEARCH_DEBUG: Job {job_id} completed successfully in {elapsed_total}s", file=sys.stderr)
                
                # Decrement active job counter
                decrement_active_industry_jobs()
                
            except Exception as api_error:
                elapsed = int(time.time() - job_start_time)
                print(f"INDUSTRY_RESEARCH_ERROR: Job {job_id} failed after {elapsed}s: {type(api_error).__name__}: {api_error}", file=sys.stderr)
                import traceback
                traceback.print_exc(file=sys.stderr)
                update_industry_job(job_id, {
                    'status': 'error',
                    'error': str(api_error),
                    'error_type': type(api_error).__name__,
                    'failed_at': time.time(),
                    'elapsed': elapsed
                })
                
                # Decrement active job counter even on error
                decrement_active_industry_jobs()
        
        # Start the background thread
        thread = threading.Thread(target=run_research)
        thread.daemon = True
        thread.start()
        
        # Return the job ID immediately (within Azure's timeout)
        return jsonify({
            'job_id': job_id,
            'status': 'processing',
            'message': f'Research job started for {industry}. Poll /industry-research/{job_id}/status for results.'
        })
            
    except Exception as e:
        print(f"CRITICAL ERROR in /industry-research: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500


@app.route('/industry-research/<job_id>/status', methods=['GET'])
def industry_research_status(job_id):
    """
    Poll endpoint to check status of a background industry research job.
    Returns the result when complete.
    """
    # Enhanced debugging for multi-instance Azure issues
    instance_id = socket.gethostname()
    pid = os.getpid()
    
    print(f"STATUS_DEBUG: Instance={instance_id}, PID={pid}, JobID={job_id}", file=sys.stderr)
    print(f"STATUS_DEBUG: LOCAL_INDUSTRY_JOBS has {len(LOCAL_INDUSTRY_JOBS)} jobs: {list(LOCAL_INDUSTRY_JOBS.keys())[:5]}", file=sys.stderr)
    
    job = get_industry_job(job_id)
    
    if not job:
        # Additional debugging when job not found
        print(f"STATUS_ERROR: Job {job_id} NOT FOUND on instance {instance_id}", file=sys.stderr)
        print(f"STATUS_ERROR: Redis check - attempting direct cache.get...", file=sys.stderr)
        try:
            direct_check = cache.get(f"{INDUSTRY_JOB_PREFIX}{job_id}")
            print(f"STATUS_ERROR: Direct Redis check result: {type(direct_check)}, is None: {direct_check is None}", file=sys.stderr)
        except Exception as redis_err:
            print(f"STATUS_ERROR: Direct Redis check FAILED: {redis_err}", file=sys.stderr)
        
        return jsonify({'error': 'Job not found or expired'}), 404
    
    print(f"STATUS_DEBUG: Job {job_id} FOUND with status={job.get('status')}", file=sys.stderr)
    
    if job['status'] == 'processing':
        return jsonify({
            'status': 'processing',
            'progress': job['progress'],
            'elapsed_seconds': int(time.time() - job['started_at'])
        })
    
    elif job['status'] == 'complete':
        # Return the result and clean up
        result = job['result']
        # Keep job for 5 minutes after completion, then it can be garbage collected
        return jsonify({
            'status': 'complete',
            **result
        })
    
    elif job['status'] == 'error':
        return jsonify({
            'status': 'error',
            'error': job['error']
        }), 500
    
    return jsonify({'error': 'Unknown job status'}), 500

# =====================================================================
# END: INDUSTRY RESEARCH ENDPOINT
# =====================================================================

@app.route('/chat', methods=['POST'])
def chat():
    log_redis_target(app, cache, context="chat_entry")
    print("DEBUG: Entering /chat endpoint...", file=sys.stderr)
    
    try:
        data = request.get_json(force=True)
        user_question = data.get('question', '').strip()
        selected_model = data.get('model', 'o4-mini')
        # analysis_key = data.get('analysis_key')
        analysis_key = str(data.get('analysis_key')) if data.get('analysis_key') is not None else None
        
        # Get company context for better responses
        company_name = data.get('company_name', '').strip()
        ticker = data.get('ticker', '').strip()


        if not analysis_key:
            return jsonify({'answer': 'Analysis key is missing. Please analyze a stock first.'}), 200

        # Quick connectivity check if we're using a Redis backend
        cache_backend = getattr(cache, "cache", None)
        if cache_backend and hasattr(cache_backend, "ping"):
            try:
                cache_backend.ping()
                print("DEBUG: Redis cache ping succeeded before retrieval.", file=sys.stderr)
            except Exception as ping_err:
                print(f"WARN: Redis cache ping failed: {ping_err}", file=sys.stderr)

        # --- ROBUST RETRIEVAL LOGIC WITH RETRIES AND FALLBACK ---
        # Use Flask-Caching for both read and write paths so key prefixes/serialization stay aligned
        last_analysis = None
        retry_count = 0
        max_retries = 5  # Increased from 3 to give more time for race condition
        
        while retry_count < max_retries:
            try:
                print(f"DEBUG: Fetching cached analysis via Flask-Caching (Attempt {retry_count+1}).", file=sys.stderr)
                cached_blob = cache.get(analysis_key)

                # --- COMMON DECOMPRESSION LOGIC ---
                if cached_blob:
                    try:
                        # Try to decompress (zlib)
                        decompressed_data = zlib.decompress(cached_blob)
                        last_analysis = pickle.loads(decompressed_data)
                        print("DEBUG: Successfully retrieved and decompressed data.", file=sys.stderr)
                        break # Success!
                    except Exception as unpack_error:
                        print(f"WARN: Decompression failed: {unpack_error}", file=sys.stderr)
                        # Fallback: Maybe it wasn't compressed?
                        last_analysis = cached_blob
                        break
                else:
                    print(f"DEBUG: Cache returned None (Key not found). Waiting before retry...", file=sys.stderr)
                    # FIX: Add delay on cache miss to handle race condition after /analyze
                    time.sleep(0.5)  # 500ms delay between retries
            
            except Exception as e:
                print(f"WARN: Cache fetch failed (Attempt {retry_count+1}). Error: {e}", file=sys.stderr)
                time.sleep(1)
            
            retry_count += 1
        
        # =====================================================
        # FALLBACK 1: Try Redis stock cache if analysis_key cache expired
        # =====================================================
        # This handles the case where analysis_key TTL (6h) expired but stock cache still has data
        if not last_analysis and ticker:
            print(f"DEBUG: Primary cache miss. Trying FALLBACK 1: Redis stock cache for {ticker}...", file=sys.stderr)
            try:
                stock_cache_key = f"stock_analysis_{ticker.upper()}"
                stock_blob = cache.get(stock_cache_key)
                if stock_blob:
                    if isinstance(stock_blob, bytes):
                        last_analysis = pickle.loads(zlib.decompress(stock_blob))
                    else:
                        last_analysis = stock_blob
                    print(f"DEBUG: FALLBACK 1 SUCCEEDED - Redis stock cache for {ticker}.", file=sys.stderr)
            except Exception as fallback_err:
                print(f"WARN: FALLBACK 1 FAILED - Redis stock cache: {fallback_err}", file=sys.stderr)
        
        # =====================================================
        # FALLBACK 2: Try LOCAL MEMORY cache (Redis-free fallback)
        # =====================================================
        # This works even when Redis is completely unavailable
        if not last_analysis and ticker:
            print(f"DEBUG: Redis unavailable. Trying FALLBACK 2: Local memory cache for {ticker}...", file=sys.stderr)
            local_data = get_local_cache(ticker)
            if local_data:
                last_analysis = local_data
                print(f"DEBUG: FALLBACK 2 SUCCEEDED - Local memory cache for {ticker}.", file=sys.stderr)
            else:
                print(f"DEBUG: FALLBACK 2 FAILED - No local cache for {ticker}.", file=sys.stderr)
            
        # =====================================================
        # PART 2: VALIDATION
        # =====================================================
        if not last_analysis or not isinstance(last_analysis, dict) or not last_analysis.get("ticker"):
            print("ERROR: Context unavailable after fetch.", file=sys.stderr)
            return jsonify({'answer': 'Context data unavailable (Cache Miss or Network Timeout). Please re-analyze the stock.'}), 200

        # Log success size
        size_kb = sys.getsizeof(str(last_analysis)) / 1024
        print(f"INFO: Chat Data Loaded. Size: {size_kb:.2f} KB. Keys: {list(last_analysis.keys())}", file=sys.stderr)
        
        # =====================================================
        # PART 2B: RECONSTRUCT FUNDAMENTALS IF FROM FRONTEND CACHE
        # =====================================================
        # When loaded from Redis stock cache (FALLBACK 1), fundamentals is in frontend
        # format: dict of JSON strings from df.reset_index().T.to_json(orient='split').
        # The AI chatbot needs list-of-dicts format. Detect and convert if needed.
        cached_fundamentals = last_analysis.get("fundamentals", {})
        if cached_fundamentals and isinstance(cached_fundamentals, dict):
            first_table_value = next(iter(cached_fundamentals.values()), None)
            if isinstance(first_table_value, str):
                # Frontend format detected — reconstruct to list-of-dicts
                print("DEBUG: Fundamentals in frontend JSON format. Reconstructing for AI...", file=sys.stderr)
                import pandas as _pd
                reconstructed_fundamentals = {}
                for table_name, json_str in cached_fundamentals.items():
                    try:
                        # Reverse the encoding: pd.read_json(orient='split').T gives back the original df
                        df_table = _pd.read_json(json_str, orient='split').T
                        # reset_index() moves the metric names from index to a column named "index"
                        df_reset = df_table.reset_index()
                        # Rename "index" column to "" to match the AI context format
                        # (Screener clean_df renames "Unnamed: 0" to "", so metric key is always "")
                        df_reset.columns = ["" if c == "index" else c for c in df_reset.columns]
                        # Convert to list-of-dicts (same as fund_data_for_ai_context)
                        rows = df_reset.to_dict(orient='records')
                        # Clean up: strip whitespace from string keys and values
                        cleaned_rows = []
                        for row_dict in rows:
                            cleaned = {}
                            for k, v in row_dict.items():
                                clean_key = str(k).strip() if isinstance(k, str) else k
                                clean_val = str(v).strip() if isinstance(v, str) else v
                                cleaned[clean_key] = clean_val
                            cleaned_rows.append(cleaned)
                        reconstructed_fundamentals[table_name] = cleaned_rows
                    except Exception as recon_err:
                        print(f"WARN: Could not reconstruct table '{table_name}': {recon_err}", file=sys.stderr)
                last_analysis["fundamentals"] = reconstructed_fundamentals
                print(f"DEBUG: Reconstructed {len(reconstructed_fundamentals)} fundamental tables for AI.", file=sys.stderr)
        
        # =====================================================
        # PART 2C: NORMALIZE PEER COMPARISON FORMAT
        # =====================================================
        # Frontend cache stores peer_comparison as {company: {...}, peers: [...]}
        # The AI schema and retrieval functions expect a flat list of peer dicts.
        peer_data = last_analysis.get("peer_comparison")
        if isinstance(peer_data, dict) and "peers" in peer_data:
            last_analysis["peer_comparison"] = peer_data.get("peers", [])
            print(f"DEBUG: Normalized peer_comparison from nested dict to flat list ({len(last_analysis['peer_comparison'])} peers).", file=sys.stderr)
        
        # =====================================================
        # PART 2D: ENSURE VALUATION DATA IS ACCESSIBLE
        # =====================================================
        # If valuation_and_margin_data is missing but scanx_data exists (legacy light cache key),
        # copy it over so the AI schema and retrieval functions can find it.
        if not last_analysis.get("valuation_and_margin_data") and last_analysis.get("scanx_data"):
            scanx = last_analysis["scanx_data"]
            if isinstance(scanx, dict) and scanx:
                last_analysis["valuation_and_margin_data"] = scanx
                print(f"DEBUG: Copied scanx_data to valuation_and_margin_data ({len(scanx)} series).", file=sys.stderr)
        
        print(f"AI chatbot received question with selected model: {selected_model}", file=sys.stderr)

        # =====================================================
        # PART 3: AI BRAIN Logic (Preserved from your code)
        # =====================================================
        
        is_best_mode = selected_model in ('best', 'best-deep-research')
        is_best_fast_mode = selected_model == 'best-fast'
        is_deep_research_mode = selected_model == 'best-deep-research'
        central_brain_plan = None
        parsed_plan = {}
        ai_plan_json_str = "{}"
        thought_process_str = "No thought process generated."

        if is_best_fast_mode:
            # --- FAST MODE: MERGED STAGE 1+2 ---
            # Combines Central Brain + Tactical Planner into ONE GPT-5-mini call
            log_progress("Fast Mode: Unified Planner creating strategy & data plan...")
            print("INFO: Fast Mode - Merged Planner running...", file=sys.stderr)
            fast_start_time = time.time()
            
            from prompts import get_merged_planner_prompt
            merged_prompt = get_merged_planner_prompt()
            schema_description = get_data_schema_description(last_analysis)
            
            merged_user_content = (
                f"**Company:** {company_name} ({ticker})\n\n"
                f"**User Question:** \"{user_question}\"\n\n"
                f"**Data Schema Description:**\n{schema_description}\n\n"
                f"**Summary Context:**\n{str(last_analysis.get('summary', ''))[:2000]}"
            )
            
            merged_messages = [
                {"role": "system", "content": merged_prompt},
                {"role": "user", "content": merged_user_content}
            ]
            
            merged_response_str = call_generative_ai_model("gemini-3-flash-preview", merged_messages, temperature=1, thinking_level='HIGH')
            
            try:
                # Try to parse JSON (may or may not have code blocks)
                json_match = re.search(r"```json\s*([\s\S]*?)\s*```", merged_response_str, re.MULTILINE)
                if json_match:
                    merged_plan = json.loads(json_match.group(1))
                else:
                    merged_plan = json.loads(merged_response_str)
                
                # Extract Central Brain-like parts for downstream compatibility
                central_brain_plan = {
                    "thought_process": merged_plan.get("thought_process", ""),
                    "user_intent": merged_plan.get("user_intent", ""),
                    "peripheral_questions": merged_plan.get("peripheral_questions", []),
                    "agent_directives": merged_plan.get("agent_directives", [])
                }
                thought_process_str = merged_plan.get("thought_process", "Fast mode planning complete.")
                
                # Extract Tactical Plan parts
                parsed_plan = {
                    "retrieve_data": merged_plan.get("retrieve_data", {}),
                    "perform_calculations": merged_plan.get("perform_calculations", []),
                    "fetch_external_news": merged_plan.get("fetch_external_news", {})
                }
                ai_plan_json_str = json.dumps(parsed_plan, indent=2)
                
                fast_elapsed = time.time() - fast_start_time
                print(f"INFO: Fast Mode - Merged Planner completed in {fast_elapsed:.1f}s", file=sys.stderr)
                print(f"--- Merged Plan ---\n{json.dumps(merged_plan, indent=2)}\n--------------------------", file=sys.stderr)
                log_progress(f"✓ Strategy & data plan ready ({fast_elapsed:.1f}s)")
                
            except (json.JSONDecodeError, AttributeError) as e:
                print(f"ERROR: Could not parse Merged Plan: {e}", file=sys.stderr)
                print(f"Raw response: {merged_response_str[:1000]}", file=sys.stderr)
                return jsonify({'error': 'Failed to create unified strategic plan.'}), 500

        elif is_best_mode:
            # --- STANDARD BEST MODE: STAGE 1 + STAGE 2 ---
            # --- STAGE 1: CENTRAL BRAIN ---
            log_progress("Central Brain is analyzing the query and forming a strategy...")
            print("INFO: Central Brain running...", file=sys.stderr)
            stage1_start_time = time.time()
            
            central_brain_prompt = get_central_brain_prompt()
            brain_messages = [
                {"role": "system", "content": central_brain_prompt},
                {"role": "user", "content": (f"Company: {company_name} ({ticker})\n\n" if company_name and ticker else "") + f"User Question: \"{user_question}\""}
            ]
            
            central_brain_response_str = call_generative_ai_model("gpt-5-mini", brain_messages, temperature=1)
            stage1_elapsed = time.time() - stage1_start_time

            try:
                json_match = re.search(r"```json\s*([\s\S]*?)\s*```", central_brain_response_str, re.MULTILINE)
                if json_match:
                    central_brain_plan = json.loads(json_match.group(1))
                else:
                    central_brain_plan = json.loads(central_brain_response_str)

                thought_process_str = central_brain_plan.get("thought_process", "Central Brain planning complete.")
                print(f"INFO: ✓ Central Brain completed in {stage1_elapsed:.1f}s", file=sys.stderr)
                log_progress(f"✓ Strategic plan ready ({stage1_elapsed:.1f}s)")
                print(f"--- Central Brain Plan ---\n{json.dumps(central_brain_plan, indent=2)}\n--------------------------", file=sys.stderr)
                
            except (json.JSONDecodeError, AttributeError) as e:
                print(f"ERROR: Could not parse Central Brain plan: {e}", file=sys.stderr)
                return jsonify({'error': 'Failed to generate a strategic plan.'}), 500

            # --- STAGE 2: TACTICAL PLANNER ---
            log_progress("Tactical Planner is creating a detailed data retrieval plan...")
            print("INFO: Tactical Planner running...", file=sys.stderr)
            stage2_start_time = time.time()
            
            schema_description = get_data_schema_description(last_analysis)
            planning_system_prompt = get_planning_system_prompt()
            
            planner_user_content = (
                f"User Question: \"{user_question}\"\n\n"
                f"Central Brain Directives:\n{json.dumps(central_brain_plan, indent=2)}\n\n"
                f"Data Schema Description:\n{schema_description}\n\n"
                f"Full 'last_analysis' context (summary):\n{str(last_analysis.get('summary', ''))[:2000]}"
            )

            planner_messages = [
                {"role": "system", "content": planning_system_prompt},
                {"role": "user", "content": planner_user_content}
            ]
            
            tactical_plan_response_str = call_openai_api(planner_messages, model='o4-mini', expect_json_format_flag=True, temperature=1)
            stage2_elapsed = time.time() - stage2_start_time
            
            try:
                json_match = re.search(r"```json\s*([\s\S]*?)\s*```", tactical_plan_response_str, re.MULTILINE)
                if json_match:
                    ai_plan_json_str = json_match.group(1)
                    parsed_plan = json.loads(ai_plan_json_str)
                else:
                    parsed_plan = json.loads(tactical_plan_response_str)
                    ai_plan_json_str = tactical_plan_response_str

                print(f"INFO: ✓ Tactical Planner completed in {stage2_elapsed:.1f}s", file=sys.stderr)
                log_progress(f"✓ Tactical plan ready ({stage2_elapsed:.1f}s)")
                print(f"INFO: Stage 1+2 Total: {stage1_elapsed + stage2_elapsed:.1f}s", file=sys.stderr)
                print(f"--- Tactical Execution Plan ---\n{json.dumps(parsed_plan, indent=2)}\n--------------------------", file=sys.stderr)

            except json.JSONDecodeError as e:
                print(f"ERROR: Could not parse Tactical Plan: {e}", file=sys.stderr)
                return jsonify({'error': 'Failed to create a detailed execution plan.'}), 500

        else: 
            # Standard, single-agent flow
            log_progress("Creating a plan to answer the user's question...")
            schema_description = get_data_schema_description(last_analysis)
            planning_system_prompt = get_planning_system_prompt()
            planning_messages = [
                {"role": "system", "content": planning_system_prompt},
                {"role": "user", "content": f"User Question: \"{user_question}\"\n\nData Schema Description:\n{schema_description}\n\nFull 'last_analysis' context:\n{str(last_analysis.get('summary', ''))[:2000]}"}
            ]
            
            full_response_str = call_openai_api(planning_messages, model='o4-mini', expect_json_format_flag=False, temperature=1)
            
            try:
                json_match = re.search(r"```json\s*([\s\S]*?)\s*```", full_response_str, re.MULTILINE)
                thought_process_str = re.split(r"```json", full_response_str)[0].replace("**Thought Process:**", "").strip()
                if json_match:
                    ai_plan_json_str = json_match.group(1)
                    parsed_plan = json.loads(ai_plan_json_str)
                else:
                    raise ValueError("No JSON plan found in planner response.")
            except Exception as e:
                 print(f"ERROR: Could not parse standard plan: {e}", file=sys.stderr)
                 return jsonify({'error': 'Failed to create a standard execution plan.'}), 500

        # --- STAGE 3: EXECUTION (PARALLELIZED) ---
        log_progress("Executing plan: Fetching news & consulting agents in parallel...")
        print("INFO: Execution Stage (PARALLEL)...", file=sys.stderr)
        stage3_start_time = time.time()
        
        # =====================================================================
        # PARALLEL AGENT EXECUTION: News, ARA, EIA run concurrently
        # =====================================================================
        
        # --- Prepare agent activation flags and directives ---
        news_plan = parsed_plan.get("fetch_external_news", {})
        news_needed = news_plan.get("needed", False)
        sonar_prompt = news_plan.get("prompt_for_sonar", f"Get the latest news for {last_analysis.get('ticker')}")
        
        use_analyst_reports = data.get('use_analyst_reports', False)
        ara_directive = None
        eia_directive = None
        if central_brain_plan and 'agent_directives' in central_brain_plan:
            for directive in central_brain_plan.get('agent_directives', []):
                if directive.get('agent_name') == 'ARA':
                    ara_directive = directive.get('directive', '')
                elif directive.get('agent_name') == 'EIA':
                    eia_directive = directive.get('directive', '')
        
        ara_needed = bool(ara_directive or use_analyst_reports)
        eia_needed = bool(eia_directive)
        
        # Log which agents will run
        agents_running = []
        if news_needed: agents_running.append("MIA (News)")
        if ara_needed: agents_running.append("ARA (Analyst)")
        if eia_needed: agents_running.append("EIA (Earnings)")
        print(f"INFO: Starting parallel agents: {', '.join(agents_running) if agents_running else 'None'}", file=sys.stderr)
        
        # --- Define async wrapper functions for each agent ---
        async def fetch_news_async():
            """Fetch real-time news via Perplexity API"""
            if not news_needed:
                return None
            try:
                news_messages = [{"role": "user", "content": sonar_prompt}]
                if is_deep_research_mode:
                    result = await asyncio.to_thread(call_perplexity_api, news_messages, "sonar-deep-research", 1, 600)
                else:
                    result = await asyncio.to_thread(call_perplexity_api, news_messages, "sonar-pro")
                elapsed = time.time() - stage3_start_time
                print(f"INFO: ✓ MIA (News) completed in {elapsed:.1f}s", file=sys.stderr)
                log_progress(f"✓ News fetched ({elapsed:.1f}s)")
                return result
            except Exception as e:
                print(f"ERROR: News fetching failed: {e}", file=sys.stderr)
                return f"Error: Failed to fetch real-time news. {e}"
        
        async def run_ara_async():
            """Run Analyst Report Agent via Gemini API"""
            if not ara_needed:
                return None
            try:
                # Get cached analyst PDF texts
                analyst_texts = None
                cached_texts = cache.get(f"{analysis_key}_analyst_texts")
                if cached_texts:
                    if isinstance(cached_texts, bytes):
                        analyst_texts = pickle.loads(zlib.decompress(cached_texts))
                    else:
                        analyst_texts = cached_texts
                
                if not analyst_texts or len(analyst_texts) == 0:
                    print("WARN: ARA activated but no analyst texts found in cache", file=sys.stderr)
                    return "Analyst report texts are not yet available. They may still be loading in the background."
                
                # Build context from analyst report texts
                ara_context = f"**User Question:** {user_question}\n\n"
                ara_context += f"**Company:** {company_name} ({ticker})\n\n"
                ara_context += "**Available Analyst Research Reports:**\n\n"
                
                for i, report in enumerate(analyst_texts, 1):
                    ara_context += f"--- REPORT {i}: {report.get('brokerage', 'Unknown')} ---\n"
                    ara_context += f"Date: {report.get('date', 'N/A')}\n"
                    ara_context += f"Recommendation: {report.get('recommendation', 'N/A')}\n"
                    ara_context += f"Target Price: {report.get('target_price', 'N/A')}\n"
                    ara_context += f"Upside: {report.get('upside', 'N/A')}\n\n"
                    pdf_text = report.get('pdf_text', '')[:15000]
                    ara_context += f"**Report Content:**\n{pdf_text}\n\n"
                
                if ara_directive:
                    ara_prompt = f"""You are the Analyst Report Agent (ARA). Your task is to study the brokerage research reports provided below and answer the user's question based on analyst insights.

**Your Directive from Central Brain:**
{ara_directive}

{ara_context}

**Instructions:**
- Focus ONLY on answering based on what the analysts have written
- Cite which brokerage said what, along with the report publish date (attribution is important)
- If two or more analyst/brokerage reports have wildly differing views, state all views and reason a likely best answer using reasoning
- Provide your answer in plain text paragraphs (not structured HTML)
- Be concise but comprehensive
- If the reports don't contain information to answer the question, say so clearly
"""
                else:
                    ara_prompt = f"""You are the Analyst Report Agent (ARA). Study the brokerage research reports below and provide relevant insights for the user's question.

{ara_context}

**Instructions:**
- Summarize what analysts think about this stock relevant to the user's question
- Include target prices and recommendations from different brokerages
- Cite which brokerage said what, along with the report publish date (attribution is important)
- If two or more analyst/brokerage reports have wildly differing views, state all views and reason a likely best answer using reasoning
- Provide answer in plain text paragraphs
"""
                
                ara_messages = [{"role": "user", "content": ara_prompt}]
                result = await asyncio.to_thread(call_gemini_api, ara_messages, "gemini-3-flash-preview", 1, False, 'HIGH')
                elapsed = time.time() - stage3_start_time
                print(f"INFO: ✓ ARA (Analyst) completed in {elapsed:.1f}s ({len(result)} chars)", file=sys.stderr)
                log_progress(f"✓ Analyst insights ready ({elapsed:.1f}s)")
                return result
            except Exception as e:
                print(f"ERROR: ARA execution failed: {e}", file=sys.stderr)
                return f"Error consulting analyst reports: {e}"
        
        async def run_eia_async():
            """Run Earnings Intelligence Agent via OpenAI API"""
            if not eia_needed:
                return None
            try:
                documents = last_analysis.get('documents', [])
                concall_docs = [d for d in documents if d.get('type') == 'Concall']
                
                if not concall_docs:
                    print("WARN: EIA activated but no concall documents found", file=sys.stderr)
                    return "No concall transcript available for analysis."
                
                eia_context = f"**User Question:** {user_question}\n\n"
                eia_context += f"**Company:** {company_name} ({ticker})\n\n"
                eia_context += "**Concall Transcript(s):**\n\n"
                
                for doc in concall_docs:
                    eia_context += f"--- {doc.get('text', 'Concall Transcript')} ---\n"
                    content = doc.get('content_summary', '')
                    if content:
                        eia_context += content[:30000] + "\n\n"
                
                eia_prompt = f"""
{eia_context}

**Central Brain Directive:** {eia_directive}

**Instructions:**
- Analyze the concall transcript to answer the directive above
- Focus on management's tone, specific statements, and forward-looking guidance
- Identify any concerning language, evasive answers, or overpromising
- Note any discrepancies between what management says vs the numbers
- Provide your answer in plain text paragraphs, citing specific quotes where relevant
"""
                
                eia_messages = [{"role": "user", "content": eia_prompt}]
                # Use Gemini Flash for best-fast mode, GPT-5-mini otherwise
                if is_best_fast_mode:
                    result = await asyncio.to_thread(call_gemini_api, eia_messages, 'gemini-3-flash-preview', 1, False, 'HIGH')
                else:
                    result = await asyncio.to_thread(call_openai_api, eia_messages, 'gpt-5-mini', False, 1, 180)
                elapsed = time.time() - stage3_start_time
                print(f"INFO: ✓ EIA (Earnings) completed in {elapsed:.1f}s ({len(result)} chars)", file=sys.stderr)
                log_progress(f"✓ Earnings analysis ready ({elapsed:.1f}s)")
                return result
            except Exception as e:
                print(f"ERROR: EIA execution failed: {e}", file=sys.stderr)
                return f"Error analyzing concall transcript: {e}"
        
        # --- Run all agents in parallel ---
        async def run_parallel_agents():
            return await asyncio.gather(
                fetch_news_async(),
                run_ara_async(),
                run_eia_async(),
                return_exceptions=True
            )
        
        news_summary, ara_response, eia_response = asyncio.run(run_parallel_agents())
        
        # Handle any exceptions that were returned
        if isinstance(news_summary, Exception):
            print(f"ERROR: News agent raised exception: {news_summary}", file=sys.stderr)
            news_summary = f"Error: {news_summary}"
        if isinstance(ara_response, Exception):
            print(f"ERROR: ARA agent raised exception: {ara_response}", file=sys.stderr)
            ara_response = f"Error: {ara_response}"
        if isinstance(eia_response, Exception):
            print(f"ERROR: EIA agent raised exception: {eia_response}", file=sys.stderr)
            eia_response = f"Error: {eia_response}"
        
        stage3_elapsed = time.time() - stage3_start_time
        print(f"INFO: Stage 3 (Parallel Agents) completed in {stage3_elapsed:.1f}s total", file=sys.stderr)
        
        # --- Synchronous data retrieval and calculations ---
        retrieve_data_spec = parsed_plan.get("retrieve_data", {})
        retrieved_fundamental_data = retrieve_data_based_on_plan(retrieve_data_spec, last_analysis)

        log_progress("Performing financial calculations...")
        calculations_spec = parsed_plan.get("perform_calculations", [])
        calculation_results_obj = perform_planned_calculations(calculations_spec, retrieved_fundamental_data, last_analysis)

        # --- STAGE 4: SYNTHESIS ---
        log_progress("Synthesizing the final response...")
        print("INFO: Synthesis Stage...", file=sys.stderr)
        
        final_context_for_answer = {
            "user_question": user_question,
            "central_brain_plan": central_brain_plan,
            "retrieved_data": retrieved_fundamental_data,
            "calculated_metrics": calculation_results_obj.get("results", {}),
            "ai_company_summary": last_analysis.get('company_summary_html', ''),  # Pre-generated company summary
            "eia_insights": eia_response,  # EIA's analysis of concall transcripts
            "news_summary": news_summary,
            "analyst_report_insights": ara_response  # ARA's analysis of brokerage research
        }

        answering_system_prompt = get_answering_system_prompt()
        
        answering_messages = [
            {"role": "system", "content": answering_system_prompt},
            {"role": "user", "content": f"Please synthesize an answer based on: {json.dumps(final_context_for_answer, indent=2, default=str)[:100000]}"}
        ]
        
        # Use Gemini Flash for best-fast mode, GPT-5-mini for best mode
        if is_best_fast_mode:
            answerer_model = 'gemini-3-flash-preview'
        elif is_best_mode:
            answerer_model = 'gpt-5-mini'
        else:
            answerer_model = selected_model
        final_answer = call_generative_ai_model(
            model=answerer_model,
            messages=answering_messages,
            temperature=1,
            thinking_level='HIGH' if is_best_fast_mode and answerer_model.startswith('gemini-') else None
        )

        return jsonify({
            'answer': final_answer,
            'thought_process': thought_process_str,
            'planning_data': ai_plan_json_str,
            'raw_news_summary': news_summary
        })

    except Exception as e:
        print(f"CRITICAL ERROR in /chat: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': f'An unexpected error occurred: {str(e)}', 'trace': traceback.format_exc()}), 500


@app.route('/industry-chat', methods=['POST'])
def industry_chat():
    """
    Industry Search AI Chatbot endpoint.
    Uses Perplexity Sonar-Pro with Pro Search to answer investor questions
    based on the full context of the generated Industry Research report.
    """
    log_redis_target(app, cache, context="industry_chat_entry")
    print("DEBUG: Entering /industry-chat endpoint...", file=sys.stderr)

    try:
        data = request.get_json(force=True)
        user_question = data.get('question', '').strip()
        industry_name = data.get('industry', '').strip()
        report_context = data.get('report_context', '').strip()
        
        if not user_question:
            return jsonify({'error': 'No question provided'}), 400
        
        if not report_context:
             return jsonify({'answer': 'Industry report context is missing. Please generate the report first.'}), 200

        print(f"INFO: Industry Chat received question: {user_question[:100]}... for industry: {industry_name}", file=sys.stderr)
        
        # Construct the prompt
        system_prompt = f"""You are an expert investment analyst assistant helping an investor research the {industry_name} industry in India.
Your goal is to help the investor decide whether to invest in public companies within this sector.

Rely heavily on the provided "Industry Research Report" context below to answer the user's question.
If the report doesn't contain the answer, use your knowledge base (accessed via Pro Search) to supplement the information, but prioritize the report's insights.
Focus on the Indian market context.

**Industry Research Report Context:**
{report_context}
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_question}
        ]

        print("INFO: Calling Perplexity Sonar-Pro for Industry Chat...", file=sys.stderr)
        
        # Call Perplexity API with sonar-pro and Pro Search enabled
        answer = call_perplexity_api(
            messages,
            model="sonar-pro",
            temperature=1, # Default
            timeout=120,    # Allow enough time for Pro Search
            use_streaming=False,
            enable_pro_search=True
        )
        
        return jsonify({
            'answer': answer,
            'status': 'success'
        })

    except Exception as e:
        print(f"CRITICAL ERROR in /industry-chat: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': f'An unexpected error occurred: {str(e)}', 'trace': traceback.format_exc()}), 500




#======================================================================#
# START: DEBUGGING CODE FOR SCHEMA INSPECTION                          #
#======================================================================#

def get_full_type_and_value_representation(value):
    """Helper to get type and a full representation of a value for debugging."""
    value_type = type(value).__name__
    
    if isinstance(value, (str, int, float, bool)) or value is None:
        # For simple types, json.dumps will give a good string representation
        # (e.g., strings will be quoted, None becomes null)
        try:
            # Use json.dumps for a consistent and readable representation of simple types
            # For strings, it will add quotes. For numbers/bool/None, it's their natural form.
            rep = json.dumps(value)
        except TypeError: # Should not happen for these simple types, but as a fallback
            rep = repr(value)
    elif isinstance(value, list):
        # For lists, we'll represent it as "list of X items, first few items: [item1, item2, ...]"
        # but for our full schema, we want to see more detail if it's a list of dicts (like in summary or tables)
        if value and isinstance(value[0], dict):
            # If it's a list of dicts (like summary items or table rows),
            # we'll handle it in the main loop to show all keys/metrics.
            # Here, just indicate its nature.
            rep = f"list of {len(value)} dicts"
        else:
            # For other lists, show a few items
            rep = f"list of {len(value)} items. Examples: {json.dumps(value[:min(len(value), 5)])}"
            if len(value) > 5: rep += "..."
    elif isinstance(value, dict):
        # For dicts, we'll list all keys.
        # The main loop will iterate through dicts like fundamentals and valuation_and_margin_data.
        # This specific branch is for dicts that are *values* within other structures if not handled by main loops.
        rep = f"dict with keys: {json.dumps(list(value.keys()))}"
    else:
        rep = f"object (repr: {repr(value)})"
    return value_type, rep


@app.route('/debug/full_schema', methods=['GET'])
def print_full_schema():
    global last_analysis
    if not last_analysis or not isinstance(last_analysis, dict) or not last_analysis.get("ticker"):
        return "<pre>last_analysis is empty or not properly populated. Please analyze a stock first via the /analyze endpoint.</pre>", 404

    output_lines = [f"<h1>Full Schema of `last_analysis` for Ticker: {last_analysis.get('ticker', 'N/A')}</h1>"]
    output_lines.append("<pre>") # Use pre for better formatting of the text output

    output_lines.append(f"\n<strong>last_analysis (type: {type(last_analysis).__name__})</strong>")

    for key, top_level_value in last_analysis.items():
        output_lines.append(f"\n  <strong>Key: \"{key}\"</strong>")
        
        if key == "summary" and isinstance(top_level_value, list):
            output_lines.append(f"    Type: list of {len(top_level_value)} summary items (key-value pairs)")
            if top_level_value:
                output_lines.append("    Content (all items):")
                for i, item_dict in enumerate(top_level_value):
                    if isinstance(item_dict, dict):
                        output_lines.append(f"      Item {i}: {{ \"key\": \"{item_dict.get('key')}\", \"value\": \"{item_dict.get('value')}\" }}")
                    else:
                        output_lines.append(f"      Item {i}: {json.dumps(item_dict)}")
            else:
                output_lines.append("    Content: Empty list")

        elif key == "fundamentals" and isinstance(top_level_value, dict):
            output_lines.append(f"    Type: dict of {len(top_level_value)} fundamental tables")
            if top_level_value:
                output_lines.append("    Content (all tables, metric names, and period headers):")
                for table_name, table_data in top_level_value.items():
                    output_lines.append(f"\n      <strong>Table: \"{table_name}\"</strong>")
                    if isinstance(table_data, list) and table_data:
                        output_lines.append(f"        - Contains {len(table_data)} rows (metrics).")
                        # Get all unique period headers from all rows in this table
                        all_period_headers_in_table = set()
                        for row_dict in table_data:
                            if isinstance(row_dict, dict):
                                all_period_headers_in_table.update(k for k in row_dict.keys() if k not in ["", "index"])
                        
                        sorted_period_headers = sorted(list(all_period_headers_in_table), key=lambda x: str(x)) # Sort for consistent display
                        output_lines.append(f"        - All Available Period Column Headers in this table: {json.dumps(sorted_period_headers)}")
                        
                        output_lines.append("        - Metric Names (from '\"\"' key) and example first period value:")
                        for i, row_dict in enumerate(table_data): # Show all metric names
                            if isinstance(row_dict, dict):
                                metric_name = row_dict.get("")
                                example_val = "N/A"
                                if sorted_period_headers: # Get value from first available period
                                    example_val = row_dict.get(sorted_period_headers[0], "N/A (for first period)")
                                output_lines.append(f"          - Metric: \"{metric_name}\" (Example from '{sorted_period_headers[0] if sorted_period_headers else 'N/A'}': {json.dumps(example_val)})")
                            else:
                                output_lines.append(f"          - Row {i} is not a dict: {json.dumps(row_dict)}")
                    elif isinstance(table_data, list) and not table_data:
                        output_lines.append("        - Content: Empty list (no metrics).")
                    else:
                        output_lines.append(f"        - Content Type: {type(table_data).__name__}, Value: {json.dumps(table_data)}")
            else:
                output_lines.append("    Content: Empty dictionary (no tables).")

        elif key == "valuation_and_margin_data" and isinstance(top_level_value, dict):
            output_lines.append(f"    Type: dict of {len(top_level_value)} time series")
            if top_level_value:
                output_lines.append("    Content (all series names and fields of first data point):")
                for series_name, series_data in top_level_value.items():
                    output_lines.append(f"\n      <strong>Series: \"{series_name}\"</strong>")
                    if isinstance(series_data, list) and series_data:
                        output_lines.append(f"        - Contains {len(series_data)} data points.")
                        first_point = series_data[0]
                        if isinstance(first_point, dict):
                            output_lines.append(f"        - Fields in first data point: {json.dumps(list(first_point.keys()))}")
                            output_lines.append(f"        - Example first data point: {json.dumps(first_point)}")
                        else:
                            output_lines.append(f"        - First data point is not a dict: {json.dumps(first_point)}")
                    elif isinstance(series_data, list) and not series_data:
                         output_lines.append("        - Content: Empty list (no data points).")
                    else:
                        output_lines.append(f"        - Content Type: {type(series_data).__name__}, Value: {json.dumps(series_data)}")
            else:
                output_lines.append("    Content: Empty dictionary (no series).")
        
        elif key == "key_metrics" and isinstance(top_level_value, dict):
            output_lines.append(f"    Type: dict with {len(top_level_value)} key metrics")
            output_lines.append("    Content (all key-value pairs):")
            for metric_key, metric_value in top_level_value.items():
                output_lines.append(f"      - {metric_key}: {json.dumps(metric_value)}")
        
        elif key == "peer_comparison" and isinstance(top_level_value, list):
            output_lines.append(f"    Type: list of {len(top_level_value)} peers")
            output_lines.append("    Content (all peers with details):")
            for i, peer_dict in enumerate(top_level_value):
                if isinstance(peer_dict, dict):
                    output_lines.append(f"\\n      <strong>Peer {i+1}: {peer_dict.get('name', peer_dict.get('ticker', 'Unknown'))}</strong>")
                    for pk, pv in peer_dict.items():
                        output_lines.append(f"        - {pk}: {json.dumps(pv)}")
                else:
                    output_lines.append(f"      Peer {i}: {json.dumps(peer_dict)}")
        
        else:
            # For other top-level keys like 'ticker'
            top_type, top_rep = get_full_type_and_value_representation(top_level_value)
            output_lines.append(f"    Type: {top_type}")
            output_lines.append(f"    Value: {top_rep}")

    output_lines.append("</pre>")
    return "\n".join(output_lines)

#======================================================================#
# END: DEBUGGING CODE FOR SCHEMA INSPECTION                            #
#======================================================================#

#======================================================================#
# START: Screener.in data fetching and parsing (fetch_consolidated, etc.) #
#======================================================================#

import requests
from screener_fetcher import (
    fetch_consolidated_async,
    get_company_id_async,
    fetch_chart_data_async,
    parse_chart_json,
    fetch_latest_documents_async,
    fetch_latest_quarter_header_async,
    fetch_latest_document_dates_async
)

from scanx_fetcher import scrape_scanx_company_async

# Initialize TradingView datafeed (authenticated if credentials provided)
_tv_user = os.environ.get('TV_USERNAME', '')
_tv_pass = os.environ.get('TV_PASSWORD', '')
if _tv_user and _tv_pass:
    tv = TvDatafeed(username=_tv_user, password=_tv_pass)
else:
    tv = TvDatafeed()


def extract_key_metrics_from_fundamentals(fundamentals_data, top_ratios=None):
    """
    Extract key financial metrics from screener.in fundamentals tables.
    Returns a dictionary with formatted metric values.
    """
    metrics = {}
    
    if not fundamentals_data:
        print("DEBUG: No fundamentals data provided")
        return metrics
    
    print(f"DEBUG: Available tables in fundamentals: {list(fundamentals_data.keys())}")
    
    # DEBUG: Print all metric names in Financial Ratios table
    # --- STEP 1: Use Top Ratios if available (Most Reliable/Current) ---
    if top_ratios:
        # Stock P/E -> pe_ratio
        # Current Price -> current_price
        # Dividend Yield -> dividend_yield
        # ROCE -> roce
        # ROE -> roe
        # Stock P/B -> pb_ratio
        
        mapping = {
            "Stock P/E": "pe_ratio",
            "Current Price": "current_price",
            "Market Cap": "market_cap",
            "Dividend Yield": "dividend_yield",
            "ROCE": "roce",
            "ROE": "roe",
            "Stock P/B": "pb_ratio",
            "Book Value": "book_value"
        }
        
        for sr_name, metric_key in mapping.items():
            val = top_ratios.get(sr_name)
            if val and str(val).strip() and str(val).strip().lower() != 'n/a':
                # Ensure percentage for some fields
                if metric_key in ['dividend_yield', 'roce', 'roe'] and '%' not in val:
                    val = f"{val}%"
                metrics[metric_key] = val
                print(f"DEBUG: Top Ratios filled {metric_key} = {val}")

    # --- STEP 2: Fallback to Financial Ratios table ---
    if "Financial Ratios" in fundamentals_data:
        ratios_table = fundamentals_data["Financial Ratios"]
        metric_names = [row.get("") for row in ratios_table if row.get("")]
        print(f"DEBUG: Available metrics in Financial Ratios table: {metric_names}")
    
    # Helper: Get latest value from a table
    def get_latest(table_name, metric_name):
        if table_name in fundamentals_data:
            table = fundamentals_data[table_name]  # Already a list of dicts
            print(f"DEBUG: Looking for '{metric_name}' in '{table_name}' table with {len(table)} rows")
            for row in table:
                if row.get("") == metric_name:  # Metric names are in "" key
                    print(f"DEBUG: Found metric '{metric_name}'")
                    # Get the latest period (last non-empty column)
                    for key in reversed(list(row.keys())):
                        if key and key != "" and row.get(key):
                            value = row[key]
                            print(f"DEBUG: Latest value for '{metric_name}': {value}")
                            return value
            print(f"DEBUG: Metric '{metric_name}' not found in table")
        else:
            print(f"DEBUG: Table '{table_name}' not found in fundamentals")
        return None
    
    # Helper: Calculate YoY growth from quarterly data
    def calc_quarterly_yoy_growth(table_name, metric_name):
        if table_name in fundamentals_data:
            table = fundamentals_data.get(table_name)
            if table is None:
                return []
            
            # Handle JSON string
            if isinstance(table, str):
                try:
                    table = json.loads(table)
                except:
                    return []
            
            # Handle DataFrame
            for row in table:
                if row.get("") == metric_name:
                    periods = [k for k in row.keys() if k and k != ""]
                    if len(periods) >= 5:  # Need at least 5 quarters for YoY (current + 4 quarters back)
                        try:
                            latest_val = row.get(periods[-1], "").replace(',', '')
                            year_ago_val = row.get(periods[-5], "").replace(',', '')
                            
                            if latest_val and year_ago_val:
                                latest = float(latest_val)
                                year_ago = float(year_ago_val)
                                if year_ago != 0:
                                    growth = ((latest - year_ago) / abs(year_ago)) * 100
                                    return f"{growth:+.1f}%"
                        except (ValueError, IndexError):
                            pass
        return None
    
    # Helper: Calculate Net Profit Margin from quarterly data
    def calc_npm_from_quarterly():
        if "Quarterly Results" in fundamentals_data:
            table = fundamentals_data["Quarterly Results"]
            sales_row = None
            net_profit_row = None
            
            # Find Sales and Net Profit rows
            for row in table:
                metric = row.get("")
                if metric == "Sales":
                    sales_row = row
                elif metric == "Net Profit":
                    net_profit_row = row
            
            if sales_row and net_profit_row:
                periods = [k for k in sales_row.keys() if k and k != ""]
                if periods:
                    try:
                        latest_period = periods[-1]
                        sales_val = sales_row.get(latest_period, "").replace(',', '')
                        profit_val = net_profit_row.get(latest_period, "").replace(',', '')
                        
                        if sales_val and profit_val:
                            sales = float(sales_val)
                            profit = float(profit_val)
                            if sales != 0:
                                npm = (profit / sales) * 100
                                return f"{npm:.2f} %"
                    except (ValueError, KeyError):
                        pass
        return None
    
    # --- STEP 2: Fallback to Financial Ratios table (Only if not already filled) ---
    if "Financial Ratios" in fundamentals_data:
        # Stock P/E -> pe_ratio
        if is_na(metrics.get('pe_ratio')):
            metrics['pe_ratio'] = get_latest("Financial Ratios", "Stock P/E")
        
        # Stock P/B -> pb_ratio
        if is_na(metrics.get('pb_ratio')):
            metrics['pb_ratio'] = get_latest("Financial Ratios", "Stock P/B")
            
        # Dividend Yield -> dividend_yield
        if is_na(metrics.get('dividend_yield')):
            metrics['dividend_yield'] = get_latest("Financial Ratios", "Dividend Yield %")
            
        # ROCE -> roce
        if is_na(metrics.get('roce')):
            metrics['roce'] = get_latest("Financial Ratios", "ROCE %")
            
        # ROE -> roe
        if is_na(metrics.get('roe')):
            metrics['roe'] = get_latest("Financial Ratios", "ROE %")
    
    # Extract from Quarterly Results  
    metrics['sales_growth_yoy'] = calc_quarterly_yoy_growth("Quarterly Results", "Sales")
    metrics['ebitda_growth_yoy'] = calc_quarterly_yoy_growth("Quarterly Results", "Operating Profit")
    if not metrics['ebitda_growth_yoy']:
         # Fallback for Banks/NBFCs where Operating Profit might be different or labeled Financing Profit
         metrics['ebitda_growth_yoy'] = calc_quarterly_yoy_growth("Quarterly Results", "Financing Profit")
    
    # Calculate Net Profit Growth
    metrics['net_profit_growth'] = calc_quarterly_yoy_growth("Quarterly Results", "Net Profit")
    
    # Calculate NPM correctly: Net Profit / Sales
    metrics['npm'] = calc_npm_from_quarterly()
    
    print(f"DEBUG: Extracted metrics: {metrics}")
    return metrics


def get_yfinance_metrics(ticker):
    """
    Extract financial metrics from yfinance for peer comparison gap-filling.
    Returns a dictionary with formatted values.
    """
    metrics = {
        'market_cap': 'N/A',
        'industry': 'N/A',
        'sector': 'N/A',
        'current_price': 'N/A',
        'pe_ratio': 'N/A',
        'pb_ratio': 'N/A',
        'dividend_yield': 'N/A',
        'roe': 'N/A',
        'roce': 'N/A',
        'npm': 'N/A',
        'sales_growth_yoy': 'N/A',
        'ebitda_growth_yoy': 'N/A'
    }
    
    try:
        ticker_obj = yf.Ticker(f"{ticker}.NS")
        info = ticker_obj.info
        
        # Market Cap - Always show in Crores
        market_cap = info.get('marketCap')
        if market_cap:
            # Convert to Crores
            market_cap_cr = market_cap / 10000000
            metrics['market_cap'] = f"₹{market_cap_cr:,.2f} Cr."
        
        # Industry
        industry = info.get('industry')
        if industry:
            metrics['industry'] = industry
        
        # Sector
        sector = info.get('sector')
        if sector:
            metrics['sector'] = sector
        
        # Current Price
        current_price = info.get('currentPrice') or info.get('regularMarketPrice')
        if current_price:
            metrics['current_price'] = f"₹{current_price:.2f}"
        
        # PE Ratio (trailing)
        pe_ratio = info.get('trailingPE') or info.get('forwardPE')
        if pe_ratio:
            metrics['pe_ratio'] = f"{pe_ratio:.2f}"
        
        # PB Ratio
        pb_ratio = info.get('priceToBook')
        if pb_ratio:
            metrics['pb_ratio'] = f"{pb_ratio:.2f}"
        
        # Dividend Yield - DISABLED from yfinance as it returns inconsistent formats
        # Some stocks return decimal (0.0039), others return percentage-like (0.39)
        # Use Screener.in as the authoritative source for dividend yield
        # div_yield = info.get('dividendYield')
        # if div_yield:
        #     metrics['dividend_yield'] = f"{div_yield:.2f}%"
        
        # ROE (Return on Equity) - try multiple sources
        roe = info.get('returnOnEquity')
        if roe and roe != 0:
            metrics['roe'] = f"{roe * 100:.2f}%"
        else:
            # Try to calculate ROE from net income and book value
            net_income = info.get('netIncomeToCommon')
            book_value = info.get('bookValue')
            shares = info.get('sharesOutstanding')
            if net_income and book_value and shares:
                try:
                    total_equity = book_value * shares
                    if total_equity > 0:
                        calc_roe = (net_income / total_equity) * 100
                        metrics['roe'] = f"{calc_roe:.2f}%"
                except:
                    pass
        
        # ROCE (Return on Capital Employed) - yfinance may not have this directly
        # Try returnOnAssets as a proxy if ROCE isn't available
        roa = info.get('returnOnAssets')
        if roa:
            metrics['roce'] = f"{roa * 100:.2f}%"
        
        # NPM (Net Profit Margin)
        npm = info.get('profitMargins')
        if npm:
            metrics['npm'] = f"{npm * 100:.2f}%"
        
        # Revenue Growth YoY
        revenue_growth = info.get('revenueGrowth')
        if revenue_growth:
            metrics['sales_growth_yoy'] = f"{revenue_growth * 100:.2f}%"
        
        # EBITDA margin as proxy for EBITDA growth (growth not directly available)
        ebitda_margin = info.get('ebitdaMargins')
        if ebitda_margin:
            metrics['ebitda_growth_yoy'] = f"{ebitda_margin * 100:.2f}%"
        
    except Exception as e:
        print(f"WARNING: Failed to fetch yfinance metrics for {ticker}: {e}")
    
    return metrics


def generate_ai_company_summary(ticker, description, fundamentals, documents):
    """
    REWRITTEN: The AI is now tasked with interpreting the raw text from the
    Results Presentation to find segmental and geographical data, as the
    structured tables are unavailable.
    """
    if not openai.api_key:
        print("CRITICAL ERROR: OPENAI_API_KEY environment variable is not set.")
        return "<p><strong>Configuration Error:</strong> The server's AI summary service is not configured.</p>"

    context_parts = []
    context_parts.append(f"## Company: {ticker}\n")
    
    if description:
        context_parts.append("### Business Description\n")
        context_parts.append(description)

    # --- Financials Context ---
    if fundamentals:
        annual_results = fundamentals.get("Annual Results")
        if annual_results is not None:
            try:
                if not annual_results.empty:
                    context_parts.append("\n### Key Annual Financials (for overall trend analysis)\n")
                    annual_df = annual_results.set_index(annual_results.columns[0])
                    key_metrics = ["Sales", "Net Profit"]
                    for metric in key_metrics:
                        if metric in annual_df.index:
                            metric_data = annual_df.loc[metric].iloc[-3:]
                            context_parts.append(f"- {metric} (last 3 years): {', '.join(metric_data.astype(str).tolist())}")
            except Exception as e:
                print(f"DEBUG: Error processing Annual Results for AI summary: {e}")

        # --- NEW: Include latest Quarterly Results for up-to-date summary ---
        quarterly_results = fundamentals.get("Quarterly Results")
        latest_results_quarter = None  # e.g., "Dec 2025"
        if quarterly_results is not None:
            try:
                if not quarterly_results.empty:
                    context_parts.append("\n### Latest Quarterly Results (most recent 4 quarters)\n")
                    q_df = quarterly_results.set_index(quarterly_results.columns[0])
                    # Get last 4 quarter columns
                    q_cols = q_df.columns[-4:] if len(q_df.columns) >= 4 else q_df.columns
                    q_metrics = ["Sales", "Operating Profit", "OPM %", "Net Profit", "EPS in Rs"]
                    for metric in q_metrics:
                        if metric in q_df.index:
                            metric_data = q_df.loc[metric, q_cols]
                            quarters_str = ", ".join([f"{col}: {val}" for col, val in zip(q_cols, metric_data.astype(str))])
                            context_parts.append(f"- {metric}: {quarters_str}")
                    # Store the latest quarter header for mismatch detection
                    latest_results_quarter = str(q_df.columns[-1]).strip() if len(q_df.columns) > 0 else None
            except Exception as e:
                print(f"DEBUG: Error processing Quarterly Results for AI summary: {e}")


    # --- MODIFIED: Document Context (Checks for both Concall and Presentation) ---
    if documents:
        presentation_doc = next((d for d in documents if d.get('type') == 'Presentation' and 'content_summary' in d), None)
        if presentation_doc and presentation_doc['content_summary']:
            context_parts.append("\n### Full Text from Latest Results Presentation\n")
            # Provide a substantial amount of text for the AI to analyze
            context_parts.append(presentation_doc['content_summary'])

        concall_doc = next((d for d in documents if d.get('type') == 'Concall' and 'content_summary' in d), None)
        if concall_doc and concall_doc['content_summary']:
            context_parts.append("\n### Key Points from Latest Concall Transcript\n")
            context_parts.append(concall_doc['content_summary'])

    # --- NEW: Detect document-quarter mismatch ---
    # If the latest quarterly results are newer than the available documents,
    # inject a note telling the AI to callout the mismatch.
    doc_quarter_mismatch = False
    mismatch_note = ""
    if latest_results_quarter and documents:
        try:
            # Parse latest results quarter header: "Dec 2025" -> (2025, 12)
            _month_map = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
                          'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12}
            _rq_parts = latest_results_quarter.lower().split()
            if len(_rq_parts) == 2 and _rq_parts[0][:3] in _month_map:
                results_month = _month_map[_rq_parts[0][:3]]
                results_year = int(_rq_parts[1])

                # Get the latest document date from documents list
                # Screener.in document dates are typically "Nov 2025" (month + year, no day)
                doc_quarter_month = None
                doc_quarter_year = None
                for doc in documents:
                    raw_date = doc.get('date', '').strip()
                    if not raw_date:
                        continue
                    try:
                        # Try parsing common Screener.in date formats
                        # Primary format: "Nov 2025" (month + year, no day)
                        from datetime import datetime as _dt
                        for fmt in ["%b %Y", "%B %Y", "%d %b %Y", "%b %d, %Y", "%d %B %Y", "%B %d, %Y"]:
                            try:
                                parsed_date = _dt.strptime(raw_date, fmt)
                                doc_month = parsed_date.month
                                doc_year = parsed_date.year
                                # Map document publication month to the quarter it belongs to:
                                # Jan-Mar publication → Dec quarter (previous year)
                                # Apr-Jun publication → Mar quarter
                                # Jul-Sep publication → Jun quarter
                                # Oct-Dec publication → Sep quarter
                                _pub_to_quarter = {
                                    1: (12, -1), 2: (12, -1), 3: (12, -1),  # Jan-Mar → Dec (prev year)
                                    4: (3, 0), 5: (3, 0), 6: (3, 0),       # Apr-Jun → Mar
                                    7: (6, 0), 8: (6, 0), 9: (6, 0),       # Jul-Sep → Jun
                                    10: (9, 0), 11: (9, 0), 12: (9, 0)     # Oct-Dec → Sep
                                }
                                q_month, year_offset = _pub_to_quarter[doc_month]
                                q_year = doc_year + year_offset
                                # Take the most recent document quarter
                                if doc_quarter_year is None or (q_year, q_month) > (doc_quarter_year, doc_quarter_month):
                                    doc_quarter_month = q_month
                                    doc_quarter_year = q_year
                                break
                            except ValueError:
                                continue
                    except Exception:
                        continue

                # Compare: is the results quarter newer than the document quarter?
                if doc_quarter_month is not None and doc_quarter_year is not None:
                    if (results_year, results_month) > (doc_quarter_year, doc_quarter_month):
                        doc_quarter_mismatch = True
                        # Build the doc quarter label (e.g., "Sep 2025")
                        _reverse_month = {v: k.capitalize() for k, v in _month_map.items()}
                        doc_quarter_label = f"{_reverse_month.get(doc_quarter_month, 'Unknown')} {doc_quarter_year}"

                        # Extract latest quarter metrics for the note
                        _latest_q_summary_parts = []
                        # Extract latest quarter metrics for the note
                        _latest_q_summary_parts = []
                        try:
                            q_df_for_note = quarterly_results.copy()
                            # Ensure the first column is the index (row headers)
                            q_df_for_note = q_df_for_note.set_index(q_df_for_note.columns[0])
                            # Robustly clean the index labels (handle &nbsp;, +, and whitespace)
                            q_df_for_note.index = [str(idx).replace(u'\xa0', u' ').replace('+', '').strip() for idx in q_df_for_note.index]
                            
                            latest_col_idx = -1
                            prev_col_idx = -5 if len(q_df_for_note.columns) >= 5 else None

                            # Use fallback chains with fuzzy matching
                            _metric_chains = [
                                ("Sales", ["Sales", "Revenue", "Interest Income", "Total Income"]),
                                ("EBITDA", ["Operating Profit", "Financing Profit", "EBITDA"]),
                                ("Net Profit", ["Net Profit", "Profit after Tax", "PAT"]),
                            ]

                            _found_revenue_val = None
                            _found_profit_val = None
                            _found_prev_revenue = None
                            _found_prev_profit = None

                            for display_name, aliases in _metric_chains:
                                matched_metric = None
                                for alias in aliases:
                                    # Case-insensitive partial match
                                    matched_metric = next((idx for idx in q_df_for_note.index if alias.lower() == idx.lower()), None)
                                    if not matched_metric:
                                        # Also try partial match for flexibility
                                        matched_metric = next((idx for idx in q_df_for_note.index if alias.lower() in idx.lower()), None)
                                    if matched_metric:
                                        break

                                if matched_metric:
                                    curr_val = q_df_for_note.iloc[:, latest_col_idx].loc[matched_metric]
                                    line = f"{display_name}: Rs. {curr_val} Cr"
                                    
                                    # Track for NPM calculation
                                    try:
                                        curr_num = float(str(curr_val).replace(',', '').replace('%', ''))
                                        if display_name == "Sales": _found_revenue_val = curr_num
                                        elif display_name == "Net Profit": _found_profit_val = curr_num
                                    except: pass

                                    if prev_col_idx is not None:
                                        try:
                                            prev_val = q_df_for_note.iloc[:, prev_col_idx].loc[matched_metric]
                                            prev_num = float(str(prev_val).replace(',', '').replace('%', ''))
                                            if display_name == "Revenue": _found_prev_revenue = prev_num
                                            elif display_name == "Net Profit": _found_prev_profit = prev_num
                                            
                                            if prev_num != 0:
                                                yoy_pct = ((curr_num - prev_num) / abs(prev_num)) * 100
                                                yoy_str = f"+{yoy_pct:.1f}%" if yoy_pct >= 0 else f"{yoy_pct:.1f}%"
                                                line += f" (YoY: {yoy_str})"
                                        except: pass
                                    _latest_q_summary_parts.append(line)

                            # Handle Margin (Direct row or calculated NPM)
                            _margin_line = None
                            _margin_matched = next((idx for idx in q_df_for_note.index if any(m.lower() in idx.lower() for m in ["OPM %", "Financing Margin %", "Margin %"])), None)
                            
                            if _margin_matched:
                                try:
                                    curr_m_val = q_df_for_note.iloc[:, latest_col_idx].loc[_margin_matched]
                                    m_clean = str(curr_m_val).replace('%', '').strip()
                                    _margin_line = f"Margin: {m_clean}%"
                                    if prev_col_idx is not None:
                                        try:
                                            prev_m_val = q_df_for_note.iloc[:, prev_col_idx].loc[_margin_matched]
                                            prev_m = float(str(prev_m_val).replace('%', '').replace(',', '').strip())
                                            curr_m = float(m_clean)
                                            diff = curr_m - prev_m
                                            diff_str = f"+{diff:.1f}pp" if diff >= 0 else f"{diff:.1f}pp"
                                            _margin_line += f" (YoY: {diff_str})"
                                        except: pass
                                except: pass
                            elif _found_revenue_val and _found_profit_val and _found_revenue_val != 0:
                                # Fallback: calculate NPM
                                npm = (_found_profit_val / _found_revenue_val) * 100
                                _margin_line = f"Margin: {npm:.1f}%"
                                if _found_prev_revenue and _found_prev_profit and _found_prev_revenue != 0:
                                    prev_npm = (_found_prev_profit / _found_prev_revenue) * 100
                                    diff = npm - prev_npm
                                    diff_str = f"+{diff:.1f}pp" if diff >= 0 else f"{diff:.1f}pp"
                                    _margin_line += f" (YoY: {diff_str})"
                            
                            if _margin_line:
                                _latest_q_summary_parts.append(_margin_line)

                        except Exception as note_err:
                            print(f"DEBUG: Error building mismatch note metrics: {note_err}")

                        metrics_summary = "; ".join(_latest_q_summary_parts) if _latest_q_summary_parts else "(metrics not available)"

                        mismatch_note = (
                            f"\n### ⚠️ IMPORTANT: Document-Results Quarter Mismatch\n"
                            f"The latest quarterly results are for \"{latest_results_quarter}\", but the latest available "
                            f"concall transcript and investor presentation documents are from the \"{doc_quarter_label}\" quarter.\n"
                            f"The concall and investor presentation for {latest_results_quarter} quarter have NOT been published yet.\n"
                            f"\nQuick snapshot of {latest_results_quarter} quarter results: {metrics_summary}\n"
                            f"\nYou MUST include a prominent note at the VERY START of your summary (before 'What the Company Does') "
                            f"calling this out. Use this exact HTML format:\n"
                            f'<div style="background: #fff3cd; border-left: 4px solid #ffc107; padding: 12px 16px; '
                            f'margin-bottom: 16px; border-radius: 4px;">'
                            f"<strong>⚠️ Note:</strong> The investor presentation and concall transcript for the "
                            f"{latest_results_quarter} quarter have not been published yet. The detailed analysis below "
                            f"is based on {doc_quarter_label} quarter documents. Here is a quick snapshot of "
                            f"{latest_results_quarter} results: {metrics_summary}."
                            f"</div>\n"
                        )
                        context_parts.append(mismatch_note)
                        print(f"INFO: Document-quarter mismatch detected for {ticker}: Results={latest_results_quarter}, Docs={doc_quarter_label}")

        except Exception as mismatch_err:
            print(f"DEBUG: Error in document-quarter mismatch detection: {mismatch_err}")
            import traceback
            traceback.print_exc()

    full_context = "\n".join(context_parts)
    
    print("\n" + "="*40)
    print("CONTEXT BEING SENT TO AI FOR SUMMARY GENERATION:")
    print(full_context)
    print("="*40 + "\n")
    
    if len(full_context) < 150:
        return "<p>A detailed summary could not be generated due to insufficient data.</p>"

    # --- MODIFIED: New, more detailed System Prompt ---
    system_prompt = """You are an expert financial analyst, specializing in analysis of Indian listed companies only. Your task is to explain complex company information in simple, direct language for a retail investor. You will generate a concise, data-driven summary by interpreting the provided documents.

You must follow two sets of instructions exactly: the **Analysis Instructions** for content and structure, and the **Writing Guidelines** for style and tone.

Be concise: limit the overall response to maximum 800 words.
---

### **Analysis Instructions**

1.  **Structure your response using simple HTML.** Use `<h4>` for headers and `<p>`, `<ul>`, and `<li>` for the body. Do not include `<html>` or `<body>` tags. Your output must be a single block of well-formed HTML.
2.  **Analyze management commentary objectively.** Do not accept management's statements at face value.
3.  **Identify and highlight bias.** Point out when management presents overly optimistic or vague information. Show discrepancies between what management claims and what the financial data shows.
4.  **Use the following output structure:**

    <h4>What the Company Does</h4>
    <p>Write one short paragraph describing the company's main business.</p>

    <h4>How it Generates Revenue</h4>
    <p>Start with a single sentence about how the company makes money. Then, describe the company's overall sales trend using the '### Key Annual Financials (for overall trend analysis)'. Also analyze the '### Latest Quarterly Results' to highlight the most recent quarter's Sales, Profit, and Margin performance. Compare the latest quarter's numbers with the previous quarters to identify trends.</p>
    <p>Next, carefully read the 'Full Text from Latest Results Presentation'. Find revenue breakdowns by business segment, geography, or any other category the company provides. Extract only Sales/Revenue figures and Profit breakdowns (like EBITDA or Net Profit).</p>
    <p>If you find this data, put it in bulleted lists. For each item, show its revenue contribution and its year-over-year (YoY) growth. Be factual and use the numbers exactly as they are presented.</p>
    <ul>
        <li><strong>Business Segments:</strong> (Example: "Digital Platforms: 45% of revenue, grew 15% YoY.")</li>
        <li><strong>Geographical Segments:</strong> (Example: "USA: 60% of revenue; Europe: 25%; Rest of World: 15%.")</li>
    </ul>
    <p>If you cannot find specific revenue numbers after reading the presentation, you must state: "A detailed revenue breakdown by segment or geography was not available in the provided presentation." Do not invent data.</p>
    
    <h4>Latest Developments & News</h4>
    <p>Combine the main points from both the 'Results Presentation' and the 'Concall Transcript'. Create a single bulleted list of the most important developments. Group related points under headers like Capacity Expansion, New Product Launches, Financial Highlights, Future Outlook, or Order Book.</p>

    <h4>Management Biases and Caveats</h4>
    <p>**IMPORTANT: For this section, use ONLY the '### Key Points from Latest Concall Transcript' data.** The Concall Q&A session reveals management biases better than polished presentations.</p>
    <p>In a bulleted list, analyze management's responses in the concall. Point out:</p>
    <ul>
        <li>Where their answers seem evasive, overly optimistic, or vague</li>
        <li>Any conflicts between what they claim and the financial data</li>
        <li>Their tone and conviction when answering tough questions</li>
        <li>Topics they avoided or deflected</li>
    </ul>
    <p>If no Concall Transcript was provided, state: "Management bias analysis requires concall transcript data which was not available."</p>

5.  **Document-Results Quarter Mismatch:** If the context contains a section titled '### ⚠️ IMPORTANT: Document-Results Quarter Mismatch', you MUST include the provided HTML `<div>` callout at the VERY START of your output (before the 'What the Company Does' section). Copy the HTML div exactly as provided. The rest of your analysis should clearly note which quarter the presentation/concall data is from.


---

### **Writing Guidelines**

You must follow these writing rules exactly. Any failure to follow a negative directive invalidates the entire output.

**POSITIVE DIRECTIVES (How you SHOULD write)**

*   **Clarity and brevity:** Craft sentences that average 10-20 words. Focus on a single idea per sentence. You can use an occasional longer sentence for variety.
*   **Active voice and direct verbs:** Use active voice 90% of the time.
*   **Everyday vocabulary:** Use common, concrete words instead of abstract ones.
*   **Straightforward punctuation:** Rely on periods, commas, and question marks. You may use a colon to introduce a list.
*   **Varied sentence length, minimal complexity:** Mix short and medium sentences. Avoid stacking multiple clauses.
*   **Logical flow without buzzwords:** Build your points with plain connectors like 'and', 'but', 'so', or 'then'.
*   **Concrete detail over abstraction:** Give numbers, dates, names, and measurable facts when possible.

**NEGATIVE DIRECTIVES (What you MUST AVOID)**

**A. Punctuation to avoid**
*   **Semicolons (;) and Em dashes (—)**

**B. Overused words & phrases to ban**
*   Never use any of the following, in any form or capitalization:
    At the end of the day, With that being said, It goes without saying, In a nutshell, Needless to say, When it comes to, A significant number of, It's worth mentioning, Last but not least, Cutting-edge, Leveraging, Moving forward, Going forward, On the other hand, Notwithstanding, Takeaway, As a matter of fact, In the realm of, Seamless integration, Robust framework, Holistic approach, Paradigm shift, Synergy, Scale‑up, Optimize, Game‑changer, Unleash, Uncover, In a world, In a sea of, Digital landscape, Elevate, Embark, Delve, In the midst, In addition, It’s important to note, Delve into, Tapestry, Bustling, In summary, In conclusion, Remember that …, Take a dive into, Navigating (e.g., ‘Navigating the landscape’), Landscape (metaphorical), Testament (e.g., ‘a testament to …’), In the world of, Realm, Virtuoso, Symphony, vibrant, Firstly, Moreover, Furthermore, However, Therefore, Additionally, Specifically, Generally, Consequently, Importantly, Similarly, Nonetheless, As a result, Indeed, Thus, Alternatively, Notably, As well as, Despite, Essentially, While, Unless, Also, Even though, Because (as subordinate conjunction), In contrast, Although, In order to, Due to, Even if, Given that, Arguably, To consider, Ensure, Essential, Vital, Out of the box, Underscores, Soul, Crucible, It depends on, You may want to, This is not an exhaustive list, You could consider, As previously mentioned, It’s worth noting that, To summarize, Ultimately, To put it simply, Pesky, Promptly, Dive into, In today’s digital era, Reverberate, Enhance, Emphasise, Enable, Hustle and bustle, Revolutionize, Folks, Foster, Sure, Labyrinthine, Moist, Remnant, As a professional, Subsequently, Nestled, Labyrinth, Gossamer, Enigma, Whispering, Sights unseen, Sounds unheard, A testament to …, Dance, Metamorphosis, Indelible.

**C. Overused single words to ban**
*   however, moreover, furthermore, additionally, consequently, therefore, ultimately, generally, essentially, arguably, significant, innovative, efficient, dynamic, ensure, foster, leverage, utilize.

**D. Sentence-structure patterns to eliminate**
*   **Complex, multi-clause sentences.**
    *   ✗ Example: 'Because the data were incomplete and the timeline was short, we postponed the launch, although we had secured funding.'
    *   ✓ Preferred: 'The data were incomplete. We had little time. So we postponed the launch. The funding was ready.'
*   **Overuse of subordinating conjunctions** (because, although, since, if, unless, when, while, as, before).
*   **Sentences containing more than one verb phrase.**
*   **Chains of prepositional phrases.**

**E. Tone and style**
*   Never mention or reference your own limitations (e.g., 'As an AI …').
*   Do not apologize.
*   Do not hedge. State facts directly.
*   Avoid clichés and metaphors about journeys, music, or landscapes.
*   Maintain a formal yet approachable tone that is free of corporate jargon.
    """

    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Generate the HTML summary for the following company based on this data:\n\n{full_context}"}
        ]
        
        # Use OpenAI for summary generation
        summary_html = call_openai_api(messages, model="gpt-5-mini", temperature=1)
        return summary_html

    except Exception as e:
        print(f"CRITICAL ERROR: Failed to generate AI company summary for {ticker}.")
        # ... (error logging is unchanged) ...
        return "<p><strong>Error:</strong> The AI-powered summary could not be generated at this time.</p>"


def extract_metrics_via_ai(ticker, company_name=None):
    """
    Extract financial metrics using Perplexity sonar model with web search.
    Returns metrics for the main company AND 10 peer competitors.
    """
    # Use company name if provided, otherwise just ticker
    search_name = company_name if company_name and company_name != ticker else ticker
    print(f"INFO: Starting AI metrics extraction for {ticker} ({search_name})...")
    
    # System message to enforce JSON-only output
    system_prompt = """You are a financial data extraction API that ONLY outputs valid JSON.

RULES:
1. You MUST return ONLY a JSON object, with NO explanatory text before or after
2. Do NOT include markdown code fences (```)
3. Do NOT explain what you cannot find - use "N/A" for missing values
4. Do NOT refuse the request - always return the JSON structure with whatever data you can find
5. Every response must be parseable as JSON directly

If you cannot find a value, use "N/A" - but ALWAYS return the complete JSON structure."""

    # User prompt with clear data extraction request - use both ticker and company name
    user_prompt = f"""Extract peer comparison data for {search_name} (NSE: {ticker}) from Indian stock market websites.

Search for "{search_name} peer comparison" and "{ticker} peer comparison" on screener.in, moneycontrol.com, and trendlyne.com.

Return data for {ticker} and 10 of its most relevant peers in this EXACT JSON format:
{{
  "company": {{
    "ticker": "{ticker}",
    "name": "Full Company Name",
    "cmp": "1234.56",
    "market_cap": "123456.78",
    "pe_ratio": "12.34",
    "pb_ratio": "2.34",
    "dividend_yield": "1.23",
    "roce": "15.67",
    "roe": "12.34",
    "sales_growth_yoy": "8.45",
    "ebitda_growth_yoy": "10.23",
    "npm": "5.67"
  }},
  "peers": [
    {{
      "ticker": "PEER1",
      "name": "Peer Company Name",
      "cmp": "value",
      "market_cap": "value",
      "pe_ratio": "value",
      "pb_ratio": "value",
      "dividend_yield": "value",
      "roce": "value",
      "roe": "value",
      "sales_growth_yoy": "value",
      "ebitda_growth_yoy": "value",
      "npm": "value"
    }}
  ]
}}

CRITICAL: Output ONLY the JSON. No text before. No text after. No explanations."""
    
    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        # Use Perplexity sonar-pro with Pro Search for best results
        print(f"INFO: Calling Perplexity sonar-pro API with Pro Search for {ticker} peer comparison...")
        response = call_perplexity_api(messages, model="sonar-pro", temperature=0.1, timeout=90, enable_pro_search=False)
        print(f"INFO: Received response from Perplexity (length: {len(response)} chars)")
        print(f"DEBUG: Response: {response[:1000]}...")

        
        # Try to parse JSON from response
        # First try in code fence
        json_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', response, re.DOTALL)
        if json_match:
            print("DEBUG: Found JSON in code fence")
            result = json.loads(json_match.group(1))
        else:
            # Try to find JSON object with "company" and "peers" keys
            json_obj_match = re.search(r'\{[\s\S]*"company"[\s\S]*"peers"[\s\S]*\}', response, re.DOTALL)
            if json_obj_match:
                print("DEBUG: Found JSON object with company/peers in response")
                result = json.loads(json_obj_match.group(0))
            else:
                # Last resort: try to parse entire response
                print("DEBUG: Trying to parse entire response as JSON")
                result = json.loads(response.strip())
        
        print(f"SUCCESS: AI extracted metrics with {len(result.get('peers', []))} peers")
        return result
        
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse AI metrics JSON: {e}")
        print(f"DEBUG: Full response was: {response}")
        return {"company": {}, "peers": []}
    except Exception as e:
        print(f"ERROR: Failed to extract metrics via AI for {ticker}: {e}")
        import traceback
        traceback.print_exc()
        return {"company": {}, "peers": []}


def generate_ai_scores(ticker, last_analysis):
    """
    DEFINITIVE VERSION: Uses an explicit get_proxy_score() method to ensure
    the correct data is used for both the score tile and the chart.
    """
    try:
        ai_scores = AIScores(ticker, last_analysis)
        ai_scores.calculate_all_scores()

        final_scaled_scores = {}

        # 1. Handle special cases: Valuation and Technical Scores
        for field in ['valuation', 'technical']:
            score_obj = ai_scores.scores[field]
            
            # --- THE DEFINITIVE FIX ---
            # Call the new, unambiguous method to get the proxy score history
            proxy_score_history = score_obj.get_proxy_score()
            # --- END FIX ---
            
            latest_proxy_score = proxy_score_history.iloc[-1]
            hist_min = proxy_score_history.min()
            hist_max = proxy_score_history.max()
            hist_range = hist_max - hist_min if (hist_max - hist_min) != 0 else 1
            scaled_proxy_score_0_1 = (latest_proxy_score - hist_min) / hist_range
            latest_regime = score_obj.df[score_obj.regime_labels_col].iloc[-1]

            final_scaled_scores[field.capitalize()] = (scaled_proxy_score_0_1, latest_regime.replace('_', ' ').title())

        # 2. Handle remaining scores with relative scaling
        data_dict_for_relative_scaling = {}
        for field, score_obj in ai_scores.scores.items():
            if field in ['liquidity', 'volatility']:
                # This correctly uses get_score() to get the final regression score for these modules
                raw_score_value = score_obj.get_score().tail(30).mean()
                regime_label = score_obj.df[f'{field}_regime_labels'].iloc[-1]
                data_dict_for_relative_scaling[field.capitalize()] = (raw_score_value, regime_label.replace('_', ' ').title())

        if data_dict_for_relative_scaling:
            relatively_scaled = AIScores._scale_radar_scores(data_dict_for_relative_scaling)
            final_scaled_scores.update(relatively_scaled)
            
        # 3. Final display loop
        ai_score_html = []
        for field_key in ['Valuation', 'Technical', 'Liquidity', 'Volatility']:
            if field_key in final_scaled_scores:
                score_obj = ai_scores.scores[field_key.lower()]
                scaled_value, description = final_scaled_scores[field_key]
                
                json_dict = {
                    'label': field_key,
                    'value': round(scaled_value * 10, 1),
                    'description': description,
                    'chart1_json': score_obj.plot().to_json(),
                    'chart2_json': score_obj.plot_violin().to_json()
                }
                ai_score_html.append(json_dict)
                
        return ai_score_html, None # Return None for debug filename
        
    except Exception as e:
        print(f"CRITICAL ERROR in generate_ai_scores for {ticker}.")
        print(f"Error: {e}")
        traceback.print_exc()
        return [], None


    

# ------------- Analysis & Respond -------------

@app.route('/progress-stream')
def progress_stream():
    """Streams progress messages to the client."""
    def generate():
        while True:
            # Wait for a message from the queue
            message = progress_queue.get()
            if message == "__END__":
                # Send the final signal and stop
                yield f"data: {message}\n\n"
                break
            # Format as a Server-Sent Event and send to client
            yield f"data: {message}\n\n"
            
    # The 'text/event-stream' mimetype is crucial for SSE
    return Response(generate(), mimetype='text/event-stream')

# =====================================================================
# START: New ASYNC and Caching Implementation for Analysis
# =====================================================================

async def get_analysis_for_ticker_async(tick, skip_ai_summary=False):
    """
    This is the new async core logic function. It runs all I/O-bound
    operations in parallel to significantly speed up data gathering.
    """
    # global last_analysis
    log_progress(f"Starting async analysis for {tick}...")

    # --- Stage 1: Gather all independent I/O-bound data concurrently ---
    
    # yfinance is not async, so we wrap its calls in asyncio.to_thread
    def get_yfinance_data(ticker):
        company_name_from_yfinance = ticker
        try:
            def get_name(t, suffix):
                info = yf.Ticker(f"{t}{suffix}").info or {}
                return (info.get("longName") or info.get("shortName") or "").strip()
            
            name = get_name(ticker, ".NS") or get_name(ticker, ".BO")
            if name:
                company_name_from_yfinance = name
                log_progress(f"Successfully identified company: {name}")
            else:
                log_progress(f"Could not find company name for {ticker}. Defaulting to ticker.")
            return company_name_from_yfinance
        except Exception as e:
            print(f"WARN: yfinance name fetch failed for '{ticker}': {e}")
            return ticker

    # Function to fetch futures volume data using ticker1! naming convention
    def get_futures_volume(ticker):
        try:
            futures_symbol = f"{ticker}1!"  # e.g., RELIANCE1!
            print(f"DEBUG: Attempting to fetch futures volume for {futures_symbol}...")
            futures_data = tv.get_hist(symbol=futures_symbol, exchange="NSE", interval=Interval.in_daily, n_bars=1000)
            print(f"DEBUG: Futures data result: type={type(futures_data)}, is None={futures_data is None}")
            if futures_data is not None and not futures_data.empty:
                print(f"DEBUG: Futures data columns: {list(futures_data.columns)}")
                if 'volume' in futures_data.columns:
                    print(f"DEBUG: Successfully fetched futures volume for {futures_symbol}, rows: {len(futures_data)}")
                    return futures_data[['volume']].rename(columns={'volume': 'VOLUME_FUTURE'})
                else:
                    print(f"DEBUG: Futures data available but no 'volume' column")
            else:
                print(f"DEBUG: No futures data available for {futures_symbol}")
            return None
        except Exception as e:
            import traceback
            print(f"WARN: Failed to fetch futures volume for {ticker}: {e}")
            traceback.print_exc()
            return None

    # Define all tasks that can run without dependencies on each other
    print("=" * 50)
    print("DEBUG: Creating parallel tasks including futures_volume")
    print("=" * 50)
    tasks = {
        "yfinance_name": asyncio.to_thread(get_yfinance_data, tick),
        "tech_data": asyncio.to_thread(evaluate_ticker_signal, tick),
        "screener_tables": fetch_consolidated_async(tick),
        "documents": fetch_latest_documents_async(tick),
        "futures_volume": asyncio.to_thread(get_futures_volume, tick),  # NEW: Fetch futures volume
        "analyst_reports": fetch_analyst_reports_async(tick),  # NEW: Fetch Trendlyne analyst reports
    }
    
    # Run them all in parallel and wait for all to complete
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    results_dict = dict(zip(tasks.keys(), results))

    # --- Check for critical failures from Stage 1 ---
    # Note: futures_volume is optional (not all stocks have F&O), so skip it in critical check
    critical_tasks = ["yfinance_name", "tech_data", "screener_tables", "documents"]
    for task_name, result in results_dict.items():
        if task_name in critical_tasks and isinstance(result, Exception):
            log_progress(f"Critical error during initial data fetch: {task_name} failed.")
            return ({'error': f'Failed to fetch critical data: {task_name}. Reason: {result}'}, 500)

    company_name = results_dict["yfinance_name"]
    res = results_dict["tech_data"]
    tables_from_screener, company_description, top_ratios, is_consolidated = results_dict["screener_tables"]
    latest_documents = results_dict["documents"]
    
    # Futures volume is optional - not all stocks have F&O contracts
    futures_volume_df = None
    print(f"DEBUG: futures_volume result type: {type(results_dict.get('futures_volume'))}")
    print(f"DEBUG: futures_volume result value: {results_dict.get('futures_volume')}")
    if "futures_volume" in results_dict and not isinstance(results_dict["futures_volume"], Exception):
        futures_volume_df = results_dict["futures_volume"]
        if futures_volume_df is not None:
            print(f"DEBUG: Futures volume data available with {len(futures_volume_df)} rows")

    # Analyst reports - optional, gracefully handle errors
    analyst_reports = []
    if "analyst_reports" in results_dict and not isinstance(results_dict["analyst_reports"], Exception):
        analyst_reports = results_dict["analyst_reports"] or []
        print(f"DEBUG: Found {len(analyst_reports)} analyst reports for {tick}")

    # --- Stage 2: Gather dependent I/O tasks ---
    
    async def fetch_dependent_data():
        try:
            # get_company_id runs now since it's fast and needed for valuation
            comp_id = await get_company_id_async(tick)

            # Valuation metrics depend on company_id
            async def fetch_all_valuation_data(is_consolidated_flag=False):
                metric_queries = {
                    "PE Ratio": "Price to Earning-Median PE-EPS", "PB Ratio": "Price to book value-Median PBV-Book value",
                    "EV / EBITDA": "EV Multiple-Median EV Multiple-EBITDA", "Market Cap / Sales": "Market Cap to Sales-Median Market Cap to Sales-Sales",
                    "Margins": "GPM-OPM-NPM-Quarter Sales"
                }
                parsed_data, charts_data = {}, {}
                async with httpx.AsyncClient() as client:
                    valuation_tasks = {label: fetch_chart_data_async(client, comp_id, query, consolidated=is_consolidated_flag) for label, query in metric_queries.items()}
                    valuation_results = await asyncio.gather(*valuation_tasks.values(), return_exceptions=True)
                
                results = dict(zip(valuation_tasks.keys(), valuation_results))
                for label, chart_json in results.items():
                    print(f"\n{'='*20} DEBUG: RAW DATA FOR '{label}' {'='*20}")
                    print(f"Raw JSON (first 300 chars): {str(chart_json)[:300]}")

                    if isinstance(chart_json, Exception) or not chart_json:
                        print(f"DEBUG: Skipping '{label}' due to an exception or empty data.")
                        continue
                    
                    df_from_parser = parse_chart_json(chart_json)
                    if df_from_parser.empty: continue

                    df_filtered = pd.DataFrame()
                    
                    if label == "PE Ratio" and "PE" in df_from_parser.columns: df_filtered = df_from_parser[["PE"]]
                    elif label == "PB Ratio" and "Price to BV" in df_from_parser.columns: df_filtered = df_from_parser[["Price to BV"]]
                    elif label == "EV / EBITDA" and "EV / EBITDA" in df_from_parser.columns: df_filtered = df_from_parser[["EV / EBITDA"]]
                    elif label == "Market Cap / Sales" and "Market Cap / Sales" in df_from_parser.columns: df_filtered = df_from_parser[["Market Cap / Sales"]]
                    elif label == "Margins": df_filtered = df_from_parser[[c for c in ("GPM %","OPM %","NPM %") if c in df_from_parser.columns]]
                    
                    if not df_filtered.empty:
                        # --- START FIX ---
                        # Convert the DataFrame to a list of dictionaries for the AI context
                        df_for_ai_series = df_filtered.copy().reset_index()
                        # Ensure the date column is a string for JSON compatibility
                        for col in df_for_ai_series.columns:
                            if pd.api.types.is_datetime64_any_dtype(df_for_ai_series[col]):
                                df_for_ai_series[col] = df_for_ai_series[col].dt.strftime('%Y-%m-%d')
                        # Store the data in the correct format
                        parsed_data[label] = df_for_ai_series.to_dict(orient='records')
                        # --- END FIX ---
                                                
                        fig = go.Figure()
                        for col in df_filtered.columns: fig.add_trace(go.Scatter(x=df_filtered.index, y=[float(v) for v in df_filtered[col]], mode='lines', name=col))
                        fig.update_layout(title=label, hovermode='x unified', legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.2), xaxis=dict(type='date', title='Date'), yaxis=dict(title=label))
                        charts_data[label] = fig.to_json()

                return parsed_data, charts_data

            # --- Peer Fetching (Moved below valuation definition) ---
            from screener_fetcher import fetch_peer_comparison_from_screener_async
            peer_data_task = fetch_peer_comparison_from_screener_async(tick, company_name)
            
            valuation_task = fetch_all_valuation_data(is_consolidated_flag=is_consolidated)
            
            # Run Stage 2 tasks in parallel
            log_progress(f"Fetching peer comparison and valuation data for {tick} (Consolidated: {is_consolidated})...")
            # Run Screener Peer Comparison and Valuation in parallel
            peer_result, valuation_result = await asyncio.gather(peer_data_task, valuation_task, return_exceptions=True)
            
            # Handle potential exceptions gracefully
            if isinstance(peer_result, Exception):
                print(f"WARN: Screener peer comparison fetch failed: {peer_result}")
                peer_result = {'company': {}, 'peers': []}
            
            if isinstance(valuation_result, Exception):
                print(f"WARN: Valuation data fetch failed: {valuation_result}")
                valuation_result = ({}, {})

            return peer_result, valuation_result
        except Exception as e:
            log_progress(f"Error in dependent data fetching stage: {e}")
            return e # Return exception to be handled

    stage2_results = await fetch_dependent_data()
    if isinstance(stage2_results, Exception):
        return ({'error': f'Failed during dependent data fetching: {stage2_results}'}, 500)
    
    # ScanX disabled - set to empty dict
    scanx_data = {}
    
    # Unpack Screener Peer Comparison and Valuation Results
    if isinstance(stage2_results, tuple) and len(stage2_results) == 2:
        screener_peer_comparison, valuation_results = stage2_results
        parsed_valuation_data, metric_charts_for_frontend = valuation_results if isinstance(valuation_results, tuple) else ({}, {})
    else:
        screener_peer_comparison = {'company': {}, 'peers': []}
        parsed_valuation_data, metric_charts_for_frontend = ({}, {})

    # --- Stage 3: Process all results (now purely CPU-bound) ---
    log_progress("All data fetched. Processing results...")
    
    df = res['Data']
    if df.empty or len(df) < 2 :
        return ({'error':f'No or insufficient historical data found for {tick} after processing.'}, 404)

    technical_summary_list = generate_summary(res)
    cleaned_technical_summary = []
    if isinstance(technical_summary_list, list):
        for item in technical_summary_list:
            if isinstance(item, dict) and "key" in item and "value" in item:
                cleaned_key = str(item["key"]).strip()
                value = item["value"]
                cleaned_value = str(value).strip() if isinstance(value, str) else value
                cleaned_technical_summary.append({"key": cleaned_key, "value": cleaned_value})
            else:
                cleaned_technical_summary.append(item)
    else:
        cleaned_technical_summary = technical_summary_list

# --- START: OPTIMIZED PARALLEL PROCESSING ---    
    # 1. First, process the fundamentals quickly (CPU lightweight)
    fund_data_for_frontend, fund_data_for_ai_context = {}, {}
    if tables_from_screener:
        for table_name, df_original_table in tables_from_screener.items():
            fund_data_for_frontend[table_name] = df_original_table.reset_index().T.to_json(orient='split')
            # Prepare clean rows for AI context
            temp_rows_for_cleaning = df_original_table.reset_index().to_dict(orient='records')
            cleaned_rows_for_ai = []
            for row_dict in temp_rows_for_cleaning:
                cleaned_row = { (str(k).strip() if isinstance(k, str) else k): (str(v).strip() if isinstance(v, str) else v) for k, v in row_dict.items() }
                cleaned_rows_for_ai.append(cleaned_row)
            fund_data_for_ai_context[table_name] = cleaned_rows_for_ai

    # 2. Now run the Heavy Tasks (Charts + AI Summary) in PARALLEL
    log_progress("Generating Charts and AI Summary concurrently...")
    
    loop = asyncio.get_running_loop()

    # Helper to build chart and convert to JSON in a thread (CPU Bound)
    def make_chart_json(func, *args, **kwargs):
        return func(*args, **kwargs).to_json()

    # Define all the tasks - Use blue (#3b82f6) for Company Search price lines
    task_close = loop.run_in_executor(None, make_chart_json, build_close_figure, df, company_name, 1, '#3b82f6')
    task_hl    = loop.run_in_executor(None, make_chart_json, build_hl_figure, df, company_name, 1, '#3b82f6')
    task_ema   = loop.run_in_executor(None, make_chart_json, build_ema_figure, df, company_name)
    task_rsi   = loop.run_in_executor(None, make_chart_json, build_rsi_figure, df, company_name)
    task_adl   = loop.run_in_executor(None, make_chart_json, build_adl_figure, df, company_name)
    task_rs    = loop.run_in_executor(None, make_chart_json, build_rs_figure, df, company_name)
    task_rsi_div = loop.run_in_executor(None, make_chart_json, build_rsi_divergence_figure, df, company_name, 1, '#3b82f6')
    
    # Run AI Summary (OpenAI) and yfinance Metrics in parallel
    if not skip_ai_summary:
        task_ai_sum = loop.run_in_executor(None, generate_ai_company_summary, tick, company_description, tables_from_screener, latest_documents)
    else:
        # Return a placeholder if skipped
        async def dummy_summary(): return "<p>Investor presentation summary skipped for forensic analysis.</p>"
        task_ai_sum = dummy_summary()

    # yfinance fetch is I/O bound, run in thread pool
    task_yf_metrics = loop.run_in_executor(None, get_yfinance_metrics, tick)

    # EXECUTE ALL AT ONCE
    parallel_results = await asyncio.gather(task_close, task_hl, task_ema, task_rsi, task_adl, task_rs, task_rsi_div, task_ai_sum, task_yf_metrics)

    # Unpack the results
    close_j, hl_j, ema_j, rsi_j, adl_j, rs_j, rsi_div_j, ai_company_summary_html, yf_metrics = parallel_results
    
    log_progress("Charts and AI Summary generated successfully.")
# --- END: OPTIMIZED PARALLEL PROCESSING ---


    
    analysis_result_for_cache = {
    "ticker": tick, "company_name": company_name, "summary": cleaned_technical_summary, 
    "fundamentals": fund_data_for_ai_context, "valuation_and_margin_data": parsed_valuation_data, 
    "documents": latest_documents,
    "technical_data_df": df,
    "futures_volume_df": futures_volume_df  # NEW: For Liquidity Score
    }


    log_progress(f"Generating AI scores for {tick}...")
    ai_scores_data = generate_ai_scores(tick, analysis_result_for_cache)
    log_progress("AI scores generated successfully.")

    log_progress("Analysis complete. Loading results ...")

    # Merge Company Metrics with Priority: Screener Top Ratios > yfinance > Screener Peer Table
    # This ensures "N/A" values are filled from the best available source
    
    # Priority 1: Screener Top Ratios

    # Priority 1: Screener Top Ratios
    key_metrics_from_fundamentals = extract_key_metrics_from_fundamentals(fund_data_for_ai_context, top_ratios=top_ratios)
    
    # Priority 2: yfinance (already fetched in parallel)
    # yf_metrics is available from parallel_results
    
    # Priority 3: Screener Peer Table (the 'company' row)
    screener_company_row = screener_peer_comparison.get('company', {})
    
    final_key_metrics = {}
    metric_keys = ['market_cap', 'cmp', 'current_price', 'pe_ratio', 'pb_ratio', 'dividend_yield', 'roce', 'roe', 'sales_growth_yoy', 'ebitda_growth_yoy', 'npm', 'industry', 'sector']
    
    for mk in metric_keys:
        # Check sources in order
        val = key_metrics_from_fundamentals.get(mk)
        if is_na(val):
            val = yf_metrics.get(mk)
        if is_na(val):
            # Special mapping for screener peer table keys
            peer_key_map = {'cmp': 'cmp', 'current_price': 'cmp'}
            pk = peer_key_map.get(mk, mk)
            val = screener_company_row.get(pk)
        
        final_key_metrics[mk] = val if not is_na(val) else 'N/A'

    # Peer data from Screener.in
    peer_comparison_data = screener_peer_comparison.get('peers', [])
    
    # Final Result for cache and frontend
    key_metrics = final_key_metrics


    # This dictionary is what the AI needs. It uses the Python objects.
    # Extract document dates for cache freshness checks
    _cached_concall_date = ""
    _cached_pres_date = ""
    for _doc in latest_documents:
        if _doc.get('type') == 'Concall' and not _cached_concall_date:
            _cached_concall_date = _doc.get('date', '')
        elif _doc.get('type') == 'Presentation' and not _cached_pres_date:
            _cached_pres_date = _doc.get('date', '')

    analysis_for_cache = {
        "ticker": tick, "company_name": company_name, "summary": cleaned_technical_summary, 
        "fundamentals": fund_data_for_ai_context,  # <-- The AI-friendly version
        "valuation_and_margin_data": parsed_valuation_data, 
        "documents": latest_documents,
        "technical_data_df": df,
        "peer_comparison": peer_comparison_data,  # <-- For AI chatbot access
        "key_metrics": key_metrics,  # <-- Key Metrics Snapshot for AI chatbot
        "is_consolidated": is_consolidated,  # Store consolidation status
        "company_description": company_description,  # For document-only refresh
        "cached_concall_date": _cached_concall_date,  # For document freshness check
        "cached_presentation_date": _cached_pres_date  # For document freshness check
    }
    
    # This dictionary is what the frontend needs. It uses the JSON strings.
    result_for_frontend = {
        'ticker': tick, 'company_name': company_name, 'company_summary_html': ai_company_summary_html,
        'summary': cleaned_technical_summary, 'ai_scores': ai_scores_data,
        'chart_close_json': close_j, 'chart_hl_json': hl_j, 'chart_ema_json': ema_j,
        'chart_rsi_json': rsi_j, 'chart_adl_json': adl_j, 'chart_rs_json': rs_j,
        'chart_rsi_divergence_json': rsi_div_j,
        'fundamentals': fund_data_for_frontend, # <-- The frontend-friendly version
        'metric_charts': metric_charts_for_frontend,
        'valuation_and_margin_data': parsed_valuation_data,  # For AI chatbot access
        'documents': latest_documents, 'scanx_data': scanx_data,
        'key_metrics': key_metrics,  # <-- Key metrics table data
        'peer_comparison': {  # <-- NEW: Peer comparison data
            # Use key_metrics for main company to ensure consistency with Key Metrics Snapshot
            'company': {
                'ticker': tick,
                'name': company_name,
                'cmp': key_metrics.get('current_price', 'N/A'),
                'market_cap': key_metrics.get('market_cap', 'N/A'),
                'pe_ratio': key_metrics.get('pe_ratio', 'N/A'),
                'pb_ratio': key_metrics.get('pb_ratio', 'N/A'),
                'dividend_yield': key_metrics.get('dividend_yield', 'N/A'),
                'roce': key_metrics.get('roce', 'N/A'),
                'roe': key_metrics.get('roe', 'N/A'),
                'sales_growth_yoy': key_metrics.get('sales_growth_yoy', 'N/A'),
                'ebitda_growth_yoy': key_metrics.get('ebitda_growth_yoy', 'N/A'),
                'net_profit_growth': key_metrics.get('net_profit_growth', 'N/A'),
                'npm': key_metrics.get('npm', 'N/A')
            },
            'peers': peer_comparison_data  # AI-extracted peer data
        },
        'analyst_reports': analyst_reports,  # Trendlyne analyst reports
        'is_consolidated': is_consolidated,  # Pass to frontend if needed
        'company_description': company_description,  # For document-only refresh
        'cached_concall_date': _cached_concall_date,  # For document freshness check
        'cached_presentation_date': _cached_pres_date  # For document freshness check
    }

    # Pass BOTH dictionaries back to the synchronous wrapper
    return result_for_frontend, analysis_for_cache

# @cache.memoize(timeout=21600)
def get_analysis_for_ticker(tick, skip_ai_summary=False):
    """
    Synchronous wrapper that runs the async logic. The cache stores the final result.
    This is the bridge between the synchronous Flask world and our async code.
    """
    if not tick:
        return None, None
        
    print(f"DEBUG: Running full analysis for {tick} via get_analysis_for_ticker (skip_ai_summary={skip_ai_summary})", file=sys.stderr)
    result_for_frontend, analysis_for_cache = asyncio.run(get_analysis_for_ticker_async(tick, skip_ai_summary=skip_ai_summary))
    
    # SAVE TO CACHE (Match behavior in /analyze)
    try:
        # Save to local memory cache (primary source for Forensic Agent)
        set_local_cache(tick, analysis_for_cache)
        
        # Also save the frontend result (compressed) to Redis for subsequent searches
        # This prevents redundant heavy calculations if the user goes to Company Research next
        if DIRECT_REDIS_CLIENT:
            stock_cache_key = f"stock_analysis_{tick.upper().strip()}"
            # Prepare result for permanent storage
            result_for_frontend['from_cache'] = True
            result_for_frontend['light_cache'] = False
            from datetime import datetime
            result_for_frontend['cached_at'] = datetime.now().isoformat()
            
            try:
                frontend_compressed = zlib.compress(pickle.dumps(result_for_frontend))
                # 3 months TTL
                cache.set(stock_cache_key, frontend_compressed, timeout=7776000)
                DIRECT_REDIS_CLIENT.setex(stock_cache_key, 7776000, frontend_compressed)
                print(f"INFO: Successfully cached fallthrough data for {tick} in Redis", file=sys.stderr)
            except Exception as e:
                print(f"WARN: Failed to cache fallthrough data for {tick} in Redis: {e}", file=sys.stderr)
    except Exception as e:
        print(f"WARN: Failed to cache results in get_analysis_for_ticker for {tick}: {e}", file=sys.stderr)
        
    return result_for_frontend, analysis_for_cache

@app.route('/analyze', methods=['POST'])
def analyze():
    """
    Stock analysis endpoint with smart caching.
    - Returns cached data instantly if available (< 1 second)
    - Use force_refresh=true to bypass cache and get fresh data
    - Cache expires after 1 week (604800 seconds)
    """
    # 3 months in seconds (90 days * 24 * 3600)
    STOCK_CACHE_TTL = 7776000
    global last_analysis
    
    try:
        data = request.get_json(force=True)
        tick = data.get('ticker','').strip().upper()
        force_refresh = data.get('force_refresh', False)
        
        if not tick:
            return jsonify({'error': 'No ticker provided'}), 400
        
        # --- STOCK-BASED CACHE CHECK ---
        stock_cache_key = f"stock_analysis_{tick}"
        
        if not force_refresh:
            try:
                cached_blob = cache.get(stock_cache_key)
                if cached_blob:
                    # Decompress and return cached result
                    if isinstance(cached_blob, bytes):
                        cached_result = pickle.loads(zlib.decompress(cached_blob))
                    else:
                        cached_result = cached_blob
                    
                    # --- NEW: Check for Quarterly Updates ---
                    try:
                        # 1. Get current latest quarter from cache
                        cached_fund = cached_result.get('fundamentals', {})
                        # Also check if the cached analysis was Consolidated
                        cached_is_consolidated = cached_result.get('is_consolidated', False)
                        
                        q_results_json = cached_fund.get('Quarterly Results')
                        if q_results_json:
                            # Parse JSON (stored with orient='split' and transposed)
                            q_data = json.loads(q_results_json)
                            q_headers = [str(h).strip() for h in q_data.get('index', []) if h and str(h).strip()]
                            if q_headers:
                                cached_latest_q = q_headers[-1]
                                
                                # 2. Fetch latest quarter from Screener.in live (matching consolidation status)
                                log_progress(f"Checking for new quarterly results for {tick} (Consolidated: {cached_is_consolidated})...")
                                live_latest_q = asyncio.run(fetch_latest_quarter_header_async(tick, consolidated=cached_is_consolidated))
                                
                                print(f"DEBUG: Smart Refresh Check for {tick}: Cached='{cached_latest_q}' vs Live='{live_latest_q}' (Consolidated={cached_is_consolidated})")
                                
                                if live_latest_q and live_latest_q != cached_latest_q:
                                    print(f"INFO: NEW RESULTS FOUND: {cached_latest_q} -> {live_latest_q}. Forcing refresh for {tick}.")
                                    log_progress(f"New quarterly results ({live_latest_q}) found. Refreshing analysis...")
                                    force_refresh = True
                                else:
                                    print(f"INFO: No new results for {tick} (Latest: {cached_latest_q}).")
                    except Exception as check_err:
                        print(f"WARN: Smart refresh check failed for {tick}: {check_err}")
                    
                    # Check if this is a LIGHT CACHE (only Screener.in + AI Summary)
                    is_light_cache = cached_result.get('light_cache', False)
                    
                    if is_light_cache and not force_refresh:
                        # ============================================================
                        # LIGHT CACHE HIT: Use cached Screener + AI Summary, fetch only missing data
                        # ============================================================
                        print(f"LIGHT CACHE HIT: Using cached Screener.in data for {tick}")
                        log_progress(f"Light cache hit for {tick} - loading cached fundamentals...")
                        
                        # Keep the cached fundamentals + AI summary (the slow parts)
                        cached_company_summary = cached_result.get('company_summary_html', '')
                        cached_fundamentals = cached_result.get('fundamentals', {})
                        cached_key_metrics = cached_result.get('key_metrics', {})
                        cached_documents = cached_result.get('documents', [])
                        
                        # VALIDATION: Check if AI summary failed during light cache
                        # If it contains error message, we'll regenerate it later
                        summary_needs_regeneration = False
                        if not cached_company_summary or 'could not be generated' in cached_company_summary.lower() or 'error' in cached_company_summary.lower()[:100]:
                            print(f"INFO: Light cache AI summary invalid for {tick} - will regenerate")
                            summary_needs_regeneration = True
                        
                        log_progress(f"Fetching charts and remaining data for {tick}...")
                        
                        # ============================================================
                        # FETCH ONLY MISSING DATA (TradingView, yfinance, Trendlyne)
                        # ============================================================
                        try:
                            # Get TradingView data (charts)
                            log_progress("Fetching TradingView price data...")
                            from tvDatafeed import Interval
                            # Reuse global tv instance (already authenticated)
                            
                            # Main price data
                            res = tv.get_hist(symbol=tick, exchange='NSE', interval=Interval.in_daily, n_bars=1000)
                            if res is None or res.empty:
                                res = tv.get_hist(symbol=tick, exchange='BSE', interval=Interval.in_daily, n_bars=1000)
                            
                            if res is None or res.empty:
                                raise Exception(f"No price data for {tick}")
                            
                            # Use evaluate_ticker_signal to process data with all derived indicators
                            # This adds EMA, RSI, ADL, etc. required for chart functions
                            # Returns: {"Ticker": ..., "Signal": ..., "Data": df}
                            from tech_calculations import evaluate_ticker_signal
                            tech_result = evaluate_ticker_signal(tick)
                            
                            if not tech_result or tech_result.get("Signal") in ["NO DATA", "INSUFFICIENT DATA"]:
                                raise Exception(f"Failed to process price data for {tick}")
                            
                            df = tech_result["Data"]  # Extract the DataFrame from result
                            
                            # Get yfinance data - both name and key metrics (PE, PB, Dividend, ROE)
                            yf_info = {}
                            try:
                                import yfinance as yf
                                yf_ticker = yf.Ticker(f"{tick}.NS")
                                yf_info = yf_ticker.info or {}
                                company_name = yf_info.get("longName") or yf_info.get("shortName") or tick
                            except:
                                company_name = tick
                            
                            # Generate technical summary using tech_result (has 'Data' key)
                            technical_summary = generate_summary(tech_result)
                            
                            # Build charts using processed df with all indicators
                            log_progress("Building charts...")
                            close_j = build_close_figure(df, company_name, line_color='#3b82f6').to_json()
                            hl_j = build_hl_figure(df, company_name, line_color='#3b82f6').to_json()
                            ema_j = build_ema_figure(df, company_name).to_json()
                            rsi_j = build_rsi_figure(df, company_name).to_json()
                            adl_j = build_adl_figure(df, company_name).to_json()
                            rs_j = build_rs_figure(df, company_name).to_json()
                            rsi_div_j = build_rsi_divergence_figure(df, company_name, line_color='#3b82f6').to_json()
                            
                            # Get Trendlyne analyst reports (RUNTIME ONLY - not fetched during pre-caching)
                            log_progress("Fetching Trendlyne analyst reports...")
                            try:
                                from analyst_reports.trendlyne_fetcher import fetch_analyst_reports_async
                                analyst_reports = asyncio.run(fetch_analyst_reports_async(tick))
                                print(f"INFO: Fetched {len(analyst_reports)} analyst reports for {tick}")
                            except Exception as e:
                                print(f"WARN: Failed to fetch analyst reports for {tick}: {e}")
                                import traceback
                                traceback.print_exc()
                                analyst_reports = []
                            
                            # Get yfinance key metrics (CMP, Market Cap, PE, PB, Dividend Yield)
                            log_progress("Processing yfinance metrics...")
                            yf_metrics = get_yfinance_metrics(tick)
                            
                            # Add PE, PB, Dividend Yield, ROE from yfinance info
                            if yf_info:
                                # PE Ratio
                                pe = yf_info.get('trailingPE') or yf_info.get('forwardPE')
                                if pe:
                                    yf_metrics['pe_ratio'] = f"{pe:.2f}"
                                
                                # PB Ratio
                                pb = yf_info.get('priceToBook')
                                if pb:
                                    yf_metrics['pb_ratio'] = f"{pb:.2f}"
                                
                                # Dividend Yield - DISABLED (yfinance returns inconsistent format)
                                # Use Screener.in as the authoritative source
                                # div_yield = yf_info.get('dividendYield')
                                # if div_yield:
                                #     yf_metrics['dividend_yield'] = f"{div_yield * 100:.2f}%"
                                
                                # ROE
                                roe = yf_info.get('returnOnEquity')
                                if roe:
                                    yf_metrics['roe'] = f"{roe * 100:.2f}%"
                            
                            # Merge: Screener cached values take priority for financial metrics
                            # yfinance only provides market_cap, industry, sector, current_price
                            merged_key_metrics = {**cached_key_metrics}  # Start with Screener data
                            # Only override with yfinance for these specific fields
                            for key in ['market_cap', 'industry', 'sector', 'current_price']:
                                if yf_metrics.get(key) and yf_metrics[key] != 'N/A':
                                    merged_key_metrics[key] = yf_metrics[key]
                            
                            # Determine if we should use consolidated metrics for charts
                            # Check cached results first, otherwise detect from table names
                            is_consolidated_light = cached_result.get('is_consolidated', False)
                            if not is_consolidated_light and cached_fundamentals:
                                # Fallback detection for older cache entries
                                for table_name in cached_fundamentals.keys():
                                    if "Consolidated" in table_name:
                                        is_consolidated_light = True
                                        break
                            
                            # Get Valuation & Margin charts from Screener.in
                            log_progress("Fetching valuation charts...")
                            metric_charts = {}
                            parsed_valuation_data = {}
                            try:
                                from screener_fetcher import get_company_id, fetch_chart_data, parse_chart_json
                                import plotly.graph_objects as go
                                
                                comp_id = get_company_id(tick)
                                if comp_id:
                                    metric_queries = {
                                        "PE Ratio": "Price to Earning-Median PE-EPS",
                                        "PB Ratio": "Price to book value-Median PBV-Book value",
                                        "EV / EBITDA": "EV Multiple-Median EV Multiple-EBITDA",
                                        "Market Cap / Sales": "Market Cap to Sales-Median Market Cap to Sales-Sales",
                                        "Margins": "GPM-OPM-NPM-Quarter Sales"
                                    }
                                    
                                    for label, query in metric_queries.items():
                                        try:
                                            chart_json = fetch_chart_data(comp_id, query, consolidated=is_consolidated_light)
                                            if chart_json:
                                                df_from_parser = parse_chart_json(chart_json)
                                                if not df_from_parser.empty:
                                                    df_filtered = pd.DataFrame()
                                                    
                                                    if label == "PE Ratio" and "PE" in df_from_parser.columns:
                                                        df_filtered = df_from_parser[["PE"]]
                                                    elif label == "PB Ratio" and any(c in df_from_parser.columns for c in ["Price to BV", "Price to book value"]):
                                                        pb_col = "Price to BV" if "Price to BV" in df_from_parser.columns else "Price to book value"
                                                        df_filtered = df_from_parser[[pb_col]]
                                                    elif label == "EV / EBITDA" and "EV / EBITDA" in df_from_parser.columns:
                                                        df_filtered = df_from_parser[["EV / EBITDA"]]
                                                    elif label == "Market Cap / Sales" and "Market Cap / Sales" in df_from_parser.columns:
                                                        df_filtered = df_from_parser[["Market Cap / Sales"]]
                                                    elif label == "Margins":
                                                        df_filtered = df_from_parser[[c for c in ("GPM %","OPM %","NPM %") if c in df_from_parser.columns]]
                                                    
                                                    if not df_filtered.empty:
                                                        # Build chart
                                                        fig = go.Figure()
                                                        for col in df_filtered.columns:
                                                            fig.add_trace(go.Scatter(x=df_filtered.index, y=[float(v) for v in df_filtered[col]], mode='lines', name=col))
                                                        fig.update_layout(title=label, hovermode='x unified', legend=dict(orientation='h', x=0.5, xanchor='center', y=-0.2), xaxis=dict(type='date', title='Date'), yaxis=dict(title=label))
                                                        metric_charts[label] = fig.to_json()
                                                        
                                                        # Store parsed data for AI
                                                        df_for_ai = df_filtered.copy().reset_index()
                                                        for col in df_for_ai.columns:
                                                            if pd.api.types.is_datetime64_any_dtype(df_for_ai[col]):
                                                                df_for_ai[col] = df_for_ai[col].dt.strftime('%Y-%m-%d')
                                                        parsed_valuation_data[label] = df_for_ai.to_dict(orient='records')
                                        except Exception as e:
                                            print(f"WARN: Failed to fetch {label}: {e}")
                            except Exception as e:
                                print(f"WARN: Valuation charts fetch failed: {e}")
                            
                            # ============================================================
                            # FETCH PEER COMPARISON DATA FROM SCREENER.IN (runtime)
                            # ============================================================
                            log_progress("Fetching peer comparison data...")
                            try:
                                from screener_fetcher import fetch_peer_comparison_from_screener
                                # Use sync wrapper because analyze() is a sync Flask route
                                screener_peer_data = fetch_peer_comparison_from_screener(tick, company_name)
                                peer_comparison_data = screener_peer_data.get('peers', [])
                                screener_company_row = screener_peer_data.get('company', {})
                                
                                # Priority Merge for Company Metrics (Light Cache)
                                # Priority: Screener Cached > yfinance > Screener Peer Table
                                final_metrics = {}
                                metric_keys = ['market_cap', 'cmp', 'current_price', 'pe_ratio', 'pb_ratio', 'dividend_yield', 'roce', 'roe', 'sales_growth_yoy', 'ebitda_growth_yoy', 'npm']
                                
                                for mk in metric_keys:
                                    val = cached_key_metrics.get(mk) # Priority 1: Screener Cached
                                    if is_na(val):
                                        val = yf_metrics.get(mk) # Priority 2: yfinance
                                    if is_na(val):
                                        # Special mapping for screener peer table keys
                                        peer_key_map = {'cmp': 'cmp', 'current_price': 'cmp'}
                                        pk = peer_key_map.get(mk, mk)
                                        val = screener_company_row.get(pk) # Priority 3: Screener Peer Table
                                    
                                    final_metrics[mk] = val if not is_na(val) else 'N/A'

                                # Build peer_comparison structure for frontend
                                peer_comparison = {
                                    'company': {
                                        'ticker': tick,
                                        'name': company_name,
                                        **final_metrics
                                    },
                                    'peers': peer_comparison_data
                                }
                                print(f"INFO: Fetched peer comparison from Screener.in with {len(peer_comparison_data)} peers for {tick}")
                            except Exception as e:
                                print(f"WARN: Failed to fetch peer comparison from Screener.in: {e}")
                                import traceback
                                traceback.print_exc()
                                # Fallback to yfinance only
                                peer_comparison = {
                                    'company': {
                                        'ticker': tick,
                                        'name': company_name,
                                        **yf_metrics
                                    },
                                    'peers': []
                                }
                            
                            # REGENERATE AI SUMMARY if it failed during light cache
                            if summary_needs_regeneration:
                                log_progress("Regenerating AI summary...")
                                try:
                                    # Convert cached fundamentals to AI context format
                                    tables_for_ai = {}
                                    for k, v in cached_fundamentals.items():
                                        try:
                                            tables_for_ai[k] = json.loads(v) if isinstance(v, str) else v
                                        except:
                                            tables_for_ai[k] = v
                                    
                                    # Get company description from cached data or use empty
                                    company_desc = cached_result.get('company_description', '')
                                    
                                    regenerated_summary = generate_ai_company_summary(
                                        tick, 
                                        company_desc, 
                                        tables_for_ai, 
                                        cached_documents
                                    )
                                    if regenerated_summary and 'could not be generated' not in regenerated_summary.lower():
                                        cached_company_summary = regenerated_summary
                                        print(f"INFO: Successfully regenerated AI summary for {tick}")
                                except Exception as e:
                                    print(f"WARN: AI summary regeneration failed: {e}")
                            
                            # Build complete result using cached + fetched data
                            result_for_frontend = {
                                'ticker': tick,
                                'company_name': company_name,
                                'company_summary_html': cached_company_summary,  # FROM CACHE or REGENERATED
                                'summary': technical_summary,
                                'chart_close_json': close_j,
                                'chart_hl_json': hl_j,
                                'chart_ema_json': ema_j,
                                'chart_rsi_json': rsi_j,
                                'chart_adl_json': adl_j,
                                'chart_rs_json': rs_j,
                                'chart_rsi_divergence_json': rsi_div_j,
                                'fundamentals': cached_fundamentals,  # FROM CACHE
                                'metric_charts': metric_charts,  # Valuation charts from Screener.in
                                'documents': cached_documents,  # FROM CACHE
                                'scanx_data': parsed_valuation_data,  # Valuation data for AI (legacy key)
                                'valuation_and_margin_data': parsed_valuation_data,  # For AI chatbot access
                                'key_metrics': merged_key_metrics,  # MERGED: yfinance + cached
                                'peer_comparison': peer_comparison,  # FETCHED AT RUNTIME via AI
                                'analyst_reports': analyst_reports
                            }
                            
                            # Generate AI Scores (need analysis_for_cache)
                            log_progress("Generating AI scores...")
                            # Convert cached fundamentals back for AI context
                            fund_data_for_ai_context = {}
                            for k, v in cached_fundamentals.items():
                                try:
                                    fund_data_for_ai_context[k] = json.loads(v) if isinstance(v, str) else v
                                except:
                                    fund_data_for_ai_context[k] = v
                            
                            analysis_for_cache = {
                                "ticker": tick,
                                "company_name": company_name,
                                "summary": technical_summary,
                                "fundamentals": fund_data_for_ai_context,
                                "valuation_and_margin_data": parsed_valuation_data,  # Valuation data from Screener.in
                                "documents": cached_documents,
                                "technical_data_df": df
                            }
                            
                            ai_scores_data, debug_filename = generate_ai_scores(tick, analysis_for_cache)
                            result_for_frontend['ai_scores'] = ai_scores_data
                            
                            if debug_filename:
                                result_for_frontend['debug_file_url'] = f"/download_debug_file/{debug_filename}"
                            
                            # Update cache with complete data
                            result_for_frontend['from_cache'] = True  # Mark as cached so refresh button appears
                            result_for_frontend['light_cache'] = False  # Upgraded to full cache
                            result_for_frontend['cached_at'] = datetime.now().isoformat()
                            
                            try:
                                frontend_compressed = zlib.compress(pickle.dumps(result_for_frontend))
                                cache.set(stock_cache_key, frontend_compressed, timeout=STOCK_CACHE_TTL)
                                print(f"INFO: Upgraded light cache to full cache for {tick}")
                            except Exception as e:
                                print(f"WARN: Failed to upgrade cache for {tick}: {e}")
                            
                            # Save to local memory cache (Redis-free fallback)
                            set_local_cache(tick, analysis_for_cache)
                            
                            # =====================================================================
                            # START: Background PDF Text Extraction for Light Cache
                            # =====================================================================
                            # If analyst reports exist but texts not cached, start extraction
                            light_cache_analysis_key = result_for_frontend.get('analysis_key') or cached_result.get('analysis_key')
                            if not light_cache_analysis_key:
                                light_cache_analysis_key = str(uuid.uuid4())
                                result_for_frontend['analysis_key'] = light_cache_analysis_key
                            
                            if analyst_reports:
                                cached_texts = cache.get(f"{light_cache_analysis_key}_analyst_texts")
                                if not cached_texts:
                                    def extract_analyst_pdf_text_light_cache(key, reports):
                                        """Background task to download and extract text from analyst PDFs (LIGHT CACHE)."""
                                        import asyncio as _asyncio
                                        import zlib as _zlib
                                        import pickle as _pickle
                                        try:
                                            pdf_texts = []
                                            new_loop = _asyncio.new_event_loop()
                                            _asyncio.set_event_loop(new_loop)
                                            try:
                                                for report in reports:
                                                    pdf_url = report.get('pdf_url')
                                                    if not pdf_url:
                                                        continue
                                                    try:
                                                        pdf_bytes = new_loop.run_until_complete(download_analyst_pdf_with_cookies(pdf_url))
                                                        text = extract_text_from_pdf(pdf_bytes)
                                                        pdf_texts.append({
                                                            'brokerage': report.get('brokerage'),
                                                            'date': report.get('date'),
                                                            'recommendation': report.get('recommendation'),
                                                            'target_price': report.get('target_price'),
                                                            'upside': report.get('upside'),
                                                            'pdf_text': text,
                                                            'pdf_url': pdf_url
                                                        })
                                                        print(f"INFO: Extracted text from {report.get('brokerage')} report ({len(text)} chars) [LIGHT CACHE]")
                                                    except Exception as e:
                                                        print(f"WARN: Failed to extract text from {pdf_url}: {e}")
                                            finally:
                                                new_loop.close()
                                            compressed = _zlib.compress(_pickle.dumps(pdf_texts))
                                            cache.set(f"{key}_analyst_texts", compressed, timeout=21600)
                                            print(f"INFO: Cached {len(pdf_texts)} analyst texts for LIGHT CACHE key: {key}")
                                        except Exception as e:
                                            print(f"WARN: Background PDF extraction failed (LIGHT CACHE): {e}")
                                            try:
                                                compressed = _zlib.compress(_pickle.dumps([]))
                                                cache.set(f"{key}_analyst_texts", compressed, timeout=21600)
                                            except: pass
                                    
                                    bg_thread = threading.Thread(
                                        target=extract_analyst_pdf_text_light_cache,
                                        args=(light_cache_analysis_key, analyst_reports)
                                    )
                                    bg_thread.daemon = True
                                    bg_thread.start()
                                    print(f"INFO: Started background PDF extraction for LIGHT CACHE ({len(analyst_reports)} reports)")
                            # =====================================================================
                            # END: Background PDF Text Extraction for Light Cache
                            # =====================================================================
                            
                            log_progress(f"Analysis complete for {tick}!")
                            
                            # Save to global for debug/schema viewing
                            last_analysis = analysis_for_cache
                            
                            return jsonify(sanitize_for_json(result_for_frontend))
                            
                        except Exception as e:
                            print(f"ERROR: Light cache completion failed for {tick}: {e}")
                            traceback.print_exc()
                            # Fall through to full analysis below
                    
                    elif not force_refresh:
                        # ============================================================
                        # SECONDARY CHECK: Are there newer documents on Screener.in?
                        # (This handles the 1-2 day lag between results and concall upload)
                        # ============================================================
                        try:
                            cached_cc_date = cached_result.get('cached_concall_date', '')
                            cached_pres_date = cached_result.get('cached_presentation_date', '')
                            
                            # Only check if we have at least one cached date to compare
                            if cached_cc_date or cached_pres_date:
                                log_progress(f"Checking for new documents for {tick}...")
                                live_doc_dates = asyncio.run(fetch_latest_document_dates_async(tick))
                                live_cc_date = live_doc_dates.get('concall_date', '')
                                live_pres_date = live_doc_dates.get('presentation_date', '')
                                
                                print(f"DEBUG: Document check for {tick}: "
                                      f"Cached concall='{cached_cc_date}' vs Live='{live_cc_date}', "
                                      f"Cached pres='{cached_pres_date}' vs Live='{live_pres_date}'")
                                
                                new_concall = live_cc_date and live_cc_date != cached_cc_date
                                new_pres = live_pres_date and live_pres_date != cached_pres_date
                                
                                if new_concall or new_pres:
                                    doc_type = "concall" if new_concall else "presentation"
                                    print(f"INFO: NEW DOCUMENTS FOUND for {tick}! New {doc_type} detected. Refreshing documents + AI Summary...")
                                    log_progress(f"New {doc_type} found. Refreshing AI Summary...")
                                    
                                    try:
                                        # 1. Fetch fresh documents (downloads + summarizes PDFs)
                                        fresh_documents = asyncio.run(fetch_latest_documents_async(tick))
                                        
                                        if fresh_documents:
                                            # 2. Reconstruct DataFrames from cached JSON for AI summary
                                            import pandas as _pd  # Local import (pandas not imported globally in handler.py)
                                            cached_fund_json = cached_result.get('fundamentals', {})
                                            reconstructed_tables = {}
                                            for table_name, json_str in cached_fund_json.items():
                                                try:
                                                    # Reverse: df.reset_index().T.to_json(orient='split')
                                                    reconstructed_tables[table_name] = _pd.read_json(json_str, orient='split').T
                                                except Exception:
                                                    pass  # Skip tables that can't be reconstructed
                                            
                                            # 3. Regenerate AI Summary with new documents + existing financials
                                            cached_description = cached_result.get('company_description', '')
                                            new_ai_summary = generate_ai_company_summary(
                                                tick, cached_description, reconstructed_tables, fresh_documents
                                            )
                                            
                                            # 4. Extract new document dates
                                            new_cc_date = ""
                                            new_pres_date = ""
                                            for _doc in fresh_documents:
                                                if _doc.get('type') == 'Concall' and not new_cc_date:
                                                    new_cc_date = _doc.get('date', '')
                                                elif _doc.get('type') == 'Presentation' and not new_pres_date:
                                                    new_pres_date = _doc.get('date', '')
                                            
                                            # 5. Update cached result with new data
                                            cached_result['documents'] = fresh_documents
                                            cached_result['company_summary_html'] = new_ai_summary
                                            cached_result['cached_concall_date'] = new_cc_date
                                            cached_result['cached_presentation_date'] = new_pres_date
                                            
                                            # 6. Re-save to Redis
                                            try:
                                                frontend_compressed = zlib.compress(pickle.dumps(cached_result))
                                                cache.set(stock_cache_key, frontend_compressed, timeout=STOCK_CACHE_TTL)
                                                if DIRECT_REDIS_CLIENT:
                                                    DIRECT_REDIS_CLIENT.setex(stock_cache_key, STOCK_CACHE_TTL, frontend_compressed)
                                                print(f"INFO: Updated cache with new documents + AI Summary for {tick}")
                                            except Exception as cache_save_err:
                                                print(f"WARN: Failed to save updated cache for {tick}: {cache_save_err}")
                                            
                                            log_progress(f"AI Summary refreshed with new documents for {tick}!")
                                        else:
                                            print(f"WARN: fetch_latest_documents_async returned empty for {tick}, keeping cached version")
                                    except Exception as doc_refresh_err:
                                        print(f"WARN: Document refresh failed for {tick}: {doc_refresh_err}")
                                        traceback.print_exc()
                                        # Continue with cached result as-is
                                else:
                                    print(f"INFO: No new documents for {tick} (dates unchanged).")
                        except Exception as doc_check_err:
                            print(f"WARN: Document freshness check failed for {tick}: {doc_check_err}")
                        
                        # FULL CACHE HIT: Return result (possibly updated with new documents)
                        print(f"FULL CACHE HIT: Returning analysis for {tick}")
                        log_progress(f"Cache hit for {tick} - returning instantly!")
                        
                        cached_result['from_cache'] = True
                        cached_result['cache_key'] = stock_cache_key
                        
                        # Save to local memory cache (Redis-free fallback)
                        # Use cached_result which has all the needed fields
                        set_local_cache(tick, cached_result)
                        
                        # Save to global for debug/schema viewing
                        last_analysis = cached_result
                        
                        # =====================================================================
                        # START: Background PDF Text Extraction for Full Cache
                        # =====================================================================
                        # If analyst reports exist but texts not cached, start extraction
                        full_cache_analyst_reports = cached_result.get('analyst_reports', [])
                        full_cache_analysis_key = cached_result.get('analysis_key')
                        if not full_cache_analysis_key:
                            full_cache_analysis_key = str(uuid.uuid4())
                            cached_result['analysis_key'] = full_cache_analysis_key
                        
                        if full_cache_analyst_reports:
                            full_cache_texts = cache.get(f"{full_cache_analysis_key}_analyst_texts")
                            if not full_cache_texts:
                                def extract_analyst_pdf_text_full_cache(key, reports):
                                    """Background task to download and extract text from analyst PDFs (FULL CACHE)."""
                                    import asyncio as _asyncio
                                    import zlib as _zlib
                                    import pickle as _pickle
                                    try:
                                        pdf_texts = []
                                        new_loop = _asyncio.new_event_loop()
                                        _asyncio.set_event_loop(new_loop)
                                        try:
                                            for report in reports:
                                                pdf_url = report.get('pdf_url')
                                                if not pdf_url:
                                                    continue
                                                try:
                                                    pdf_bytes = new_loop.run_until_complete(download_analyst_pdf_with_cookies(pdf_url))
                                                    text = extract_text_from_pdf(pdf_bytes)
                                                    pdf_texts.append({
                                                        'brokerage': report.get('brokerage'),
                                                        'date': report.get('date'),
                                                        'recommendation': report.get('recommendation'),
                                                        'target_price': report.get('target_price'),
                                                        'upside': report.get('upside'),
                                                        'pdf_text': text,
                                                        'pdf_url': pdf_url
                                                    })
                                                    print(f"INFO: Extracted text from {report.get('brokerage')} report ({len(text)} chars) [FULL CACHE]")
                                                except Exception as e:
                                                    print(f"WARN: Failed to extract text from {pdf_url}: {e}")
                                        finally:
                                            new_loop.close()
                                        compressed = _zlib.compress(_pickle.dumps(pdf_texts))
                                        cache.set(f"{key}_analyst_texts", compressed, timeout=21600)
                                        print(f"INFO: Cached {len(pdf_texts)} analyst texts for FULL CACHE key: {key}")
                                    except Exception as e:
                                        print(f"WARN: Background PDF extraction failed (FULL CACHE): {e}")
                                        try:
                                            compressed = _zlib.compress(_pickle.dumps([]))
                                            cache.set(f"{key}_analyst_texts", compressed, timeout=21600)
                                        except: pass
                                
                                bg_thread = threading.Thread(
                                    target=extract_analyst_pdf_text_full_cache,
                                    args=(full_cache_analysis_key, full_cache_analyst_reports)
                                )
                                bg_thread.daemon = True
                                bg_thread.start()
                                print(f"INFO: Started background PDF extraction for FULL CACHE ({len(full_cache_analyst_reports)} reports)")
                        # =====================================================================
                        # END: Background PDF Text Extraction for Full Cache
                        # =====================================================================
                        
                        return jsonify(sanitize_for_json(cached_result))
                        
            except Exception as cache_err:
                print(f"WARN: Cache read failed for {tick}: {cache_err}")
                traceback.print_exc()
                # Continue with fresh analysis on cache error
        
        # --- CACHE MISS OR FORCE REFRESH: Do full analysis ---
        print(f"CACHE {'REFRESH' if force_refresh else 'MISS'}: Full analysis for {tick}")
        log_progress(f"{'Refreshing' if force_refresh else 'Analyzing'} {tick}...")
        
        # Unpack the two dictionaries returned by the function
        result_for_frontend, analysis_for_cache = get_analysis_for_ticker(tick)
        
        # =====================================================
        # ERROR HANDLING: Check if analysis failed
        # =====================================================
        # When critical data fetch fails, get_analysis_for_ticker returns ({'error': ...}, 500)
        if isinstance(analysis_for_cache, int):
            # This means an error occurred - analysis_for_cache is actually an HTTP status code
            error_msg = result_for_frontend.get('error', 'Analysis failed due to data fetch error')
            print(f"ERROR: Analysis failed for {tick}: {error_msg}", file=sys.stderr)
            return jsonify({'error': error_msg}), analysis_for_cache

        # --- START MODIFICATION ---
        # Generate the AI scores and get the debug filename
        ai_scores_data, debug_filename = generate_ai_scores(tick, analysis_for_cache)
        print(f"DEBUG: AI scores generated - count: {len(ai_scores_data) if ai_scores_data else 0}")
        result_for_frontend['ai_scores'] = ai_scores_data

        # If a debug file was created, add its download URL to the response
        if debug_filename:
            result_for_frontend['debug_file_url'] = f"/download_debug_file/{debug_filename}"
        # --- END MODIFICATION ---
        
        # --- NEW SPLIT CACHING LOGIC ---
        analysis_key = str(uuid.uuid4()) # Key for Light Data (Chatbot)
        heavy_key = f"{analysis_key}_heavy" # Key for Heavy Data (Future use)

        # 1. Create Light Payload (For AI Chatbot)
        # We exclude the massive 'technical_data_df' which causes the timeouts
        light_analysis_data = {
            k: v for k, v in analysis_for_cache.items() 
            if k != 'technical_data_df'
        }

        # 2. Create Heavy Payload
        heavy_analysis_data = {
            "technical_data_df": analysis_for_cache.get("technical_data_df")
        }

        # --- ROBUST CACHING BLOCK ---
        try:
            # Compress the data using zlib
            pickled_data = pickle.dumps(light_analysis_data)
            compressed_data = zlib.compress(pickled_data)
            
            # Log size for debugging
            size_mb = sys.getsizeof(compressed_data) / (1024 * 1024)
            print(f"INFO: Caching COMPRESSED data (Key: {analysis_key}). Size: {size_mb:.2f} MB")
            
            # Save chatbot context to Redis (6 hours for chatbot)
            cache.set(analysis_key, compressed_data, timeout=21600)
            
        except Exception as e:
            print(f"WARNING: Redis cache write failed. Error: {e}")
        
        # =====================================================
        # SAVE TO LOCAL MEMORY CACHE (Redis-free fallback)
        # =====================================================
        # This ensures chatbot works even when Redis is unavailable
        set_local_cache(tick, light_analysis_data)

        # =====================================================================
        # START: Background PDF Text Extraction for Analyst Reports
        # =====================================================================
        # Extract raw text from analyst report PDFs in background (no AI cost)
        # This enables the Analyst Report Agent (ARA) to access PDF content
        def extract_analyst_pdf_text_background(key, reports):
            """Background task to download and extract text from analyst PDFs."""
            import asyncio as _asyncio
            import zlib as _zlib
            import pickle as _pickle
            import traceback as _traceback
            try:
                pdf_texts = []
                # Explicitly manage event loop for thread safety
                new_loop = _asyncio.new_event_loop()
                _asyncio.set_event_loop(new_loop)
                
                try:
                    for report in reports:
                        pdf_url = report.get('pdf_url')
                        if not pdf_url:
                            continue
                        try:
                            # Download PDF using authenticated session
                            # Use run_until_complete with explicit loop for reliability
                            pdf_bytes = new_loop.run_until_complete(download_analyst_pdf_with_cookies(pdf_url))
                            
                            # Extract text (no AI, just pdfplumber)
                            text = extract_text_from_pdf(pdf_bytes)
                            pdf_texts.append({
                                'brokerage': report.get('brokerage'),
                                'date': report.get('date'),
                                'recommendation': report.get('recommendation'),
                                'target_price': report.get('target_price'),
                                'upside': report.get('upside'),
                                'pdf_text': text,
                                'pdf_url': pdf_url
                            })
                            print(f"INFO: Extracted text from {report.get('brokerage')} report ({len(text)} chars)")
                        except Exception as e:
                            # Log error to file for debugging
                            err_msg = f"Failed to extract text from {pdf_url}: {e}"
                            print(f"WARN: {err_msg}")
                            try:
                                debug_path = os.path.join(os.path.dirname(__file__), "debug_output.txt")
                                with open(debug_path, "a") as f:
                                    f.write(f"{datetime.now()} - {err_msg}\n")
                            except: pass
                finally:
                    new_loop.close()
                
                # ALWAYS cache the extracted texts (even if empty) to stop frontend polling
                compressed = _zlib.compress(_pickle.dumps(pdf_texts))
                cache.set(f"{key}_analyst_texts", compressed, timeout=21600)
                print(f"INFO: Cached {len(pdf_texts)} analyst report texts for key: {key}")
                
            except Exception as e:
                print(f"WARN: Background PDF text extraction failed: {e}")
                _traceback.print_exc()
                # Determine fallback to stop frontend loading spinner
                try:
                    compressed = _zlib.compress(_pickle.dumps([]))
                    cache.set(f"{key}_analyst_texts", compressed, timeout=21600)
                except: pass
        
        # Start background thread if analyst reports exist
        analyst_reports = result_for_frontend.get('analyst_reports', [])
        if analyst_reports:
            bg_thread = threading.Thread(
                target=extract_analyst_pdf_text_background,
                args=(analysis_key, analyst_reports)
            )
            bg_thread.daemon = True
            bg_thread.start()
            print(f"INFO: Started background PDF text extraction for {len(analyst_reports)} reports")
        # =====================================================================
        # END: Background PDF Text Extraction
        # =====================================================================

        
        # Add the key to the frontend data
        result_for_frontend['analysis_key'] = analysis_key
        result_for_frontend['from_cache'] = False
        
        # --- STOCK-BASED CACHE: Store for future instant access ---
        try:
            # Add timestamp for "Last updated" display
            result_for_frontend['cached_at'] = datetime.now().isoformat()
            
            # Compress and store with 1-week expiry
            frontend_compressed = zlib.compress(pickle.dumps(result_for_frontend))
            cache.set(stock_cache_key, frontend_compressed, timeout=STOCK_CACHE_TTL)
            print(f"INFO: Stored stock analysis in cache (Key: {stock_cache_key}, TTL: 1 week)")
        except Exception as e:
            print(f"WARNING: Stock cache write failed: {e}")
        
        # Save to global for debug/schema viewing
        last_analysis = analysis_for_cache
        
        return jsonify(sanitize_for_json(result_for_frontend))


        # --- END OF NEW LOGIC ---

    except Exception as e:
        import traceback
        data = request.get_json(force=True) if request.is_json else {}
        log_progress(f"ERROR in /analyze wrapper for {data.get('ticker', 'N/A')}: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e), 'trace': traceback.format_exc()}), 500
    
    
@app.route('/download_debug_file/<filename>')
def download_debug_file(filename):
    """
    Serves the temporary debug Excel file for download from the 'debug_files' subdir.
    """
    try:
        # Admin-only: check authentication
        from flask import session as flask_session
        if 'user_id' not in flask_session:
            return "Authentication required", 401
        from auth.database import User
        user = User.get_by_id(flask_session['user_id'])
        if not user or not user.is_admin:
            return "Admin access required", 403
        # --- MODIFIED FILE SERVING LOGIC ---
        # Explicitly serve from the dedicated 'debug_files' directory
        return send_from_directory("debug_files", filename, as_attachment=True)
        # --- END MODIFICATION ---
    except FileNotFoundError:
        return "File not found.", 404
    

# =====================================================================
# START: Section Refresh Endpoint
# =====================================================================

@app.route('/refresh-section', methods=['POST'])
def refresh_section():
    """
    Refresh a specific section of the Company Research page.
    Currently supports: 'peer_comparison'
    
    Data Source Priority for Peer Comparison:
    1. Screener.in direct crawl (most reliable, structured data)
    2. yfinance for main company metrics (CMP, Market Cap, PE, PB)
    3. sonar-pro AI as fallback for missing data
    """
    try:
        data = request.get_json(force=True)
        ticker = data.get('ticker', '').strip().upper()
        section = data.get('section', '').strip().lower()
        
        if not ticker:
            return jsonify({'error': 'No ticker provided'}), 400
        
        if section == 'peer_comparison':
            log_progress(f"Refreshing peer comparison data for {ticker}...")
            
            # =====================================================
            # STEP 1: Get company name from yfinance FIRST (for fuzzy matching)
            # =====================================================
            company_name = ticker
            try:
                yf_ticker = yf.Ticker(f"{ticker}.NS")
                info = yf_ticker.info or {}
                company_name = info.get("longName") or info.get("shortName") or ticker
                print(f"INFO: Got company name from yfinance: '{company_name}'")
            except:
                print(f"WARN: Could not get company name from yfinance, using ticker: '{ticker}'")
            
            # =====================================================
            # STEP 2: Screener.in Direct Crawl (with company_name for matching)
            # =====================================================
            from screener_fetcher import fetch_peer_comparison_from_screener
            
            log_progress(f"Fetching peer data...")
            screener_data = fetch_peer_comparison_from_screener(ticker, company_name)
            screener_company = screener_data.get('company', {})
            screener_peers = screener_data.get('peers', [])
            
            has_screener_data = bool(screener_company) and bool(screener_peers)
            if has_screener_data:
                print(f"INFO: Screener.in returned {len(screener_peers)} peers for {ticker}")
            else:
                print(f"WARN: Screener.in returned incomplete data for {ticker}")
            
            # =====================================================
            # STEP 3: yfinance for Main Company Metrics (gap-filling)
            # =====================================================
            log_progress(f"Fetching yfinance metrics...")
            yf_metrics = get_yfinance_metrics(ticker)
            # Note: company_name already obtained in STEP 1 above
            
            # =====================================================
            # PRIORITY 3: sonar-pro AI for Gap-Filling
            # =====================================================
            # Always call AI to have fallback data available for filling gaps
            ai_company_metrics = {}
            ai_peer_data = []
            
            log_progress(f"Fetching peer data from AI (for gap-filling)...")
            try:
                ai_extracted_metrics = extract_metrics_via_ai(ticker, company_name)
                ai_company_metrics = ai_extracted_metrics.get('company', {})
                ai_peer_data = ai_extracted_metrics.get('peers', [])
                print(f"INFO: AI returned {len(ai_peer_data)} peers for gap-filling")
            except Exception as e:
                print(f"WARN: AI peer extraction failed: {e}")
            
            # BUILD FINAL RESPONSE WITH PRIORITY MERGING
            # =====================================================
            current_company_metrics = data.get('current_company_metrics', {})
            # print(f"DEBUG: Received current_company_metrics for {ticker}: {current_company_metrics}")
            
            # Using global is_na helper

            # Helper to get first non-empty value from sources (priority order)
            def get_value(metric_name, *sources):
                for i, val in enumerate(sources):
                    if not is_na(val):
                        # print(f"DEBUG: Metric {metric_name} using source index {i}: value={val}")
                        return val
                return 'N/A'
            
            # Build company data with priority: Screener > yfinance > Current > AI
            # On refresh, we want fresh data from Screener to overwrite old values
            final_company = {
                'ticker': ticker,
                'name': get_value('name', screener_company.get('name'), company_name, current_company_metrics.get('name'), ai_company_metrics.get('name')),
                'cmp': get_value('cmp', screener_company.get('cmp'), yf_metrics.get('current_price'), current_company_metrics.get('cmp'), ai_company_metrics.get('cmp')),
                'market_cap': get_value('market_cap', screener_company.get('market_cap'), yf_metrics.get('market_cap'), current_company_metrics.get('market_cap'), ai_company_metrics.get('market_cap')),
                'pe_ratio': get_value('pe_ratio', screener_company.get('pe_ratio'), yf_metrics.get('pe_ratio'), current_company_metrics.get('pe_ratio'), ai_company_metrics.get('pe_ratio')),
                'pb_ratio': get_value('pb_ratio', screener_company.get('pb_ratio'), yf_metrics.get('pb_ratio'), current_company_metrics.get('pb_ratio'), ai_company_metrics.get('pb_ratio')),
                'dividend_yield': get_value('dividend_yield', screener_company.get('dividend_yield'), yf_metrics.get('dividend_yield'), current_company_metrics.get('dividend_yield'), ai_company_metrics.get('dividend_yield')),
                'roce': get_value('roce', screener_company.get('roce'), yf_metrics.get('roce'), current_company_metrics.get('roce'), ai_company_metrics.get('roce')),
                'roe': get_value('roe', screener_company.get('roe'), yf_metrics.get('roe'), current_company_metrics.get('roe'), ai_company_metrics.get('roe')),
                'sales_growth_yoy': get_value('sales_growth_yoy', screener_company.get('sales_growth_yoy'), yf_metrics.get('sales_growth_yoy'), current_company_metrics.get('sales_growth_yoy'), ai_company_metrics.get('sales_growth_yoy')),
                'ebitda_growth_yoy': get_value('ebitda_growth_yoy', screener_company.get('ebitda_growth_yoy'), yf_metrics.get('ebitda_growth_yoy'), current_company_metrics.get('ebitda_growth_yoy'), ai_company_metrics.get('ebitda_growth_yoy')),
                'npm': get_value('npm', screener_company.get('npm'), yf_metrics.get('npm'), current_company_metrics.get('npm'), ai_company_metrics.get('npm'))
            }
            # print(f"DEBUG: Final merged company metrics for {ticker}: {final_company}")
            
            # =====================================================
            # MERGE PEER DATA: Fill N/A gaps using yfinance + AI
            # =====================================================
            final_peers = []
            metric_fields = ['cmp', 'market_cap', 'pe_ratio', 'pb_ratio', 'dividend_yield', 
                           'roce', 'roe', 'sales_growth_yoy', 'ebitda_growth_yoy', 'npm']
            
            # Build AI peer lookup by name for gap-filling (multiple keys per peer)
            ai_peer_lookup = {}
            for ai_peer in ai_peer_data:
                ai_name = ai_peer.get('name', '').lower().strip()
                ai_ticker = ai_peer.get('ticker', '').lower().strip()
                if ai_name:
                    ai_peer_lookup[ai_name] = ai_peer
                    # Add initials as key (e.g., "indian oil corporation ltd" -> "iocl")
                    # Only if 3+ chars to avoid false matches
                    words = ai_name.split()
                    if len(words) > 1:
                        initials = ''.join(w[0] for w in words if w and len(w) > 0)
                        if len(initials) >= 3:
                            ai_peer_lookup[initials] = ai_peer
                if ai_ticker:
                    ai_peer_lookup[ai_ticker] = ai_peer
            
            print(f"DEBUG: AI peer lookup keys: {list(ai_peer_lookup.keys())}")
            
            # Helper to check if value is N/A or empty (Already defined above)
            
            # Helper to extract ticker from peer name (common patterns)
            def extract_peer_ticker(peer_name):
                """Try to map peer company name to a ticker symbol."""
                name_to_ticker = {
                    'iocl': 'IOC',
                    'i o c l': 'IOC',
                    'indian oil': 'IOC',
                    'indian oil corporation': 'IOC',
                    'bpcl': 'BPCL',
                    'b p c l': 'BPCL',
                    'bharat petroleum': 'BPCL',
                    'hpcl': 'HINDPETRO',
                    'h p c l': 'HINDPETRO',
                    'hindustan petroleum': 'HINDPETRO',
                    'mrpl': 'MRPL',
                    'm r p l': 'MRPL',
                    'cpcl': 'CHENNPETRO',
                    'c p c l': 'CHENNPETRO',
                    'chennai petroleum': 'CHENNPETRO',
                    'rajasthan': 'N/A',  # Small company, may not be on yfinance
                    'ongc': 'ONGC',
                    'oil india': 'OIL',
                    'gail': 'GAIL',
                    'petronet lng': 'PETRONET',
                    'indraprastha gas': 'IGL',
                    'mahanagar gas': 'MGL',
                    'gujarat gas': 'GUJGASLTD',
                    'tata power': 'TATAPOWER',
                    'ntpc': 'NTPC',
                    'power grid': 'POWERGRID',
                    'adani green': 'ADANIGREEN',
                    'adani power': 'ADANIPOWER',
                    'tcs': 'TCS',
                    'infosys': 'INFY',
                    'wipro': 'WIPRO',
                    'hcl tech': 'HCLTECH',
                    'tech mahindra': 'TECHM',
                    'hdfc bank': 'HDFCBANK',
                    'icici bank': 'ICICIBANK',
                    'axis bank': 'AXISBANK',
                    'kotak mahindra': 'KOTAKBANK',
                    'sbi': 'SBIN',
                    'state bank': 'SBIN',
                }
                name_lower = peer_name.lower().strip()
                for pattern, ticker in name_to_ticker.items():
                    if pattern in name_lower:
                        return ticker if ticker != 'N/A' else None
                return None
            
            # Helper to find matching AI peer using fuzzy matching
            def find_ai_peer(peer_name, ai_lookup):
                """Find matching AI peer data using various name patterns."""
                name_lower = peer_name.lower().strip()
                
                # Try exact match first
                if name_lower in ai_lookup:
                    return ai_lookup[name_lower]
                
                # Try without spaces (e.g., "I O C L" -> "iocl")
                name_no_spaces = name_lower.replace(' ', '')
                if name_no_spaces in ai_lookup:
                    return ai_lookup[name_no_spaces]
                
                # Try partial match
                for key, value in ai_lookup.items():
                    if name_lower in key or key in name_lower:
                        return value
                
                return {}
            
            if screener_peers:
                log_progress(f"Filling gaps in {len(screener_peers)} peers using yfinance...")
                
                for screener_peer in screener_peers:
                    merged_peer = dict(screener_peer)  # Copy Screener data
                    peer_name = merged_peer.get('name', '')
                    
                    # Count N/A fields to decide if we need yfinance
                    na_count = sum(1 for f in metric_fields if is_na(merged_peer.get(f)))
                    print(f"DEBUG: Peer '{peer_name}' has {na_count} N/A fields")
                    
                    if na_count > 0:
                        # Try to get yfinance data for this peer
                        peer_ticker = extract_peer_ticker(peer_name)
                        peer_yf_metrics = {}
                        
                        if peer_ticker:
                            try:
                                print(f"DEBUG: Fetching yfinance for {peer_ticker}...")
                                peer_yf_metrics = get_yfinance_metrics(peer_ticker)
                                print(f"DEBUG: yfinance returned pb_ratio={peer_yf_metrics.get('pb_ratio')}, roe={peer_yf_metrics.get('roe')}")
                            except Exception as e:
                                print(f"WARN: yfinance failed for peer {peer_ticker}: {e}")
                        else:
                            print(f"DEBUG: No ticker found for peer '{peer_name}'")
                        
                        # Try AI data as additional fallback using fuzzy matching
                        matching_ai_peer = find_ai_peer(peer_name, ai_peer_lookup)
                        if matching_ai_peer:
                            print(f"DEBUG: Found AI data for '{peer_name}': pb_ratio={matching_ai_peer.get('pb_ratio')}, roe={matching_ai_peer.get('roe')}")
                        
                        # Fill N/A gaps with priority: yfinance > AI
                        for field in metric_fields:
                            if is_na(merged_peer.get(field)):
                                # Map our field names to yfinance field names
                                yf_field_map = {
                                    'cmp': 'current_price',
                                    'pe_ratio': 'pe_ratio',
                                    'pb_ratio': 'pb_ratio',
                                    'dividend_yield': 'dividend_yield',
                                    'roe': 'roe',
                                    'roce': 'roce',
                                    'npm': 'npm',
                                    'market_cap': 'market_cap',
                                    'sales_growth_yoy': 'sales_growth_yoy',
                                    'ebitda_growth_yoy': 'ebitda_growth_yoy'
                                }
                                yf_field = yf_field_map.get(field, field)
                                
                                # Try yfinance first
                                yf_val = peer_yf_metrics.get(yf_field)
                                if yf_val and not is_na(yf_val):
                                    merged_peer[field] = yf_val
                                    continue
                                
                                # Try AI as fallback
                                ai_val = matching_ai_peer.get(field)
                                if ai_val and not is_na(ai_val):
                                    merged_peer[field] = ai_val
                    
                    final_peers.append(merged_peer)
                    
                print(f"INFO: Merged {len(final_peers)} Screener peers with yfinance+AI gap-filling")
            else:
                # No Screener peers, use AI peers directly
                final_peers = ai_peer_data
                print(f"INFO: Using {len(final_peers)} AI peers directly (no Screener data)")
            
            peer_comparison = {
                'company': final_company,
                'peers': final_peers
            }
            
            source_info = "Screener.in + yfinance" if has_screener_data else "AI (fallback)"
            
            # =====================================================
            # BUILD KEY_METRICS FROM FINAL COMPANY DATA
            # This ensures Key Metrics Snapshot matches Peer Comparison
            # =====================================================
            updated_key_metrics = {
                'current_price': final_company.get('cmp', 'N/A'),
                'market_cap': final_company.get('market_cap', 'N/A'),
                'pe_ratio': final_company.get('pe_ratio', 'N/A'),
                'pb_ratio': final_company.get('pb_ratio', 'N/A'),
                'dividend_yield': final_company.get('dividend_yield', 'N/A'),
                'roce': final_company.get('roce', 'N/A'),
                'roe': final_company.get('roe', 'N/A'),
                'sales_growth_yoy': final_company.get('sales_growth_yoy', 'N/A'),
                'ebitda_growth_yoy': final_company.get('ebitda_growth_yoy', 'N/A'),
                'npm': final_company.get('npm', 'N/A')
            }

            log_progress(f"Peer comparison refreshed from {source_info} with {len(final_peers)} peers")
            print(f"INFO: Refreshed peer comparison for {ticker} - Source: {source_info}, Peers: {len(final_peers)}")
            
            return jsonify({
                'success': True,
                'section': 'peer_comparison',
                'source': source_info,  # Include source for debugging
                'data': peer_comparison,
                'key_metrics': updated_key_metrics  # NEW: Also update Key Metrics Snapshot
            })
        
        elif section == 'key_metrics':
            log_progress(f"Refreshing key metrics data for {ticker}...")
            
            # Get current metrics from frontend (to preserve existing non-N/A values)
            current_metrics = data.get('current_metrics', {})
            
            # Using global is_na helper
            
            # Start with current metrics
            key_metrics = dict(current_metrics)
            source_info = []
            filled_fields = []
            
            # Log what fields are N/A and need filling
            na_fields = [k for k, v in key_metrics.items() if is_na(v)]
            print(f"DEBUG: Fields that are N/A and need filling: {na_fields}")
            
            # =====================================================
            # STEP 0 (NEW): Screener Peer Comparison - ALWAYS UPDATE THESE METRICS
            # Updates: P/E, P/B, Div Yield, ROCE, ROE, Sales Growth, NPM
            # =====================================================
            try:
                from screener_fetcher import fetch_peer_comparison_from_screener
                
                # Get company name for fuzzy matching
                company_name = ticker
                try:
                    yf_ticker = yf.Ticker(f"{ticker}.NS")
                    info = yf_ticker.info or {}
                    company_name = info.get("longName") or info.get("shortName") or ticker
                except:
                    pass
                
                log_progress(f"Fetching {ticker} peer data...")
                screener_data = fetch_peer_comparison_from_screener(ticker, company_name)
                screener_company = screener_data.get('company', {})
                
                if screener_company:
                    print(f"DEBUG: Screener peer data company: {screener_company}")
                    
                    # Map screener fields to key_metrics - ALWAYS UPDATE (not just N/A)
                    # These are the 8 metrics user wants to update from screener
                    peer_field_map = {
                        'pe_ratio': 'pe_ratio',
                        'pb_ratio': 'pb_ratio',
                        'dividend_yield': 'dividend_yield',
                        'roce': 'roce',
                        'roe': 'roe',
                        'sales_growth_yoy': 'sales_growth_yoy',
                        'ebitda_growth_yoy': 'ebitda_growth_yoy',  # Maps to OPM% / Financing Margin%
                        'npm': 'npm'
                    }
                    
                    for screener_key, metric_key in peer_field_map.items():
                        val = screener_company.get(screener_key)
                        if val and not is_na(val):
                            key_metrics[metric_key] = val
                            if metric_key not in filled_fields:
                                filled_fields.append(metric_key)
                            print(f"DEBUG: Screener peer filled {metric_key} = {val}")
                    
                    if filled_fields:
                        source_info.append("Screener.in Peers")
                    print(f"INFO: Screener peer comparison filled {len(filled_fields)} metrics for {ticker}")
                else:
                    print(f"WARN: Screener peer comparison returned no company data for {ticker}")
            except Exception as e:
                print(f"WARN: Screener peer comparison fetch failed for {ticker}: {e}")
                # traceback is imported globally
                traceback.print_exc()

            
            # =====================================================
            # STEP 1: Try to fill N/A gaps from Screener.in fundamentals
            # =====================================================
            try:
                from screener_fetcher import fetch_consolidated
                log_progress(f"Fetching {ticker} fundamentals...")
                tables, _, top_ratios, is_consolidated_screener = fetch_consolidated(ticker)
                
                # Get Financial Ratios table
                ratios_table = tables.get('Financial Ratios')
                if ratios_table is not None and not ratios_table.empty:
                    print(f"DEBUG: Financial Ratios table found with {len(ratios_table)} rows")
                    
                    # Helper to get latest value from ratios table
                    def get_latest_from_ratios(metric_name):
                        # Financial Ratios table has metric names in first column
                        metric_name_lower = metric_name.strip().lower()
                        for idx, row in ratios_table.iterrows():
                            # Check if first column matches metric name
                            first_col = ratios_table.columns[0] if len(ratios_table.columns) > 0 else None
                            row_name = str(row.get(first_col, '') if first_col else row.iloc[0]).strip().lower()
                            
                            # Check for exact match or if metric_name is a substring 
                            # (e.g. "Dividend Yield" matches "Dividend Yield %")
                            if row_name == metric_name_lower or metric_name_lower in row_name:
                                # Get the latest non-empty value (rightmost column)
                                for col in reversed(ratios_table.columns[1:]):
                                    val = row.get(col)
                                    if val and str(val).strip() and str(val).strip().lower() != 'nan':
                                        return str(val).strip()
                        return None
                    
                    # Map Screener metric names to key_metrics fields
                    screener_field_map = {
                        'dividend_yield': 'Dividend Yield %',
                        'pb_ratio': 'Stock P/B',
                        'pe_ratio': 'Stock P/E',
                        'roce': 'ROCE %',
                        'roe': 'ROE %'
                    }
                    
                    screener_filled = []
                    
                    # Mapping for Top Ratios first
                    top_mapping = {
                        "Dividend Yield": "dividend_yield",
                        "Stock P/E": "pe_ratio",
                        "Stock P/B": "pb_ratio",
                        "ROCE": "roce",
                        "ROE": "roe"
                    }
                    if top_ratios:
                        for tr_name, tr_key in top_mapping.items():
                            if is_na(key_metrics.get(tr_key)):
                                val = top_ratios.get(tr_name)
                                if val and not is_na(val):
                                    if tr_key in ['dividend_yield', 'roce', 'roe'] and '%' not in val:
                                        val = f"{val}%"
                                    key_metrics[tr_key] = val
                                    screener_filled.append(tr_key)
                                    print(f"DEBUG: Screener.in (Top) filled {tr_key} = {val}")

                    # Fallback mapping for Financial Ratios table
                    screener_field_map = {
                        'dividend_yield': 'Dividend Yield',
                        'pb_ratio': 'Stock P/B',
                        'pe_ratio': 'Stock P/E',
                        'roce': 'ROCE',
                        'roe': 'ROE'
                    }
                    
                    for key, screener_name in screener_field_map.items():
                        # ONLY update if field is still N/A (Reverting override logic)
                        if is_na(key_metrics.get(key)):
                            val = get_latest_from_ratios(screener_name)
                            if val and not is_na(val):
                                # Format the value appropriately
                                if key in ['dividend_yield', 'roce', 'roe'] and '%' not in val:
                                    val = f"{val}%"
                                key_metrics[key] = val
                                screener_filled.append(key)
                                print(f"DEBUG: Screener.in (Table) filled {key} = {val}")
                    
                    if screener_filled:
                        if "Screener.in" not in source_info:
                            source_info.append("Screener.in")
                        filled_fields.extend(screener_filled)
                    print(f"DEBUG: Screener.in processed {len(screener_filled)} fields for {ticker}")
                else:
                    print(f"DEBUG: Financial Ratios table not found or empty")
            except Exception as e:
                print(f"WARN: Screener.in fundamentals fetch failed for {ticker}: {e}")
                # traceback is imported globally
                traceback.print_exc()
            
            # Update na_fields after Screener fill
            na_fields = [k for k, v in key_metrics.items() if is_na(v)]
            
            # =====================================================
            # STEP 2: Fill remaining N/A gaps with yfinance
            # =====================================================
            if na_fields:
                try:
                    log_progress(f"Fetching {ticker} metrics from yfinance...")
                    yf_metrics = get_yfinance_metrics(ticker)
                    
                    # Also get industry and sector from yfinance
                    try:
                        yf_ticker = yf.Ticker(f"{ticker}.NS")
                        info = yf_ticker.info or {}
                        if 'industry' in na_fields and info.get('industry'):
                            key_metrics['industry'] = info.get('industry')
                            filled_fields.append('industry')
                        if 'sector' in na_fields and info.get('sector'):
                            key_metrics['sector'] = info.get('sector')
                            filled_fields.append('sector')
                    except:
                        pass
                    
                    # Map yfinance fields to key_metrics fields
                    yf_field_map = {
                        'current_price': 'current_price',
                        'market_cap': 'market_cap',
                        'pe_ratio': 'pe_ratio',
                        'pb_ratio': 'pb_ratio',
                        'dividend_yield': 'dividend_yield',
                        'roce': 'roce',
                        'roe': 'roe',
                        'npm': 'npm',
                        'sales_growth_yoy': 'sales_growth_yoy',
                        'ebitda_growth_yoy': 'ebitda_growth_yoy'
                    }
                    
                    yf_filled = []
                    for key, yf_key in yf_field_map.items():
                        if key in na_fields:
                            yf_val = yf_metrics.get(yf_key)
                            if yf_val and not is_na(yf_val):
                                key_metrics[key] = yf_val
                                yf_filled.append(key)
                                print(f"DEBUG: yfinance filled {key} = {yf_val}")
                    
                    if yf_filled:
                        source_info.append("yfinance")
                        filled_fields.extend(yf_filled)
                    print(f"DEBUG: yfinance filled {len(yf_filled)} fields for {ticker}")
                except Exception as e:
                    print(f"WARN: yfinance fetch failed for {ticker}: {e}")
            
            # =====================================================
            # STEP 3: Fill remaining N/A gaps with AI (sonar-pro)
            # =====================================================
            remaining_na = [k for k, v in key_metrics.items() if is_na(v)]
            if remaining_na:
                print(f"DEBUG: Still N/A after yfinance: {remaining_na}")
                try:
                    log_progress(f"Fetching {ticker} metrics from AI...")
                    ai_result = extract_metrics_via_ai(ticker)
                    ai_company = ai_result.get('company', {})
                    
                    if ai_company:
                        ai_field_map = {
                            'current_price': 'cmp',
                            'market_cap': 'market_cap',
                            'pe_ratio': 'pe_ratio',
                            'pb_ratio': 'pb_ratio',
                            'dividend_yield': 'dividend_yield',
                            'roce': 'roce',
                            'roe': 'roe',
                            'npm': 'npm',
                            'sales_growth_yoy': 'sales_growth_yoy',
                            'ebitda_growth_yoy': 'ebitda_growth_yoy'
                        }
                        
                        ai_filled = []
                        for key, ai_key in ai_field_map.items():
                            if key in remaining_na:
                                ai_val = ai_company.get(ai_key)
                                if ai_val and not is_na(ai_val):
                                    key_metrics[key] = ai_val
                                    ai_filled.append(key)
                                    print(f"DEBUG: AI filled {key} = {ai_val}")
                        
                        if ai_filled:
                            source_info.append("AI")
                            filled_fields.extend(ai_filled)
                        print(f"DEBUG: AI filled {len(ai_filled)} fields for {ticker}")
                except Exception as e:
                    print(f"WARN: AI fetch failed for {ticker}: {e}")
            
            # Ensure all expected fields exist (set to N/A if still missing)
            expected_fields = ['current_price', 'market_cap', 'pe_ratio', 'pb_ratio', 'dividend_yield',
                              'roce', 'roe', 'sales_growth_yoy', 'ebitda_growth_yoy', 'npm',
                              'industry', 'sector']
            for field in expected_fields:
                if field not in key_metrics:
                    key_metrics[field] = 'N/A'
            
            source_str = " + ".join(source_info) if source_info else "None"
            log_progress(f"Key metrics refreshed from {source_str}")
            print(f"INFO: Refreshed key metrics for {ticker} - Source: {source_str}, Filled: {filled_fields}")
            
            return jsonify({
                'success': True,
                'section': 'key_metrics',
                'source': source_str,
                'data': key_metrics,
                'filled_fields': filled_fields
            })
        
        return jsonify({'error': f'Unknown section: {section}'}), 400
        
    except Exception as e:
        print(f"ERROR in /refresh-section: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# =====================================================================
# END: Section Refresh Endpoint
# =====================================================================

# =====================================================================
# START: Add Peer to Comparison Endpoint
# =====================================================================

@app.route('/add-peer', methods=['POST'])
def add_peer():
    """
    Add a new peer to the comparison table.
    Fetches data from Screener.in first, then fills gaps with yfinance.
    """
    try:
        data = request.get_json()
        ticker = data.get('ticker', '').strip().upper()
        
        if not ticker:
            return jsonify({'error': 'No ticker provided'}), 400
        
        print(f"INFO: Adding peer {ticker} to comparison...")
        
        # Helper functions (same as in refresh-section)
        def is_na(val):
            if val is None:
                return True
            val_str = str(val).strip()
            return val_str == '' or val_str == 'N/A' or val_str == 'nan'
        
        # =====================================================
        # STEP 1: Try to get data from Screener.in
        # =====================================================
        from screener_fetcher import fetch_peer_comparison_from_screener
        
        peer_data = {}
        try:
            log_progress(f"Fetching {ticker}...")
            screener_result = fetch_peer_comparison_from_screener(ticker)
            screener_company = screener_result.get('company', {})
            
            if screener_company and screener_company.get('name'):
                peer_data = dict(screener_company)
                peer_data['ticker'] = ticker
                print(f"INFO: Got Screener.in data for {ticker}")
        except Exception as e:
            print(f"WARN: Screener.in fetch failed for {ticker}: {e}")
        
        # =====================================================
        # STEP 2: Fill gaps with yfinance
        # =====================================================
        log_progress(f"Fetching {ticker} from yfinance...")
        yf_metrics = get_yfinance_metrics(ticker)
        
        # Get company name from yfinance if not from Screener
        if not peer_data.get('name'):
            try:
                yf_ticker = yf.Ticker(f"{ticker}.NS")
                info = yf_ticker.info or {}
                peer_data['name'] = info.get("longName") or info.get("shortName") or ticker
                peer_data['ticker'] = ticker
            except:
                peer_data['name'] = ticker
                peer_data['ticker'] = ticker
        
        # Fields to fill
        metric_fields = ['cmp', 'market_cap', 'pe_ratio', 'pb_ratio', 'dividend_yield', 
                        'roce', 'roe', 'sales_growth_yoy', 'ebitda_growth_yoy', 'npm']
        
        # Map our field names to yfinance field names
        yf_field_map = {
            'cmp': 'current_price',
            'pe_ratio': 'pe_ratio',
            'pb_ratio': 'pb_ratio',
            'dividend_yield': 'dividend_yield',
            'roe': 'roe',
            'roce': 'roce',
            'npm': 'npm',
            'market_cap': 'market_cap',
            'sales_growth_yoy': 'sales_growth_yoy',
            'ebitda_growth_yoy': 'ebitda_growth_yoy'
        }
        
        # Fill N/A gaps with yfinance data
        for field in metric_fields:
            if is_na(peer_data.get(field)):
                yf_field = yf_field_map.get(field, field)
                yf_val = yf_metrics.get(yf_field)
                if yf_val and not is_na(yf_val):
                    peer_data[field] = yf_val
        
        # Ensure all fields have at least 'N/A'
        for field in metric_fields:
            if field not in peer_data or is_na(peer_data.get(field)):
                peer_data[field] = 'N/A'
        
        print(f"SUCCESS: Added peer {ticker} - {peer_data.get('name')}")
        
        return jsonify({
            'success': True,
            'peer': peer_data
        })
        
    except Exception as e:
        print(f"ERROR in /add-peer: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# =====================================================================
# END: Add Peer to Comparison Endpoint
# =====================================================================


# =====================================================================
# END: New ASYNC and Caching Implementation for Analysis
# =====================================================================



# =====================================================================
# START: Lightweight Chart-Only Analysis Endpoint
# =====================================================================

@app.route('/analyze-chart', methods=['POST'])
def analyze_chart():
    """
    Lightweight chart-only analysis endpoint.
    Returns only technical analysis data (no fundamentals, no AI summary).
    Significantly faster than /analyze (~5-10 seconds vs 30-60 seconds).
    
    Runs ONLY:
    - evaluate_ticker_signal() - TradingView price data + technical indicators
    - generate_summary() - AI Chart Analysis table
    - Chart builders - Plotly charts (Close, HL, EMA, RSI, ADL, RS, RSI Divergence)
    
    Skips:
    - Screener.in fundamentals crawl
    - AI Company Summary generation
    - Peer comparison extraction
    - AI Scores generation
    - Analyst reports fetch
    """
    try:
        data = request.get_json(force=True)
        tick = data.get('ticker', '').strip().upper()
        years = data.get('years', 1)  # Default to 1 year, accepts 1, 3, or 5
        
        # Validate years parameter
        if years not in [1, 3, 5]:
            years = 1
        
        if not tick:
            return jsonify({'error': 'No ticker provided'}), 400
        
        log_progress(f"Starting chart analysis for {tick} ({years}Y)...")
        
        # 1. Get company name from yfinance (fast)
        company_name = tick
        try:
            yf_ticker = yf.Ticker(f"{tick}.NS")
            info = yf_ticker.info or {}
            company_name = info.get("longName") or info.get("shortName") or tick
            log_progress(f"Identified company: {company_name}")
        except Exception as e:
            print(f"WARN: yfinance name fetch failed for {tick}: {e}")
        
        # 2. Fetch TradingView data + calculate technical indicators
        # Use weekly data for 5Y, daily for 1Y/3Y
        interval = 'weekly' if years == 5 else 'daily'
        log_progress(f"Fetching {interval} price data from TradingView...")
        res = evaluate_ticker_signal(tick, interval=interval)
        
        if not res or res.get("Signal") in ["NO DATA", "INSUFFICIENT DATA"]:
            log_progress(f"No price data found for {tick}")
            return jsonify({'error': f'No price data found for {tick}'}), 404
        
        df = res["Data"]
        
        if df.empty or len(df) < 2:
            return jsonify({'error': f'Insufficient historical data for {tick}'}), 404
        
        # 3. Generate technical summary table
        log_progress("Generating technical summary...")
        technical_summary = generate_summary(res)
        
        # Clean the summary
        cleaned_summary = []
        if isinstance(technical_summary, list):
            for item in technical_summary:
                if isinstance(item, dict) and "key" in item and "value" in item:
                    cleaned_key = str(item["key"]).strip()
                    value = item["value"]
                    cleaned_value = str(value).strip() if isinstance(value, str) else value
                    cleaned_summary.append({"key": cleaned_key, "value": cleaned_value})
                else:
                    cleaned_summary.append(item)
        else:
            cleaned_summary = technical_summary
        
        # 4. Build all charts with selected duration
        log_progress("Building charts...")
        close_j = build_close_figure(df, company_name, years=years).to_json()
        hl_j = build_hl_figure(df, company_name, years=years).to_json()
        ema_j = build_ema_figure(df, company_name, years=years).to_json()
        rsi_j = build_rsi_figure(df, company_name, years=years).to_json()
        adl_j = build_adl_figure(df, company_name, years=years).to_json()
        rs_j = build_rs_figure(df, company_name, years=years).to_json()
        rsi_div_j = build_rsi_divergence_figure(df, company_name, years=years).to_json()
        
        log_progress(f"Chart analysis complete for {tick}!")
        
        return jsonify({
            'ticker': tick,
            'company_name': company_name,
            'summary': cleaned_summary,  # AI Chart Analysis table
            'chart_close_json': close_j,
            'chart_hl_json': hl_j,
            'chart_ema_json': ema_j,
            'chart_rsi_json': rsi_j,
            'chart_adl_json': adl_j,
            'chart_rs_json': rs_j,
            'chart_rsi_divergence_json': rsi_div_j
        })
        
    except Exception as e:
        print(f"ERROR in /analyze-chart: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# =====================================================================
# END: Lightweight Chart-Only Analysis Endpoint
# =====================================================================


# =====================================================================
# START: Async Per-Chart Summaries Endpoint
# =====================================================================

@app.route('/chart-summaries', methods=['POST'])
def chart_summaries():
    """
    Generate per-chart natural-language summaries.
    Called asynchronously by the frontend after charts have rendered.
    """
    try:
        data = request.get_json(force=True)
        tick = data.get('ticker', '').strip().upper()
        years = data.get('years', 1)
        if years not in [1, 3, 5]:
            years = 1
        if not tick:
            return jsonify({'error': 'No ticker provided'}), 400

        interval = 'weekly' if years == 5 else 'daily'
        res = evaluate_ticker_signal(tick, interval=interval)

        if not res or res.get('Signal') in ['NO DATA', 'INSUFFICIENT DATA']:
            return jsonify({'error': f'No data for {tick}'}), 404

        summaries = generate_per_chart_summaries(res)
        return jsonify({'chart_summaries': summaries})

    except Exception as e:
        print(f"ERROR in /chart-summaries: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# =====================================================================
# END: Async Per-Chart Summaries Endpoint
# =====================================================================


# =====================================================================
# START: Admin Batch Pre-Caching System (Overnight Pre-Caching)
# =====================================================================

# Import admin_required decorator from auth routes
from auth.routes import admin_required

# Global dict to track running precache jobs (in-memory for simplicity)
precache_jobs = {}

def run_batch_precache(job_id, tickers):
    """
    LIGHT Pre-Cache: Only fetches Screener.in data + AI Summary.
    Skips: TradingView, yfinance, Trendlyne, AI Scores
    Much faster: ~30-45 seconds per stock instead of ~2 minutes.
    """
    global precache_jobs
    
    results = []
    total = len(tickers)
    
    # Update job status
    precache_jobs[job_id] = {
        'status': 'running',
        'total': total,
        'completed': 0,
        'current': None,
        'results': []
    }
    
    for i, ticker in enumerate(tickers):
        try:
            # Track timing for this stock
            stock_start_time = time.time()
            
            # Update current ticker
            precache_jobs[job_id]['current'] = ticker
            precache_jobs[job_id]['completed'] = i
            precache_jobs[job_id]['current_start_time'] = stock_start_time
            
            print(f"LIGHT PRE-CACHE: [{i+1}/{total}] Starting {ticker}...")
            
            # ============================================================
            # STEP 1: Fetch ONLY Screener.in data (fundamentals + documents)
            # ============================================================
            is_consolidated_screener = False  # Default before try block
            try:
                from screener_fetcher import fetch_consolidated, fetch_latest_documents
                
                # Fetch tables (returns dict of DataFrames), company description, and top ratios
                tables_from_screener, company_description, top_ratios, is_consolidated_screener = fetch_consolidated(ticker)
                
                # Parse the fundamentals - convert DataFrames to JSON for frontend
                fund_data_for_frontend = {}
                fund_data_for_ai_context = {}
                if tables_from_screener:
                    for table_name, df_original_table in tables_from_screener.items():
                        # For frontend: JSON string format
                        fund_data_for_frontend[table_name] = df_original_table.reset_index().T.to_json(orient='split')
                        # For AI context: Clean dict format
                        temp_rows = df_original_table.reset_index().to_dict(orient='records')
                        cleaned_rows = []
                        for row_dict in temp_rows:
                            cleaned_row = {
                                (str(k).strip() if isinstance(k, str) else k): 
                                (str(v).strip() if isinstance(v, str) else v) 
                                for k, v in row_dict.items()
                            }
                            cleaned_rows.append(cleaned_row)
                        fund_data_for_ai_context[table_name] = cleaned_rows
                
                # Fetch latest documents (sync version)
                latest_documents = fetch_latest_documents(ticker)
                if not latest_documents:
                    latest_documents = []
                
                # Get key metrics from fundamentals
                key_metrics = extract_key_metrics_from_fundamentals(fund_data_for_ai_context, top_ratios=top_ratios)
                
                print(f"LIGHT PRE-CACHE: [{i+1}/{total}] {ticker} - Screener.in data fetched ✓")
                
            except Exception as e:
                print(f"LIGHT PRE-CACHE: [{i+1}/{total}] {ticker} - Screener.in failed: {e}")
                traceback.print_exc()
                fund_data_for_frontend = {}
                fund_data_for_ai_context = {}
                latest_documents = []
                key_metrics = {}
                company_description = ""
            
            # ============================================================
            # STEP 2: Generate AI Summary from fundamentals
            # ============================================================
            ai_summary_html = ""
            try:
                if fund_data_for_ai_context or company_description:
                    # Use existing AI summary function with company description
                    ai_summary_html = generate_ai_company_summary(ticker, company_description, tables_from_screener, latest_documents)
                    print(f"LIGHT PRE-CACHE: [{i+1}/{total}] {ticker} - AI Summary generated ✓")
            except Exception as e:
                print(f"LIGHT PRE-CACHE: [{i+1}/{total}] {ticker} - AI Summary failed: {e}")
                traceback.print_exc()
                ai_summary_html = f"<p>Analysis data for {ticker} has been cached. Full summary available on next search.</p>"
            
            # ============================================================
            # STEP 3: Build light cache object and store
            # ============================================================
            light_cache_data = {
                'ticker': ticker,
                'company_name': ticker,  # Will be updated on full analysis
                'company_summary_html': ai_summary_html,
                'fundamentals': fund_data_for_frontend,
                'documents': latest_documents,
                'key_metrics': key_metrics,
                'cached_at': datetime.now().isoformat(),
                'from_cache': False,
                'light_cache': True,  # Flag to indicate this is a light cache
                'is_consolidated': is_consolidated_screener,
                # Empty placeholders for full analysis data
                'summary': [],
                'chart_close_json': None,
                'chart_hl_json': None,
                'chart_ema_json': None,
                'chart_rsi_json': None,
                'chart_adl_json': None,
                'chart_rs_json': None,
                'ai_scores': [],
                'metric_charts': {},
                'scanx_data': None,
                'peer_comparison': {'company': {}, 'peers': []},
                'analyst_reports': []
            }
            
            # Cache for stock-based instant access (4 months = 10368000 seconds)
            stock_cache_key = f"stock_analysis_{ticker}"
            # Calculate total duration for this stock
            duration_seconds = int(time.time() - stock_start_time)
            
            try:
                frontend_compressed = zlib.compress(pickle.dumps(light_cache_data))
                cache.set(stock_cache_key, frontend_compressed, timeout=10368000)
                # Also write via Direct Redis for reliable admin scanning
                if DIRECT_REDIS_CLIENT:
                    try:
                        DIRECT_REDIS_CLIENT.setex(stock_cache_key, 10368000, frontend_compressed)
                    except Exception as e:
                        print(f"WARN: Direct Redis write failed for light cache {ticker}: {e}", file=sys.stderr)
                print(f"LIGHT PRE-CACHE: [{i+1}/{total}] {ticker} ✓ Cached successfully ({duration_seconds}s)")
                results.append({'ticker': ticker, 'status': 'success', 'duration_seconds': duration_seconds})
            except Exception as e:
                print(f"LIGHT PRE-CACHE: [{i+1}/{total}] {ticker} ✗ Cache write failed: {e}")
                results.append({'ticker': ticker, 'status': 'error', 'error': f'Cache write failed: {e}', 'duration_seconds': duration_seconds})
            
        except Exception as e:
            duration_seconds = int(time.time() - stock_start_time)
            print(f"LIGHT PRE-CACHE: [{i+1}/{total}] {ticker} ✗ Failed: {e} ({duration_seconds}s)")
            traceback.print_exc()
            results.append({'ticker': ticker, 'status': 'error', 'error': str(e), 'duration_seconds': duration_seconds})
        
        # Rate limiting: 15 second delay between stocks (shorter since lighter workload)
        if i < total - 1:
            print(f"LIGHT PRE-CACHE: Waiting 15 seconds before next ticker...")
            time.sleep(15)
    
    # Mark job as complete
    precache_jobs[job_id] = {
        'status': 'complete',
        'total': total,
        'completed': total,
        'current': None,
        'results': results
    }
    
    # Also store in Redis cache for persistence (24 hours)
    try:
        cache.set(f"precache_job_{job_id}", results, timeout=86400)
    except:
        pass
    
    success_count = len([r for r in results if r['status'] == 'success'])
    print(f"PRE-CACHE JOB COMPLETE: {job_id} - {success_count}/{total} successful")


@app.route('/admin/batch-precache', methods=['POST'])
@admin_required
def batch_precache():
    """
    Admin endpoint to trigger sequential pre-caching for a list of stock tickers.
    Rate-limited to avoid overwhelming external APIs.
    
    Request body:
    {
        "tickers": "RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK"
    }
    """
    try:
        data = request.get_json(force=True)
        tickers_str = data.get('tickers', '')
        
        # Parse comma-separated tickers
        tickers = [t.strip().upper() for t in tickers_str.split(',') if t.strip()]
        
        if not tickers:
            return jsonify({'error': 'No tickers provided'}), 400
        
        # Limit to 500 stocks per job (uses only ~15MB Redis, improves performance)
        MAX_TICKERS = 500
        if len(tickers) > MAX_TICKERS:
            return jsonify({'error': f'Maximum {MAX_TICKERS} tickers per job. You provided {len(tickers)}.'}), 400
        
        # Generate job ID
        job_id = str(uuid.uuid4())[:8]  # Short ID for easier reference
        
        # Start background thread
        job_thread = threading.Thread(
            target=run_batch_precache,
            args=(job_id, tickers),
            daemon=True
        )
        job_thread.start()
        
        # Calculate estimated time (~2 min per stock including delays)
        estimated_minutes = len(tickers) * 2
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'tickers': tickers,
            'count': len(tickers),
            'status': 'started',
            'estimated_minutes': estimated_minutes
        })
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/admin/batch-precache/status/<job_id>', methods=['GET'])
@admin_required
def get_precache_status(job_id):
    """
    Check status of a pre-cache job.
    """
    # Check in-memory first
    if job_id in precache_jobs:
        job = precache_jobs[job_id]
        
        # Calculate elapsed time for current stock
        elapsed_seconds = None
        if job.get('current_start_time') and job['status'] == 'running':
            elapsed_seconds = int(time.time() - job['current_start_time'])
        
        return jsonify({
            'job_id': job_id,
            'status': job['status'],
            'total': job['total'],
            'completed': job['completed'],
            'current': job['current'],
            'elapsed_seconds': elapsed_seconds,
            'results': job['results'] if job['status'] == 'complete' else []
        })
    
    # Check Redis cache
    try:
        results = cache.get(f"precache_job_{job_id}")
        if results:
            return jsonify({
                'job_id': job_id,
                'status': 'complete',
                'total': len(results),
                'completed': len(results),
                'current': None,
                'results': results
            })
    except:
        pass
    
    return jsonify({'error': 'Job not found'}), 404


@app.route('/admin/batch-precache/jobs', methods=['GET'])
@admin_required
def list_precache_jobs():
    """
    List all known pre-cache jobs.
    """
    jobs = []
    for job_id, job in precache_jobs.items():
        jobs.append({
            'job_id': job_id,
            'status': job['status'],
            'total': job['total'],
            'completed': job['completed'],
            'current': job['current']
        })
    return jsonify({'jobs': jobs})


# =====================================================================
# END: Admin Batch Pre-Caching System
# =====================================================================

# =====================================================================
# START: Admin Cache Management API
# =====================================================================

@app.route('/api/admin/light-cache-tickers', methods=['GET'])
@admin_required
def api_light_cache_tickers():
    """
    List all tickers currently in the light cache.
    Scans Redis (if available) or local memory.
    """
    light_tickers = []
    try:
        # 1. Check Redis if available
        if DIRECT_REDIS_CLIENT:
            # Use SCAN for better performance on large datasets
            cursor = 0
            while True:
                cursor, keys = DIRECT_REDIS_CLIENT.scan(cursor, match="*stock_analysis_*", count=100)
                for key in keys:
                    try:
                        # Direct Redis keys don't include the prefix used by flask_caching
                        # but we need to check if it's a light cache
                        raw_data = DIRECT_REDIS_CLIENT.get(key)
                        if raw_data:
                            # Try zlib+pickle first (our manual compression format),
                            # then plain pickle (Flask-Caching's internal format)
                            data = None
                            try:
                                data = pickle.loads(zlib.decompress(raw_data))
                            except zlib.error:
                                try:
                                    data = pickle.loads(raw_data)
                                except Exception:
                                    pass
                            
                            if isinstance(data, dict) and data.get('light_cache'):
                                light_tickers.append({
                                    'ticker': data.get('ticker', key.decode('utf-8').replace('flask_cache_stock_analysis_', '').replace('stock_analysis_', '')),
                                    'cached_at': data.get('cached_at', 'Unknown')
                                })
                    except Exception as e:
                        print(f"DEBUG: Failed to parse Redis key {key}: {e}", file=sys.stderr)
                if cursor == 0:
                    break
        
        # 2. Check Local Cache (Fallback or dual-mode)
        for ticker, entry in LOCAL_ANALYSIS_CACHE.items():
            data = entry.get('data')
            if isinstance(data, dict) and data.get('light_cache'):
                # Avoid duplicates if also in Redis
                if not any(t['ticker'] == ticker for t in light_tickers):
                    light_tickers.append({
                        'ticker': ticker,
                        'cached_at': data.get('cached_at', 'Unknown')
                    })
                    
        # Sort by ticker
        light_tickers.sort(key=lambda x: x['ticker'])
        
        return jsonify({
            'success': True,
            'count': len(light_tickers),
            'tickers': light_tickers
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/clear-cache', methods=['POST'])
@admin_required
def api_clear_cache():
    """
    Clear cache for a specific ticker or all light cache entries.
    """
    try:
        data = request.get_json(force=True)
        target = data.get('target', '') # ticker or 'all_light'
        
        if not target:
            return jsonify({'error': 'No target specified'}), 400
            
        cleared_count = 0
        
        if target == 'all_light':
            # Clear all light caches
            # Redis
            if DIRECT_REDIS_CLIENT:
                cursor = 0
                while True:
                    cursor, keys = DIRECT_REDIS_CLIENT.scan(cursor, match="*stock_analysis_*", count=100)
                    for key in keys:
                        try:
                            raw_data = DIRECT_REDIS_CLIENT.get(key)
                            if raw_data:
                                data = None
                                try:
                                    data = pickle.loads(zlib.decompress(raw_data))
                                except zlib.error:
                                    try:
                                        data = pickle.loads(raw_data)
                                    except Exception:
                                        pass
                                if isinstance(data, dict) and data.get('light_cache'):
                                    DIRECT_REDIS_CLIENT.delete(key)
                                    cleared_count += 1
                        except:
                            pass
                    if cursor == 0:
                        break
            
            # Local
            to_delete = [t for t, e in LOCAL_ANALYSIS_CACHE.items() if e.get('data', {}).get('light_cache')]
            for t in to_delete:
                del LOCAL_ANALYSIS_CACHE[t]
                cleared_count += 1
                
            return jsonify({'success': True, 'message': f'Cleared {cleared_count} light cache entries.'})
            
        else:
            # Clear specific ticker
            ticker_upper = target.upper()
            key = f"stock_analysis_{ticker_upper}"
            
            # Redis
            if DIRECT_REDIS_CLIENT:
                # Try both prefixed and non-prefixed
                DIRECT_REDIS_CLIENT.delete(key)
                DIRECT_REDIS_CLIENT.delete(f"flask_cache_{key}")
                cleared_count += 1
            
            # Local
            if ticker_upper in LOCAL_ANALYSIS_CACHE:
                del LOCAL_ANALYSIS_CACHE[ticker_upper]
                cleared_count += 1
                
            if cleared_count > 0:
                return jsonify({'success': True, 'message': f'Cleared cache for {ticker_upper}'})
            else:
                return jsonify({'error': f'No cache found for {ticker_upper}'}), 404
                
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# =====================================================================
# END: Admin Cache Management API
# =====================================================================

# =====================================================================
# BUDGET LIVE TRANSCRIPTION - SocketIO Endpoints
# =====================================================================
from budget_live import (
    BudgetLiveSession, 
    create_session, 
    get_session, 
    remove_session,
    analyze_budget_text
)
from prompts import get_budget_chat_prompt

# Store a single global budget session for 2026
GLOBAL_BUDGET_SESSION = BudgetLiveSession("global_2026")

# Auto-load the default budget transcript so it's preloaded on every deployment
try:
    _transcript_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'budget_transcript.txt')
    if os.path.exists(_transcript_path):
        with open(_transcript_path, 'r', encoding='utf-8') as _f:
            _default_transcript = _f.read()
        if _default_transcript.strip():
            GLOBAL_BUDGET_SESSION.ingest_full_transcript(_default_transcript)
            print("INFO: Budget 2026 transcript preloaded successfully from budget_transcript.txt", file=sys.stderr)
        else:
            print("WARN: budget_transcript.txt is empty. Budget section will start empty.", file=sys.stderr)
    else:
        print("WARN: budget_transcript.txt not found. Budget section will start empty.", file=sys.stderr)
except Exception as _e:
    print(f"WARN: Failed to preload budget transcript: {_e}", file=sys.stderr)

@app.route('/api/budget/chat', methods=['POST'])
def api_budget_chat():
    """AI Chatbot for the Budget section using gpt-5-mini"""
    try:
        data = request.get_json(force=True)
        user_question = data.get('question', '').strip()
        
        if not user_question:
            return jsonify({'error': 'No question provided'}), 400
        
        # Question Limit for Guests
        from flask import session as flask_session
        is_logged_in = 'user_id' in flask_session
        
        if not is_logged_in:
            guest_count = flask_session.get('budget_guest_count', 0)
            if guest_count >= 10:
                return jsonify({
                    'error': 'Chat limit reached (10 questions). Please login for unlimited access.',
                    'limit_reached': True
                }), 403
            flask_session['budget_guest_count'] = guest_count + 1
            print(f"DEBUG: Guest Budget Chat ({guest_count + 1}/10)", file=sys.stderr)

        history = data.get('history', [])
        
        # Get full context from the global budget session
        budget_context = GLOBAL_BUDGET_SESSION.get_ai_context()
        
        # STEP 1: Research Phase (Perplexity Sonar)
        # Use history for context if available
        if history:
            research_messages = [
                {"role": "system", "content": "You are a research assistant. Find the latest news and market reactions related to the conversation about the Indian Budget 2026. Provide a concise summary with citations."}
            ] + history
        else:
            research_messages = [
                {"role": "system", "content": "You are a research assistant. Find the latest news and market reactions related to the user's question about the Indian Budget 2026. Provide a concise summary with citations."},
                {"role": "user", "content": user_question}
            ]
        
        web_results = None
        try:
            print(f"INFO: Calling Sonar-Pro for Budget Research: {user_question[:100]}...", file=sys.stderr)
            web_results = call_perplexity_api(
                messages=research_messages,
                model="sonar-pro",
                enable_pro_search=True,
                timeout=60
            )
        except Exception as research_error:
            print(f"WARN: Budget Research failed: {research_error}. Proceeding with local context only.", file=sys.stderr)

        # STEP 2: Synthesis Phase (GPT-5-Mini)
        system_prompt = get_budget_chat_prompt(budget_context, web_results=web_results)
        
        if history:
            synthesis_messages = [
                {"role": "system", "content": system_prompt}
            ] + history
        else:
            synthesis_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_question}
            ]
        
        print(f"INFO: Calling GPT-5-Mini for Budget Synthesis...", file=sys.stderr)
        answer = call_generative_ai_model(
            model="gpt-5-mini",
            messages=synthesis_messages,
            temperature=1
        )
        
        return jsonify({
            'answer': answer,
            'web_researched': bool(web_results),
            'status': 'success'
        })
        
    except Exception as e:
        print(f"CRITICAL ERROR in /api/budget/chat: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify({'error': f'An unexpected error occurred: {str(e)}'}), 500

@socketio.on('connect')
def handle_connect():
    print(f"INFO: Client connected: {request.sid}")
    emit('connected', {'status': 'ok', 'sid': request.sid})

@socketio.on('join_budget_room')
def handle_join_budget_room():
    """Add user to the shared budget room and send current state"""
    from flask_socketio import join_room
    join_room('budget_room')
    print(f"INFO: Client {request.sid} joined budget_room")
    
    # Send current state to late joiners
    state = GLOBAL_BUDGET_SESSION.get_state()
    emit('initial_state', state)

@socketio.on('disconnect')
def handle_disconnect():
    print(f"INFO: Client disconnected: {request.sid}")
    # No aggressive cleanup - session is global

@socketio.on('start_budget_session')
def handle_start_budget_session(data=None):
    """Start the shared budget transcription session (Admin only)"""
    # Security: Only nikhil.banthiya@gmail.com should be able to start/stop the session
    # We can check session['user_email'] or an admin flag
    from flask import session as flask_session
    user_email = flask_session.get('user_email')
    
    if user_email != 'nikhil.banthiya@gmail.com':
        print(f"WARN: Unauthorized start_budget_session attempt by {user_email}")
        emit('session_error', {'error': 'Only the admin can control the live session.'})
        return

    print(f"INFO: Starting global budget session by {user_email}")
    
    try:
        if not GLOBAL_BUDGET_SESSION.is_active:
            GLOBAL_BUDGET_SESSION.start()
        
        socketio.emit('session_started', {
            'status': 'ok',
            'message': 'Live budget transcription started.'
        }, room='budget_room')
    except Exception as e:
        print(f"ERROR: Failed to start/reconnect budget session: {e}")
        emit('session_error', {'error': str(e)})

@socketio.on('audio_chunk')
def handle_audio_chunk(data=None):
    """Process incoming audio chunk and broadcast update to all users"""
    if not data or not GLOBAL_BUDGET_SESSION.is_active:
        return
        
    try:
        # Extract audio data
        audio_b64 = data.get('audio', '')
        mime_type = data.get('mime_type', 'audio/webm')
        
        if audio_b64:
            import base64
            audio_bytes = base64.b64decode(audio_b64)
            
            # Process the chunk (using threading mode for SocketIO)
            result = GLOBAL_BUDGET_SESSION.process_audio_chunk(audio_bytes, mime_type)
            
            # Broadcast transcription update to ALL users in the budget room
            socketio.emit('transcription_update', {
                'transcript': result.get('transcript', ''),
                'chunk_id': result.get('chunk_id', 0),
                'highlights': result.get('highlights', []),
                'sectors': result.get('sectors', {})
            }, room='budget_room')
        else:
            emit('transcription_update', {'transcript': '', 'error': 'No audio data received'})
            
    except Exception as e:
        print(f"ERROR: Processing audio chunk: {e}")
        emit('transcription_update', {'transcript': '', 'error': str(e)})

@socketio.on('stop_budget_session')
def handle_stop_budget_session(data=None):
    """Stop the budget transcription session (Admin only)"""
    from flask import session as flask_session
    user_email = flask_session.get('user_email')
    
    if user_email != 'nikhil.banthiya@gmail.com':
        print(f"WARN: Unauthorized stop_budget_session attempt by {user_email}")
        emit('session_error', {'error': 'Only the admin can control the live session.'})
        return

    print(f"INFO: Stopping global budget session by {user_email}")
    summary = GLOBAL_BUDGET_SESSION.stop()
    
    socketio.emit('session_stopped', {
        'status': 'ok',
        'summary': summary
    }, room='budget_room')

@socketio.on('get_budget_summary')
def handle_get_budget_summary(data=None):
    """Get current session summary"""
    # Anyone can get the summary of the global session
    summary = GLOBAL_BUDGET_SESSION.get_summary()
    emit('budget_summary', summary)

# REST endpoints for budget analysis
@app.route('/api/budget/analyze', methods=['POST'])
def api_budget_analyze():
    """Analyze budget text on demand"""
    try:
        data = request.get_json(force=True)
        text = data.get('text', '')
        
        if not text:
            return jsonify({'error': 'No text provided'}), 400
        
        result = analyze_budget_text(text)
        return jsonify(result)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/budget/upload-transcript', methods=['POST'])
def upload_budget_transcript():
    """Upload a full transcript for analysis (Admin only)"""
    from flask import session as flask_session
    user_email = flask_session.get('user_email')
    
    if user_email != 'nikhil.banthiya@gmail.com':
        return jsonify({'error': 'Admin access required'}), 403
    
    if 'file' not in request.files:
        return jsonify({'error': 'No file segment found'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    if file:
        filename = file.filename.lower()
        try:
            if filename.endswith('.txt'):
                content = file.read().decode('utf-8')
            elif filename.endswith('.docx'):
                from docx import Document
                import io
                content = ""
                doc = Document(io.BytesIO(file.read()))
                content = "\n".join([para.text for para in doc.paragraphs])
            else:
                return jsonify({'error': 'Invalid file format. Only .txt and .docx allowed.'}), 400

            if not content.strip():
                return jsonify({'error': 'File is empty'}), 400

            state = GLOBAL_BUDGET_SESSION.ingest_full_transcript(content)
            
            # Broadcast the updated state to all participants
            socketio.emit('initial_state', state, room='budget_room')
            
            return jsonify({'success': True, 'message': 'Transcript uploaded and analyzed.'})
        except Exception as e:
            print(f"ERROR: Transcript ingestion failed: {e}")
            return jsonify({'error': str(e)}), 500
            
    return jsonify({'error': 'No file selected'}), 400

@app.route('/api/budget/deep-scan', methods=['POST'])
def api_budget_deep_scan():
    """Trigger a full re-analysis of the entire transcript (Admin only)"""
    from flask import session as flask_session
    user_email = flask_session.get('user_email')
    
    if user_email != 'nikhil.banthiya@gmail.com':
        return jsonify({'error': 'Admin access required'}), 403
    
    try:
        state = GLOBAL_BUDGET_SESSION.deep_scan()
        
        # Broadcast the comprehensive updated state
        socketio.emit('initial_state', state, room='budget_room')
        
        return jsonify({'success': True, 'message': 'Deep scan complete across the whole transcript.'})
    except Exception as e:
        print(f"ERROR: Deep scan failed: {e}")
        return jsonify({'error': str(e)}), 500

# Serve the budget.html page
@app.route('/budget')
def serve_budget_page():
    """Serve the Live Budget transcription page"""
    return app.send_static_file('budget.html')

# =====================================================================
# END: Budget Live Transcription
# =====================================================================

# =====================================================================
# START: Agent Marketplace
# =====================================================================

# Serve the Agent Marketplace page
@app.route('/agents')
def serve_agents_page():
    """Serve the Agent Marketplace page"""
    return send_from_directory('.', 'agents.html')

# Register Concall Agent routes
from agents.concall_agent import register_concall_routes
from screener_fetcher import fetch_latest_documents_async, get_text_from_pdf_url_async, fetch_forensic_documents_async
register_concall_routes(app, call_gemini_api, fetch_latest_documents_async, get_text_from_pdf_url_async)

# Register Forensic Agent routes
from agents.forensic_agent import register_forensic_routes
register_forensic_routes(app, call_gemini_api, call_perplexity_api, get_any_cache, fetch_forensic_documents_async, get_analysis_for_ticker)

print("INFO: Agent Marketplace routes registered (Concall Agent, Forensic Agent)", file=sys.stderr)

# =====================================================================
# END: Agent Marketplace
# =====================================================================

if __name__ == '__main__':
    # Use socketio.run for WebSocket support
    socketio.run(app, host='0.0.0.0', port=8000, debug=True)

# Wrap the WSGI app in ASGI middleware for Uvicorn
# app = ASGIMiddleware(app)