#!/usr/bin/env python3
"""
handler.py — Technical analysis web-server

Before running, ensure you have installed system TA-Lib and Python packages:

1. System-level TA-Lib (Linux):
   sudo bash -c "curl -L https://anaconda.org/conda-forge/libta-lib/0.4.0/download/linux-64/libta-lib-0.4.0-h166bdaf_1.tar.bz2 \
     | tar xj -C /usr/lib/x86_64-linux-gnu/ lib --strip-components=1"

2. Python packages:
   pip install flask flask-cors tradingview-datafeed yfinance pandas numpy matplotlib openai yfinance pdfplumber plotly matplotlib scikit-learn feedparser openai google-generativeai
"""
import sys, os
sys.path.append(os.path.dirname(__file__))
from prompts import get_central_brain_prompt, get_planning_system_prompt, get_answering_system_prompt

import traceback
import time
import io
import base64
from datetime import datetime
import asyncio
import httpx
import uuid

from flask import send_from_directory

from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_caching import Cache
from flask_compress import Compress

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

from tech_calculations import (
    evaluate_ticker_signal,
    generate_summary,
    build_close_figure,
    build_hl_figure,
    build_ema_figure,
    build_rsi_figure,
    build_adl_figure,
    build_rs_figure
)

# right below your Flask-app initialization:
# last_analysis: dict = {}

# Initialize Flask app and enable CORS
app = Flask(__name__, static_folder='.', static_url_path='')
CORS(app)
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

# --- FINAL, ROBUST CACHE CONFIGURATION ---

# Get the raw connection string from the environment variable set in Azure
azure_redis_conn_string = os.getenv("CACHE_REDIS_URL")

if azure_redis_conn_string:
    print("INFO: Found Azure Redis connection string. Parsing for Python redis library.")
    
    # This new parser is much safer and handles different connection string formats.

    try:
        # Split the string by commas
        parts = azure_redis_conn_string.split(',')
        
        # The host and port are always the first part
        host_part = parts[0]
        
        # Robustly find password and ssl
        password = None
        ssl_enabled = False
        
        for part in parts[1:]:
            # Use split('=', 1) to ensure we only split on the FIRST equals sign
            # This protects passwords that contain '=' characters (like base64)
            if '=' in part:
                key, value = part.split('=', 1)
                if key.lower() == 'password':
                    password = value
                elif key.lower() == 'ssl':
                    ssl_enabled = value.lower() == 'true'

        if not password:
            raise ValueError("Password not found in Redis connection string")

        # Use 'rediss://' for SSL connections, which Azure requires
        scheme = "rediss://" if ssl_enabled else "redis://"
        
        # Construct the final, standard Redis URL
        # host_part looks like "name.redis.cache.windows.net:6380"
        formatted_redis_url = f"{scheme}:{password}@{host_part}"

        config = {
            "CACHE_TYPE": "RedisCache",
            "CACHE_DEFAULT_TIMEOUT": 21600, # 6 hours
            "CACHE_REDIS_URL": formatted_redis_url
        }
        print("INFO: Configuring cache for PRODUCTION (Redis)")

    except Exception as e:
        print(f"CRITICAL ERROR: Failed to parse Redis connection string. Error: {e}")
        # Fallback to SimpleCache if parsing fails
        config = {"CACHE_TYPE": "SimpleCache"}

    # try:
    #     # Split the string by commas to separate the parts
    #     parts = azure_redis_conn_string.split(',')
        
    #     # The host and port are always the first part
    #     host, port = parts[0].split(':')
        
    #     # Find the password and ssl parts in the rest of the string
    #     # We create a dictionary of the other options
    #     options = {p.split('=')[0]: p.split('=')[1] for p in parts[1:]}
        
    #     password = options.get('password')
    #     ssl = options.get('ssl', 'false').lower() == 'true'

    #     if not password:
    #         raise ValueError("Password not found in Redis connection string")

    #     # Use 'rediss://' for SSL connections, which Azure requires
    #     scheme = "rediss://" if ssl else "redis://"
        
    #     # Construct the final, standard Redis URL
    #     formatted_redis_url = f"{scheme}:{password}@{host}:{port}"

    #     config = {
    #         "CACHE_TYPE": "RedisCache",
    #         "CACHE_DEFAULT_TIMEOUT": 21600, # 6 hours
    #         "CACHE_REDIS_URL": formatted_redis_url
    #     }
    #     print("INFO: Configuring cache for PRODUCTION (Redis)")

    # except Exception as e:
    #     print(f"CRITICAL ERROR: Failed to parse Redis connection string. Error: {e}")
    #     # Fallback to SimpleCache if parsing fails, so the app doesn't crash
    #     config = {"CACHE_TYPE": "SimpleCache"}
else:
    # Fallback for local development
    print("INFO: CACHE_REDIS_URL not found. Configuring cache for DEVELOPMENT (SimpleCache)")
    config = {
        "CACHE_TYPE": "SimpleCache",
        "CACHE_DEFAULT_TIMEOUT": 3600
    }

app.config.from_mapping(config)
cache = Cache(app)

# Configure a simple in-memory cache.
# Data will be cached for 6 hours (21600 seconds).
# config = {
#     "CACHE_TYPE": "SimpleCache",
#     "CACHE_DEFAULT_TIMEOUT": 21600
# }
# app.config.from_mapping(config)
# cache = Cache(app)

from flask import jsonify

@app.route('/debug/env', methods=['GET'])
def debug_env():
    keys = [
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT",
        "PERPLEXITY_API_KEY",
        "GOOGLE_API_KEY",
    ]
    # returns True/False (no secrets leaked)
    return jsonify({k: bool(os.getenv(k)) for k in keys})

# Serve front-end HTML
@app.route('/')
def index():
    return send_from_directory('.', 'app.html')

# # at the top of handler.py
# import os
# import openai

# =====================================================================
# START: API Configuration and Multi-Model Handling
# =====================================================================

import openai
#from perplexity import Perplexity 
import google.generativeai as genai
from google.generativeai.types import Tool 

# Securely load API keys from environment variables
openai.api_key = os.getenv("OPENAI_API_KEY")
PERPLEXITY_API_KEY = os.getenv("PERPLEXITY_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# Configure Google Gemini
if GOOGLE_API_KEY:
    genai.api_key=GOOGLE_API_KEY

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

def call_openai_api(messages, model="gpt-4.1-mini", expect_json_format_flag=False, temperature=1):
    if not openai.api_key:
        raise ValueError("OpenAI API key is not configured.")
    try:
        completion_params = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        # Use OpenAI's JSON mode for reliable structured output
        if expect_json_format_flag and ("-turbo" in model or "-o" in model):
            completion_params["response_format"] = {"type": "json_object"}

        response = openai.chat.completions.create(**completion_params)
        return response.choices[0].message.content
    except Exception as e:
        print(f"ERROR in call_openai_api: {e}")
        raise

def call_perplexity_api(messages, model="sonar", temperature=1):
    if not PERPLEXITY_API_KEY:
        raise ValueError("Perplexity API key is not configured.")
    try:
        url = "https://api.perplexity.ai/chat/completions"
        
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        
        headers = {
            "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
            "Content-Type": "application/json"
        }
        
        response = requests.post(url, headers=headers, json=payload)
        
        # Check for HTTP errors
        response.raise_for_status()
        
        # Ensure we have valid JSON
        if response.content.strip():
            return response.json()['choices'][0]['message']['content']
        else:
            raise ValueError("Empty response from Perplexity API")
            
    except requests.exceptions.JSONDecodeError:
        print(f"Non-JSON response: {response.text}")
        raise ValueError(f"Invalid JSON response from Perplexity API: {response.text}")
    except Exception as e:
        print(f"ERROR in call_perplexity_api: {e}")
        raise



def call_gemini_api(messages, model="gemini-2.5-flash-preview-05-20", temperature=1, use_google_search=False):
    if not GOOGLE_API_KEY:
        raise ValueError("Google Gemini API key is not configured.")
    try:
        # Configure tools based on the Latest News parameter
        tools = [Tool(google_search_retrieval={})] if use_google_search else None # <--- USE THIS LINE INSTEAD
        # tools = [Tool.from_google_search_retrieval()] if use_google_search else None
        # Gemini has stricter safety settings; we set them to be permissive for financial analysis.
        safety_settings = [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ]
        gemini_model = genai.GenerativeModel(model)
        gemini_messages = convert_to_gemini_format(messages)
        
        response = gemini_model.generate_content(
            gemini_messages,
            generation_config=genai.types.GenerationConfig(temperature=temperature),
            safety_settings=safety_settings,
            tools=tools
        )
        return response.text
    except Exception as e:
        print(f"ERROR in call_gemini_api: {e}")
        raise

def call_generative_ai_model(model, messages, temperature=1):
    """
    Dispatcher function to call the appropriate AI model API.
    """
    log_progress(f"Dispatching request to model: {model}")
    try:
        if model.startswith('gpt-') or model.startswith('o4-'):
            return call_openai_api(messages, model=model, temperature=temperature)
        elif model.startswith('pplx-') or model.startswith('llama-') or model.startswith('r1-') or model.startswith('pplx-') or model.startswith('sonar'):
            return call_perplexity_api(messages, model=model, temperature=temperature)
        elif model.startswith('gemini-'):
            return call_gemini_api(messages, model=model, temperature=temperature)
        else:
            # Default to a reliable, cheap model if the selection is unknown
            print(f"WARN: Unknown model '{model}', defaulting to 'gpt-4o-mini'.")
            return call_openai_api(messages, model='gpt-4o-mini', temperature=temperature)
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
                    selected_rows_for_table = table_data
                elif isinstance(requested_metrics, list) and table_data:
                    selected_rows_for_table = [
                        row for row in table_data if row.get("") in requested_metrics
                    ]
                
                if not selected_rows_for_table:
                    continue

                requested_periods = spec.get("periods")
                final_rows_for_table = []

                if requested_periods == "all" or not requested_periods:
                    final_rows_for_table = selected_rows_for_table
                elif isinstance(requested_periods, list) and selected_rows_for_table:
                    for row in selected_rows_for_table:
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

    if not retrieved_something and "error" not in focused_data :
        focused_data["retrieval_info"] = "AI plan's 'retrieve_data' section did not specify any known data sections or the requested data was not found."
        # print(f"WARN: No specific data retrieved based on AI plan's retrieve_data. Plan section was: {json.dumps(plan, indent=2)}")

    return focused_data


# def call_openai_api(messages, model="o4-mini", expect_json_format_flag=False, temperature=1):
#     # ... (Your existing call_openai_api function remains the same) ...
#     try:
#         completion_params = {
#             "model": model,
#             "messages": messages,
#             "temperature": temperature,
#         }
#         if expect_json_format_flag and (model == "o4-mini" or model.startswith("gpt-4-turbo") or model.startswith("gpt-3.5-turbo-1106")):
#             completion_params["response_format"] = {"type": "json_object"}

#         # print(f"DEBUG: Making OpenAI call to model {model}. Expect JSON mode: {expect_json_format_flag}. Messages: {json.dumps(messages, indent=2)}")
#         response = openai.chat.completions.create(**completion_params)
#         content = response.choices[0].message.content
#         # print(f"DEBUG: OpenAI Raw Response Content:\n{content}") 
#         return content
#     except openai.RateLimitError as rle:
#         print(f"ERROR: OpenAI Rate Limit Error: {rle}")
#         raise rle # Re-raise to be caught by the route's error handler
#     except Exception as e:
#         print(f"ERROR: Error in OpenAI API call: {e}")
#         traceback.print_exc()
#         # It's better to raise a custom exception or re-raise 'e' so the route handler can give a 500
#         raise Exception(f"OpenAI API call failed: {str(e)}")

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

# new endpoint, below your /analyze route

### 2. The Complete `def chat` Function from `handler.py`

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.get_json(force=True)
        user_question = data.get('question', '').strip()
        selected_model = data.get('model', 'o4-mini')
        analysis_key = data.get('analysis_key')

        if not analysis_key:
             return jsonify({'answer': 'Analysis key is missing. Please analyze a stock first.'}), 200

        # --- ROBUST RETRIEVAL LOGIC ---
        last_analysis = None
        retry_count = 0
        max_retries = 2
        
        while retry_count < max_retries:
            try:
                print(f"INFO: Fetching data from Redis for Chat (Attempt {retry_count+1})...")
                last_analysis = cache.get(analysis_key)
                if last_analysis:
                    break # Success!
            except Exception as e:
                print(f"WARN: Redis fetch failed on attempt {retry_count+1}: {e}")
                time.sleep(0.5) # Wait half a second before retrying
            retry_count += 1
            
        # If still None after retries, we can't proceed
        if not last_analysis or not last_analysis.get("ticker"):
            print("ERROR: Could not retrieve analysis data from cache.")
            return jsonify({'answer': 'I cannot access the analysis data right now (Cache Timeout). Please try refreshing the page and analyzing the stock again.'}), 200
        # ------------------------------

        print(f"AI chatbot received question with selected model: {selected_model}")


    # try:
    #     data = request.get_json(force=True)
    #     user_question = data.get('question', '').strip()
    #     selected_model = data.get('model', 'o4-mini')
        
    #     # --- THIS IS THE NEW LOGIC ---
    #     # Get the unique analysis key from the frontend request
    #     analysis_key = data.get('analysis_key')
    #     if not analysis_key:
    #          return jsonify({'answer': 'Analysis key is missing. Please analyze a stock first.'}), 200

    #     # Retrieve the specific analysis data using the key
    #     last_analysis = cache.get(analysis_key)
    #     # --- END OF NEW LOGIC ---

    #     print(f"AI chatbot received question with selected model: {selected_model}")


        if not user_question:
            return jsonify({'error': 'No question provided'}), 400
        if not last_analysis or not last_analysis.get("ticker"):
            return jsonify({'answer': 'Please analyze a stock first. No data context is available.'}), 200

        is_best_mode = selected_model == 'best'
        central_brain_plan = None
        parsed_plan = {}
        ai_plan_json_str = "{}"
        thought_process_str = "No thought process generated."

        if is_best_mode:
            # =================================================================
            # STAGE 1: CENTRAL BRAIN - STRATEGIC PLANNING
            # =================================================================
            log_progress("Central Brain is analyzing the query and forming a strategy...")
            central_brain_prompt = get_central_brain_prompt()
            brain_messages = [
                {"role": "system", "content": central_brain_prompt},
                {"role": "user", "content": f"User Question: \"{user_question}\""}
            ]
            
            central_brain_response_str = call_generative_ai_model("gpt-4.1-mini", brain_messages, temperature=1)

            try:
                json_match = re.search(r"```json\s*([\s\S]*?)\s*```", central_brain_response_str, re.MULTILINE)
                
                if json_match:
                    # If a block is found, extract and parse it
                    central_brain_plan_str = json_match.group(1)
                    central_brain_plan = json.loads(central_brain_plan_str)
                else:
                    # If no block is found, try to parse the entire response string directly
                    # This handles cases where the model returns pure JSON without markdown
                    central_brain_plan = json.loads(central_brain_response_str)

                thought_process_str = central_brain_plan.get("thought_process", "Central Brain planning complete.")
                print(f"--- Central Brain Plan ---\n{json.dumps(central_brain_plan, indent=2)}\n--------------------------")
                
            except (json.JSONDecodeError, AttributeError) as e:
                print(f"ERROR: Could not parse Central Brain plan. Error: {e}. Response: {central_brain_response_str}")
                return jsonify({'error': 'Failed to generate a strategic plan. Please try rephrasing your question.'}), 500

            # =================================================================
            # STAGE 2: TACTICAL PLANNER - CREATING EXECUTABLE JSON
            # =================================================================
            log_progress("Tactical Planner is creating a detailed data retrieval plan...")
            schema_description = get_data_schema_description(last_analysis)
            planning_system_prompt = get_planning_system_prompt()
            
            planner_user_content = (
                f"User Question: \"{user_question}\"\n\n"
                f"Central Brain Directives:\n{json.dumps(central_brain_plan, indent=2)}\n\n"
                f"Data Schema Description:\n{schema_description}\n\n"
                f"Full 'last_analysis' context (for reference, e.g., current price):\n{json.dumps(last_analysis, indent=2, default=str)[:2000]}"
            )

            planner_messages = [
                {"role": "system", "content": planning_system_prompt},
                {"role": "user", "content": planner_user_content}
            ]
            
            tactical_plan_response_str = call_openai_api(planner_messages, model='o4-mini', expect_json_format_flag=True, temperature=1)
            
            # ===================================================================
            # START: CORRECTED TACTICAL PLAN PARSING LOGIC
            # ===================================================================
            try:
                # Use regex to robustly find the JSON block, even if the model includes extra text.
                json_match = re.search(r"```json\s*([\s\S]*?)\s*```", tactical_plan_response_str, re.MULTILINE)
                
                if json_match:
                    ai_plan_json_str = json_match.group(1)
                    parsed_plan = json.loads(ai_plan_json_str)
                else:
                    # Fallback: If no ```json``` block is found, try to parse the whole string.
                    # This handles cases where the model correctly returns *only* JSON.
                    parsed_plan = json.loads(tactical_plan_response_str)
                    ai_plan_json_str = tactical_plan_response_str

                print(f"--- Tactical Execution Plan ---\n{json.dumps(parsed_plan, indent=2)}\n--------------------------")

            except json.JSONDecodeError as e:
                print(f"ERROR: Could not parse Tactical Plan. Error: {e}. Response: {tactical_plan_response_str}")
                return jsonify({'error': 'Failed to create a detailed execution plan.'}), 500
            # ===================================================================
            # END: CORRECTED TACTICAL PLAN PARSING LOGIC
            # ===================================================================

        else: # Standard, single-agent flow
            log_progress("Creating a plan to answer the user's question...")
            schema_description = get_data_schema_description(last_analysis)
            planning_system_prompt = get_planning_system_prompt()
            planning_messages = [
                {"role": "system", "content": planning_system_prompt},
                {"role": "user", "content": f"User Question: \"{user_question}\"\n\nData Schema Description:\n{schema_description}\n\nFull 'last_analysis' context:\n{json.dumps(last_analysis, indent=2, default=str)[:2000]}"}
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
                 print(f"ERROR: Could not parse standard plan. Error: {e}. Response: {full_response_str}")
                 return jsonify({'error': 'Failed to create a standard execution plan.'}), 500

        # =================================================================
        # STAGE 3: EXECUTION (Common to both modes)
        # =================================================================
        log_progress("Executing plan: Fetching news and internal data...")
        news_summary = None
        news_plan = parsed_plan.get("fetch_external_news", {})
        if news_plan.get("needed"):
            sonar_prompt = news_plan.get("prompt_for_sonar", f"Get the latest news for {last_analysis.get('ticker')}")
            try:
                news_messages = [{"role": "user", "content": sonar_prompt}]
                news_summary = call_perplexity_api(news_messages, model="sonar-pro")
            except Exception as e:
                print(f"ERROR: News fetching failed: {e}")
                news_summary = f"Error: Failed to fetch real-time news. {e}"

        retrieve_data_spec = parsed_plan.get("retrieve_data", {})
        retrieved_fundamental_data = retrieve_data_based_on_plan(retrieve_data_spec, last_analysis)

        log_progress("Performing financial calculations...")
        calculations_spec = parsed_plan.get("perform_calculations", [])
        calculation_results_obj = perform_planned_calculations(calculations_spec, retrieved_fundamental_data, last_analysis)


        # =================================================================
        # STAGE 4: SYNTHESIS (Common to both modes, guided by brain_plan if present)
        # =================================================================
        log_progress("Synthesizing the final response...")
        final_context_for_answer = {
            "user_question": user_question,
            "central_brain_plan": central_brain_plan,
            "retrieved_data": retrieved_fundamental_data,
            "calculated_metrics": calculation_results_obj.get("results", {}),
            "documents": last_analysis.get('documents', []),
            "news_summary": news_summary
        }

        answering_system_prompt = get_answering_system_prompt()
        
        answering_messages = [
            {"role": "system", "content": answering_system_prompt},
            {"role": "user", "content": f"Please synthesize an answer based on the following consolidated data:\n{json.dumps(final_context_for_answer, indent=2, default=str)}"}
        ]
        
        answerer_model = 'gpt-4.1-mini' if is_best_mode else selected_model
        final_answer = call_generative_ai_model(
            model=answerer_model,
            messages=answering_messages,
            temperature=0.7
        )

        return jsonify({
            'answer': final_answer,
            'thought_process': thought_process_str,
            'planning_data': ai_plan_json_str,
            'raw_news_summary': news_summary
        })

    except Exception as e:
        print(f"ERROR: General Error in /chat: {e}")
        traceback.print_exc()
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
                        for i, row_dict in enumerate(table_data[:15]): # Show all metric names, but cap rows to avoid huge output if many metrics
                            if isinstance(row_dict, dict):
                                metric_name = row_dict.get("")
                                example_val = "N/A"
                                if sorted_period_headers: # Get value from first available period
                                    example_val = row_dict.get(sorted_period_headers[0], "N/A (for first period)")
                                output_lines.append(f"          - Metric: \"{metric_name}\" (Example from '{sorted_period_headers[0] if sorted_period_headers else 'N/A'}': {json.dumps(example_val)})")
                            else:
                                output_lines.append(f"          - Row {i} is not a dict: {json.dumps(row_dict)}")
                        if len(table_data) > 15:
                             output_lines.append("          - ... (more metrics exist)")
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
    fetch_latest_documents_async
)

from scanx_fetcher import scrape_scanx_company_async

# Initialize TradingView datafeed (guest)
tv = TvDatafeed()


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

    # --- Financials Context (Unchanged) ---
    if fundamentals:
        annual_results = fundamentals.get("Annual Results")
        if annual_results is not None and not annual_results.empty:
            context_parts.append("\n### Key Annual Financials (for overall trend analysis)\n")
            annual_df = annual_results.set_index(annual_results.columns[0])
            key_metrics = ["Sales", "Net Profit"]
            for metric in key_metrics:
                if metric in annual_df.index:
                    metric_data = annual_df.loc[metric].iloc[-3:]
                    context_parts.append(f"- {metric} (last 3 years): {', '.join(metric_data.astype(str).tolist())}")


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

    full_context = "\n".join(context_parts)
    
    print("\n" + "="*40)
    print("CONTEXT BEING SENT TO AI FOR SUMMARY GENERATION:")
    print(full_context)
    print("="*40 + "\n")
    
    if len(full_context) < 150:
        return "<p>A detailed summary could not be generated due to insufficient data.</p>"

    # --- MODIFIED: New, more detailed System Prompt ---
    # system_prompt = """You are an expert financial analyst AI. Generate a concise, data-driven summary of the company for a retail investor using simple HTML (<h4>, <p>, <ul>, <li>) emphasizing with <b>, <i> wherever relevant, without <html>/<body> tags. Start with a section called "What the company does", followed by "How it generates revenue", followed by "Business segments" that details of the company's split by business segments, geography, product line, and/or customer/channel, followed finally by "Latest Developments" that highlight any new projects/initiatives company has taken, for e.g. capital expansion, acquisition, new market entry, product launch, latest order book, forward looking guidance, regulatory changes, potential threats to business, or major weakness/loss to business. Keep the content crisp and easy to read such that even a new investor can understand. It should read like a dialog between two people."""
    system_prompt = """You are an expert financial analyst. Your task is to explain complex company information in simple, direct language for a retail investor. You will generate a concise, data-driven summary by interpreting the provided documents.

You must follow two sets of instructions exactly: the **Analysis Instructions** for content and structure, and the **Writing Guidelines** for style and tone.

---

### **Analysis Instructions**

1.  **Structure your response using simple HTML.** Use `<h4>` for headers and `<p>`, `<ul>`, and `<li>` for the body. Do not include `<html>` or `<body>` tags. Your output must be a single block of well-formed HTML.
2.  **Analyze management commentary objectively.** Do not accept management's statements at face value.
3.  **Identify and highlight bias.** Point out when management presents overly optimistic or vague information. Show discrepancies between what management claims and what the financial data shows.
4.  **Use the following output structure:**

    <h4>What the Company Does</h4>
    <p>Write one short paragraph describing the company's main business.</p>

    <h4>How it Generates Revenue</h4>
    <p>Start with a single sentence about how the company makes money. Then, describe the company's overall sales trend using the '### Key Annual Financials (for overall trend analysis)'.</p>
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
    <p>In a bulleted list, point out the management biases you found. Note where their commentary seems too positive, is vague, or conflicts with financial data or industry facts. Highlight the management's tone, intent and conviction in the concall as well. </p>

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
        
        summary_html = call_openai_api(messages, model="gpt-4.1-mini", temperature=1)
        return summary_html

    except Exception as e:
        print(f"CRITICAL ERROR: Failed to generate AI company summary for {ticker}.")
        # ... (error logging is unchanged) ...
        return "<p><strong>Error:</strong> The AI-powered summary could not be generated at this time.</p>"


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


# def generate_ai_scores(ticker, last_analysis):
#     """
#     MODIFIED: Now generates a downloadable Excel debug file for the Valuation Score.
#     It now returns a tuple: (score_html_list, debug_filename_or_none)
#     """
#     debug_filename = None # Initialize filename as None
#     try:
#         ai_scores = AIScores(ticker, last_analysis)
#         ai_scores.calculate_all_scores()

#         # --- START: NEW DEBUG FILE GENERATION LOGIC ---
#         try:
#             val_score_obj = ai_scores.scores.get('valuation')
#             if val_score_obj and not val_score_obj.df.empty:
#                 # Define the columns we want to inspect
#                 features = val_score_obj.features
#                 proxy_col = val_score_obj.proxy_score_col
#                 regime_col = val_score_obj.regime_labels_col
                
#                 # Ensure all required columns exist before proceeding
#                 required_cols = features + [proxy_col, regime_col]
#                 if all(col in val_score_obj.df.columns for col in required_cols):
#                     # Select only the data we need
#                     debug_df = val_score_obj.df[required_cols]

#                     # Generate a unique, temporary filename
#                     unique_id = uuid.uuid4()
#                     debug_filename = f"debug_valuation_{ticker}_{unique_id}.xlsx"
                    
#                     # --- START: MODIFIED FILE SAVING LOGIC ---
                    
#                     # 1. Define the dedicated debug directory
#                     debug_dir = "debug_files"
                    
#                     # 2. Create the directory if it doesn't already exist
#                     os.makedirs(debug_dir, exist_ok=True)
                    
#                     # 3. Create the full, explicit path to the file
#                     full_file_path = os.path.join(debug_dir, debug_filename)
                    
#                     # 4. Save the DataFrame to the specific path
#                     debug_df.to_excel(full_file_path)
                    
#                     print(f"INFO: Successfully created debug file at: {full_file_path}")

#         except Exception as e:
#             print(f"ERROR: Could not create debug file. Reason: {e}")
#         # --- END: NEW DEBUG FILE GENERATION LOGIC ---

#         # The rest of the function continues as before...
#         final_scaled_scores = {}
#         for field in ['valuation', 'technical']:
#             # ... (rest of the logic is unchanged)
#             score_obj = ai_scores.scores[field]
#             proxy_score_history = score_obj.df[score_obj.proxy_score_col]
#             latest_proxy_score = proxy_score_history.iloc[-1]
#             hist_min = proxy_score_history.min()
#             hist_max = proxy_score_history.max()
#             hist_range = hist_max - hist_min if (hist_max - hist_min) != 0 else 1
#             scaled_proxy_score_0_1 = (latest_proxy_score - hist_min) / hist_range
#             latest_regime = score_obj.df[score_obj.regime_labels_col].iloc[-1]
#             final_scaled_scores[field.capitalize()] = (scaled_proxy_score_0_1, latest_regime.replace('_', ' ').title())

#         data_dict_for_relative_scaling = {}
#         for field, score_obj in ai_scores.scores.items():
#             if field in ['liquidity', 'volatility']:
#                 raw_score_value = score_obj.get_score().tail(30).mean()
#                 regime_label = score_obj.df[f'{field}_regime_labels'].iloc[-1]
#                 data_dict_for_relative_scaling[field.capitalize()] = (raw_score_value, regime_label.replace('_', ' ').title())

#         if data_dict_for_relative_scaling:
#             relatively_scaled = AIScores._scale_radar_scores(data_dict_for_relative_scaling)
#             final_scaled_scores.update(relatively_scaled)
            
#         ai_score_html = []
#         for field_key in ['Valuation', 'Technical', 'Liquidity', 'Volatility']:
#             if field_key in final_scaled_scores:
#                 score_obj = ai_scores.scores[field_key.lower()]
#                 scaled_value, description = final_scaled_scores[field_key]
                
#                 json_dict = { 'label': field_key, 'value': round(scaled_value * 10, 1), 'description': description, 'chart1_json': score_obj.plot().to_json(), 'chart2_json': score_obj.plot_violin().to_json() }
#                 ai_score_html.append(json_dict)
                
#         return ai_score_html, debug_filename # Return both the HTML and the filename
        
#     except Exception as e:
#         print(f"CRITICAL ERROR in generate_ai_scores for {ticker}.")
#         print(f"Error: {e}")
#         traceback.print_exc()
#         return [], None # Return empty list and None on failure
    

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

async def get_analysis_for_ticker_async(tick):
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

    # Define all tasks that can run without dependencies on each other
    tasks = {
        "yfinance_name": asyncio.to_thread(get_yfinance_data, tick),
        "tech_data": asyncio.to_thread(evaluate_ticker_signal, tick),
        "screener_tables": fetch_consolidated_async(tick),
        "documents": fetch_latest_documents_async(tick),
    }
    
    # Run them all in parallel and wait for all to complete
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    results_dict = dict(zip(tasks.keys(), results))

    # --- Check for critical failures from Stage 1 ---
    for task_name, result in results_dict.items():
        if isinstance(result, Exception):
            log_progress(f"Critical error during initial data fetch: {task_name} failed.")
            return ({'error': f'Failed to fetch critical data: {task_name}. Reason: {result}'}, 500)

    company_name = results_dict["yfinance_name"]
    res = results_dict["tech_data"]
    tables_from_screener, company_description = results_dict["screener_tables"]
    latest_documents = results_dict["documents"]

    # --- Stage 2: Gather dependent I/O tasks ---
    
    async def fetch_dependent_data():
        try:
            # get_company_id runs now since it's fast and needed for valuation
            comp_id = await get_company_id_async(tick)

            # ScanX scrape depends on company_name
            slug = re.sub(r'[^a-z0-9\s-]', '', re.sub(r'\s+', '-', re.sub(r'\blimited\b', 'ltd', company_name.lower()))).strip('-')
            scanx_task = scrape_scanx_company_async(slug)

            # Valuation metrics depend on company_id
            async def fetch_all_valuation_data():
                metric_queries = {
                    "PE Ratio": "Price to Earning-Median PE-EPS", "PB Ratio": "Price to book value-Median PBV-Book value",
                    "EV / EBITDA": "EV Multiple-Median EV Multiple-EBITDA", "Market Cap / Sales": "Market Cap to Sales-Median Market Cap to Sales-Sales",
                    "Margins": "GPM-OPM-NPM-Quarter Sales"
                }
                parsed_data, charts_data = {}, {}
                async with httpx.AsyncClient() as client:
                    valuation_tasks = {label: fetch_chart_data_async(client, comp_id, query) for label, query in metric_queries.items()}
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

            valuation_task = fetch_all_valuation_data()
            
            # Run Stage 2 tasks in parallel
            stage2_results = await asyncio.gather(scanx_task, valuation_task, return_exceptions=True)
            return stage2_results
        except Exception as e:
            log_progress(f"Error in dependent data fetching stage: {e}")
            return e # Return exception to be handled

    stage2_results = await fetch_dependent_data()
    if isinstance(stage2_results, Exception):
        return ({'error': f'Failed during dependent data fetching: {stage2_results}'}, 500)
        
    scanx_data = stage2_results[0] if not isinstance(stage2_results[0], Exception) else {}
    parsed_valuation_data, metric_charts_for_frontend = stage2_results[1] if not isinstance(stage2_results[1], Exception) else ({}, {})

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
    def make_chart_json(func, *args):
        return func(*args).to_json()

    # Define all the tasks
    task_close = loop.run_in_executor(None, make_chart_json, build_close_figure, df, company_name)
    task_hl    = loop.run_in_executor(None, make_chart_json, build_hl_figure, df, company_name)
    task_ema   = loop.run_in_executor(None, make_chart_json, build_ema_figure, df, company_name)
    task_rsi   = loop.run_in_executor(None, make_chart_json, build_rsi_figure, df, company_name)
    task_adl   = loop.run_in_executor(None, make_chart_json, build_adl_figure, df, company_name)
    task_rs    = loop.run_in_executor(None, make_chart_json, build_rs_figure, df, company_name)
    
    # Run AI Summary in parallel (Network Bound)
    task_ai_sum = loop.run_in_executor(None, generate_ai_company_summary, tick, company_description, tables_from_screener, latest_documents)

    # EXECUTE ALL AT ONCE
    parallel_results = await asyncio.gather(task_close, task_hl, task_ema, task_rsi, task_adl, task_rs, task_ai_sum)

    # Unpack the results
    close_j, hl_j, ema_j, rsi_j, adl_j, rs_j, ai_company_summary_html = parallel_results
    
    log_progress("Charts and AI Summary generated successfully.")
# --- END: OPTIMIZED PARALLEL PROCESSING ---


    # close_j=build_close_figure(df,company_name).to_json()
    # hl_j   =build_hl_figure(df,company_name).to_json()
    # ema_j  =build_ema_figure(df,company_name).to_json()
    # rsi_j  =build_rsi_figure(df,company_name).to_json()
    # adl_j  =build_adl_figure(df,company_name).to_json()
    # rs_j   =build_rs_figure(df,company_name).to_json()
    
    # fund_data_for_frontend, fund_data_for_ai_context = {}, {}
    # if tables_from_screener:
    #     for table_name, df_original_table in tables_from_screener.items():
    #         fund_data_for_frontend[table_name] = df_original_table.reset_index().T.to_json(orient='split')
    #         temp_rows_for_cleaning = df_original_table.reset_index().to_dict(orient='records')
    #         cleaned_rows_for_ai = []
    #         for row_dict in temp_rows_for_cleaning:
    #             cleaned_row = { (str(k).strip() if isinstance(k, str) else k): (str(v).strip() if isinstance(v, str) else v) for k, v in row_dict.items() }
    #             cleaned_rows_for_ai.append(cleaned_row)
    #         fund_data_for_ai_context[table_name] = cleaned_rows_for_ai
    
    # log_progress(f"Generating AI company summary for {tick}...")
    # ai_company_summary_html = generate_ai_company_summary(
    #     ticker=tick,
    #     description=company_description,
    #     fundamentals=tables_from_screener,
    #     documents=latest_documents
    # )
    # log_progress("AI summary generated successfully.")
    
    analysis_result_for_cache = {
    "ticker": tick, "company_name": company_name, "summary": cleaned_technical_summary, 
    "fundamentals": fund_data_for_ai_context, "valuation_and_margin_data": parsed_valuation_data, 
    "documents": latest_documents,
    "technical_data_df": df
    }
    # Store the analysis data in the shared Redis cache
    # cache.set("last_analysis_data", analysis_result_for_cache)
    
    # last_analysis = {
    #    "ticker": tick, "company_name": company_name, "summary": cleaned_technical_summary, 
    #    "fundamentals": fund_data_for_ai_context, "valuation_and_margin_data": parsed_valuation_data, 
    #    "documents": latest_documents
    # }

    log_progress(f"Generating AI scores for {tick}...")
    ai_scores_data = generate_ai_scores(tick, analysis_result_for_cache)
    log_progress("AI scores generated successfully.")

    log_progress("Analysis complete. Loading results ...")
   
    # return {
    #     'ticker': tick, 'company_name': company_name, 'company_summary_html': ai_company_summary_html,
    #     'summary': cleaned_technical_summary, 'ai_scores': ai_scores_data,
    #     'chart_close_json': close_j, 'chart_hl_json': hl_j, 'chart_ema_json': ema_j,
    #     'chart_rsi_json': rsi_j, 'chart_adl_json': adl_j, 'chart_rs_json': rs_j,
    #     'fundamentals': fund_data_for_frontend, 'metric_charts': metric_charts_for_frontend,
    #     'documents': latest_documents, 'scanx_data': scanx_data
    # }

    # This dictionary is what the AI needs. It uses the Python objects.
    analysis_for_cache = {
        "ticker": tick, "company_name": company_name, "summary": cleaned_technical_summary, 
        "fundamentals": fund_data_for_ai_context,  # <-- The AI-friendly version
        "valuation_and_margin_data": parsed_valuation_data, 
        "documents": latest_documents,
        "technical_data_df": df
    }
    
    # This dictionary is what the frontend needs. It uses the JSON strings.
    result_for_frontend = {
        'ticker': tick, 'company_name': company_name, 'company_summary_html': ai_company_summary_html,
        'summary': cleaned_technical_summary, 'ai_scores': ai_scores_data,
        'chart_close_json': close_j, 'chart_hl_json': hl_j, 'chart_ema_json': ema_j,
        'chart_rsi_json': rsi_j, 'chart_adl_json': adl_j, 'chart_rs_json': rs_j,
        'fundamentals': fund_data_for_frontend, # <-- The frontend-friendly version
        'metric_charts': metric_charts_for_frontend,
        'documents': latest_documents, 'scanx_data': scanx_data
    }

    # Pass BOTH dictionaries back to the synchronous wrapper
    return result_for_frontend, analysis_for_cache

# @cache.memoize(timeout=21600)
def get_analysis_for_ticker(tick):
    """
    Synchronous wrapper that runs the async logic. The cache stores the final result.
    This is the bridge between the synchronous Flask world and our async code.
    """
    return asyncio.run(get_analysis_for_ticker_async(tick))

@app.route('/analyze', methods=['POST'])
def analyze():
    """
    Lightweight Flask route that calls the cached, synchronous wrapper.
    It remains unchanged from the previous caching step.
    """
    try:
        data = request.get_json(force=True)
        tick = data.get('ticker','').strip().upper()
        if not tick:
            return jsonify({'error': 'No ticker provided'}), 400
    
        # Unpack the two dictionaries returned by the function
        result_for_frontend, analysis_for_cache = get_analysis_for_ticker(tick)

        # --- START MODIFICATION ---
        # Generate the AI scores and get the debug filename
        ai_scores_data, debug_filename = generate_ai_scores(tick, analysis_for_cache)
        result_for_frontend['ai_scores'] = ai_scores_data

        # If a debug file was created, add its download URL to the response
        if debug_filename:
            result_for_frontend['debug_file_url'] = f"/download_debug_file/{debug_filename}"
        # --- END MODIFICATION ---
        
        # Generate a unique key for this analysis session
        analysis_key = str(uuid.uuid4())

        # --- ROBUST CACHING BLOCK ---
        try:
            import sys
            # Optional: Log the size to see how big the object is
            size_in_mb = sys.getsizeof(str(analysis_for_cache)) / (1024 * 1024)
            print(f"INFO: Attempting to cache analysis data. Est. Size: {size_in_mb:.2f} MB")

            # Try to save to cache, but don't let it kill the request if it fails
            cache.set(analysis_key, analysis_for_cache, timeout=21600)
            print(f"INFO: Successfully cached data for {tick}")
            
        except Exception as e:
            print(f"WARNING: Failed to write to Redis Cache. Returning data anyway. Error: {e}")
            # We continue execution so the user still sees the result, 
            # even if the "Deep Chat" feature might need a reload later.
        # ---------------------------
        
        # Add the key to the frontend data
        result_for_frontend['analysis_key'] = analysis_key
        
        return jsonify(result_for_frontend)


        # cache.set(analysis_key, analysis_for_cache, timeout=21600)
        
        # result_for_frontend['analysis_key'] = analysis_key
        
        # return jsonify(result_for_frontend)

        # --- END OF NEW LOGIC ---

    except Exception as e:
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
        # --- MODIFIED FILE SERVING LOGIC ---
        # Explicitly serve from the dedicated 'debug_files' directory
        return send_from_directory("debug_files", filename, as_attachment=True)
        # --- END MODIFICATION ---
    except FileNotFoundError:
        return "File not found.", 404
    

# =====================================================================
# END: New ASYNC and Caching Implementation for Analysis
# =====================================================================


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=True)

# Wrap the WSGI app in ASGI middleware for Uvicorn
# app = ASGIMiddleware(app)