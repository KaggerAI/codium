"""
base.py — Shared utilities for the Kagger AI Agent Marketplace.

Provides common patterns for agent registration, job management, and responses.
"""

import uuid
import time
import threading
import sys
import json
import zlib
import datetime

# =====================================================================
# REDIS PERSISTENCE
# =====================================================================
AGENT_REDIS_CLIENT = None

def set_agent_redis_client(client):
    global AGENT_REDIS_CLIENT
    AGENT_REDIS_CLIENT = client

def get_seconds_to_next_quarter_boundary():
    """Calculates seconds until next Jan 1, Apr 1, Jul 1, Oct 1"""
    now = datetime.datetime.now()
    mod_month = now.month
    
    if mod_month >= 10:
        next_boundary = datetime.datetime(now.year + 1, 1, 1)
    elif mod_month >= 7:
        next_boundary = datetime.datetime(now.year, 10, 1)
    elif mod_month >= 4:
        next_boundary = datetime.datetime(now.year, 7, 1)
    else:
        next_boundary = datetime.datetime(now.year, 4, 1)
        
    return int((next_boundary - now).total_seconds())

# =====================================================================
# IN-MEMORY JOB STORE (shared by all agents)
# =====================================================================
AGENT_JOBS = {}
AGENT_JOBS_LOCK = threading.Lock()
AGENT_JOB_TTL = 7200  # 2 hours


def create_agent_job(agent_type, ticker, metadata=None):
    """Create a new background agent job and return the job_id."""
    job_id = str(uuid.uuid4())
    job_data = {
        'job_id': job_id,
        'agent_type': agent_type,
        'ticker': ticker.upper(),
        'status': 'processing',
        'progress': 'Starting analysis...',
        'result': None,
        'error': None,
        'started_at': time.time(),
        'metadata': metadata or {}
    }
    with AGENT_JOBS_LOCK:
        AGENT_JOBS[job_id] = job_data
        
    if AGENT_REDIS_CLIENT:
        try:
            AGENT_REDIS_CLIENT.setex(f"agent_job_{job_id}", AGENT_JOB_TTL, json.dumps(job_data))
        except Exception as e:
            print(f"WARN: Failed to cache agent job in Redis: {e}", file=sys.stderr)
            
    return job_id


def update_agent_job(job_id, updates):
    """Update fields of an existing agent job."""
    job_data = None
    with AGENT_JOBS_LOCK:
        if job_id in AGENT_JOBS:
            AGENT_JOBS[job_id].update(updates)
            job_data = dict(AGENT_JOBS[job_id])
            
    if AGENT_REDIS_CLIENT:
        try:
            if not job_data:
                raw = AGENT_REDIS_CLIENT.get(f"agent_job_{job_id}")
                if raw:
                    job_data = json.loads(raw)
                    job_data.update(updates)
            if job_data:
                AGENT_REDIS_CLIENT.setex(f"agent_job_{job_id}", AGENT_JOB_TTL, json.dumps(job_data))
        except Exception as e:
            print(f"WARN: Failed to update agent job in Redis: {e}", file=sys.stderr)


def get_agent_job(job_id):
    """Retrieve an agent job by ID."""
    with AGENT_JOBS_LOCK:
        job = AGENT_JOBS.get(job_id)
        if job:
            # Check TTL
            if time.time() - job['started_at'] > AGENT_JOB_TTL:
                del AGENT_JOBS[job_id]
                return None
            return dict(job)  # return a copy
            
    if AGENT_REDIS_CLIENT:
        try:
            raw = AGENT_REDIS_CLIENT.get(f"agent_job_{job_id}")
            if raw:
                job_data = json.loads(raw)
                if time.time() - job_data['started_at'] > AGENT_JOB_TTL:
                    AGENT_REDIS_CLIENT.delete(f"agent_job_{job_id}")
                    return None
                return job_data
        except Exception as e:
            print(f"WARN: Failed to retrieve agent job from Redis: {e}", file=sys.stderr)
            
    return None


def cleanup_old_jobs():
    """Remove expired jobs from memory."""
    now = time.time()
    with AGENT_JOBS_LOCK:
        expired = [jid for jid, j in AGENT_JOBS.items()
                    if now - j['started_at'] > AGENT_JOB_TTL]
        for jid in expired:
            del AGENT_JOBS[jid]
    if expired:
        print(f"INFO: Cleaned up {len(expired)} expired agent jobs", file=sys.stderr)


# =====================================================================
# LATEST RESULTS STORE (persists agent results for tab switching)
# =====================================================================
# Key: f"{agent_type}_{ticker}" → stores the latest completed result
AGENT_LATEST_RESULTS = {}
AGENT_LATEST_LOCK = threading.Lock()


def store_latest_result(agent_type, ticker, result_data):
    """Store the latest result for an agent+ticker combination in local cache and Redis."""
    key = f"{agent_type}_{ticker.upper()}"
    ttl = get_seconds_to_next_quarter_boundary()
    expires_at = time.time() + ttl
    
    with AGENT_LATEST_LOCK:
        AGENT_LATEST_RESULTS[key] = {
            'result': result_data,
            'stored_at': time.time(),
            'expires_at': expires_at,
            'agent_type': agent_type,
            'ticker': ticker.upper()
        }
        
    if AGENT_REDIS_CLIENT:
        try:
            data_bytes = zlib.compress(json.dumps(result_data).encode('utf-8'))
            AGENT_REDIS_CLIENT.setex(f"agent_result_{key}", ttl, data_bytes)
        except Exception as e:
            print(f"WARN: Failed to cache agent result in Redis for {key}: {e}", file=sys.stderr)


def get_latest_result(agent_type, ticker):
    """Get the latest stored result for an agent+ticker, checking Redis if missing in memory."""
    key = f"{agent_type}_{ticker.upper()}"
    redis_key = f"agent_result_{key}"
    
    with AGENT_LATEST_LOCK:
        mem_val = AGENT_LATEST_RESULTS.get(key)
        if mem_val:
            if time.time() > mem_val.get('expires_at', 0):
                del AGENT_LATEST_RESULTS[key]
            else:
                return mem_val

    if AGENT_REDIS_CLIENT:
        try:
            raw = AGENT_REDIS_CLIENT.get(redis_key)
            if raw:
                result_data = json.loads(zlib.decompress(raw).decode('utf-8'))
                with AGENT_LATEST_LOCK:
                    AGENT_LATEST_RESULTS[key] = {
                        'result': result_data,
                        'stored_at': time.time(),
                        'expires_at': time.time() + get_seconds_to_next_quarter_boundary(),
                        'agent_type': agent_type,
                        'ticker': ticker.upper()
                    }
                return AGENT_LATEST_RESULTS[key]
        except Exception as e:
            print(f"WARN: Failed to retrieve agent result from Redis for {key}: {e}", file=sys.stderr)
            
    return None
