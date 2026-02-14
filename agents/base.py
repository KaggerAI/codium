"""
base.py — Shared utilities for the Kagger AI Agent Marketplace.

Provides common patterns for agent registration, job management, and responses.
"""

import uuid
import time
import threading
import sys

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
    return job_id


def update_agent_job(job_id, updates):
    """Update fields of an existing agent job."""
    with AGENT_JOBS_LOCK:
        if job_id in AGENT_JOBS:
            AGENT_JOBS[job_id].update(updates)


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
    """Store the latest result for an agent+ticker combination."""
    key = f"{agent_type}_{ticker.upper()}"
    with AGENT_LATEST_LOCK:
        AGENT_LATEST_RESULTS[key] = {
            'result': result_data,
            'stored_at': time.time(),
            'agent_type': agent_type,
            'ticker': ticker.upper()
        }


def get_latest_result(agent_type, ticker):
    """Get the latest stored result for an agent+ticker."""
    key = f"{agent_type}_{ticker.upper()}"
    with AGENT_LATEST_LOCK:
        return AGENT_LATEST_RESULTS.get(key)
