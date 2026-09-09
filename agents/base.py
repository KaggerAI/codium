"""
base.py — Shared utilities for the Kagger AI Agent Marketplace.

Provides common patterns for agent registration, job management, and responses.
"""

import uuid
import os
import time
import threading
import sys
import json
import zlib
import datetime

# =====================================================================
# COSMIC AGENT MODEL CONFIGURATION (shared by the macro and micro agents)
# =====================================================================
# Both cosmic agents read the same two settings, defined here rather than in either agent so they
# cannot drift apart. Before this existed the micro agent hardcoded its model at two call sites and
# silently ignored the macro agent's override.
#
# WHY gpt-5.4 IS THE DEFAULT, NOT gpt-5.5:
# The OpenAI complimentary-daily-token programme (Data controls -> Sharing) covers gpt-5.4 at up to
# 1M tokens/day. gpt-5.5 is NOT on that list, so every gpt-5.5 call was billed at standard rates -
# which is what produced a `credit_balance_exhausted` 429 on an account that had never knowingly
# spent anything. Keeping the free-tier-eligible model as the DEFAULT rather than as an env override
# means the agents stay inside the allowance by design instead of by remembering a flag.
#
# COST NOTE ON xhigh: reasoning tokens count against that same 1M/day allowance, and xhigh on a
# large prompt is close to the worst case for them. It is the right setting for a verification
# baseline (docs/COSMIC_ENGINE_MIGRATION.md section 7.1) and for the post-cutover narration load.
# It is expensive for high-volume chat; override per-surface with the env vars below if that bites.
COSMIC_MODEL = os.getenv("COSMIC_SYNTHESIS_MODEL", "gpt-5.4").strip() or "gpt-5.4"
COSMIC_REASONING_EFFORT = os.getenv("COSMIC_REASONING_EFFORT", "xhigh").strip() or None


def cosmic_model_config():
    """(model, reasoning_effort) for any cosmic agent call. effort may be None."""
    return COSMIC_MODEL, COSMIC_REASONING_EFFORT


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

# When this worker process came up. A job whose Redis snapshot was last written
# BEFORE this timestamp was owned by a process that no longer exists, so the
# background thread driving it died with that process and its status can never
# change again. Used by get_agent_job() to fail such a job instead of handing
# the browser a 'processing' status it would poll forever.
#
# Assumes one gunicorn worker (which is what the Dockerfile and startup.sh both
# pin). Under multiple workers a job owned by a live sibling would still be safe
# -- that sibling keeps stamping updated_at -- but a worker that restarts while a
# sibling's job is mid-flight could briefly misread it as orphaned.
PROCESS_START_TIME = time.time()


def create_agent_job(agent_type, ticker, metadata=None):
    """Create a new background agent job and return the job_id."""
    job_id = str(uuid.uuid4())
    now = time.time()
    job_data = {
        'job_id': job_id,
        'agent_type': agent_type,
        'ticker': ticker.upper(),
        'status': 'processing',
        'progress': 'Starting analysis...',
        'result': None,
        'error': None,
        'started_at': now,
        'updated_at': now,
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
    # Stamped on every write so a job orphaned by a worker restart can be told
    # apart from one that is simply slow. See PROCESS_START_TIME.
    updates = dict(updates)
    updates['updated_at'] = time.time()
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
                # Reaching Redis at all means this process has no local copy, so it
                # is not the process that started the job. If the snapshot predates
                # this process, the worker that owned it was recycled mid-run and
                # its thread is gone -- the job is orphaned, not in progress.
                if (job_data.get('status') == 'processing'
                        and job_data.get('updated_at', job_data['started_at']) < PROCESS_START_TIME):
                    print(f"WARN: Agent job {job_id} orphaned by a worker restart "
                          f"(last update predates this process)", file=sys.stderr)
                    job_data['status'] = 'error'
                    job_data['error'] = ('The server restarted while this analysis was '
                                         'running. Please run it again.')
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
