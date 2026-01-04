"""
Authentication utilities for Kagger AI.
Handles password hashing, token generation, and session management.
"""

import os
import secrets
import hashlib
from datetime import datetime, timedelta
from functools import wraps

# Use bcrypt if available, fallback to sha256 (less secure but works without C dependencies)
try:
    import bcrypt
    USE_BCRYPT = True
except ImportError:
    USE_BCRYPT = False
    print("WARNING: bcrypt not installed, using SHA256 for password hashing (less secure)")


def hash_password(password: str) -> str:
    """Hash a password securely."""
    if USE_BCRYPT:
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    else:
        # Fallback to salted SHA256
        salt = secrets.token_hex(16)
        hashed = hashlib.sha256((salt + password).encode()).hexdigest()
        return f"{salt}${hashed}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against its hash."""
    if not password_hash:
        return False
    
    if USE_BCRYPT:
        try:
            return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
        except Exception:
            return False
    else:
        # Fallback SHA256 verification
        try:
            salt, stored_hash = password_hash.split('$')
            test_hash = hashlib.sha256((salt + password).encode()).hexdigest()
            return test_hash == stored_hash
        except Exception:
            return False


def generate_invite_token() -> str:
    """Generate a secure random invite token."""
    return secrets.token_urlsafe(32)


def get_token_expiry(hours: int = 24) -> datetime:
    """Get expiry datetime for a token."""
    return datetime.utcnow() + timedelta(hours=hours)


def generate_session_token() -> str:
    """Generate a session token."""
    return secrets.token_urlsafe(32)


# Flask-Login compatibility
class AnonymousUser:
    """Anonymous user class for Flask-Login compatibility."""
    
    @property
    def is_authenticated(self):
        return False
    
    @property
    def is_active(self):
        return False
    
    @property
    def is_anonymous(self):
        return True
    
    def get_id(self):
        return None
