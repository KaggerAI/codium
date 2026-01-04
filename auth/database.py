"""
Database setup and models for Kagger AI authentication.
Uses SQLite for simplicity (perfect for 100-200 users).
"""

import os
import sqlite3
from datetime import datetime
from contextlib import contextmanager

# Database file location - use persistent storage on Azure
# Azure App Service: /home is persistent across deployments
# Local: use project directory
if os.path.exists('/home'):
    # Azure Linux App Service - /home is persistent
    DB_PATH = '/home/kagger.db'
else:
    # Local development
    DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'kagger.db')


def get_db_connection():
    """Get a database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = get_db_connection()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def init_db():
    """Initialize the database with tables."""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Users table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT,
                role TEXT DEFAULT 'user',
                is_active BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            )
        ''')
        
        # Invite tokens table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invite_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                used BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        print(f"INFO: Database initialized at {DB_PATH}")


def seed_admin():
    """Create admin user if not exists."""
    admin_email = os.getenv('ADMIN_EMAIL', 'nikhil.banthiya@gmail.com')
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Check if admin exists
        cursor.execute('SELECT id FROM users WHERE email = ?', (admin_email,))
        if cursor.fetchone() is None:
            # Create admin user (password will be set on first login)
            cursor.execute('''
                INSERT INTO users (email, role, is_active)
                VALUES (?, 'admin', 1)
            ''', (admin_email,))
            print(f"INFO: Admin user created: {admin_email}")
        else:
            print(f"INFO: Admin user already exists: {admin_email}")


# User model class
class User:
    def __init__(self, id, email, password_hash, role, is_active, created_at, last_login):
        self.id = id
        self.email = email
        self.password_hash = password_hash
        self.role = role
        self.is_active = bool(is_active)
        self.created_at = created_at
        self.last_login = last_login
    
    @property
    def is_admin(self):
        return self.role == 'admin'
    
    @property
    def is_authenticated(self):
        return True
    
    @property
    def is_anonymous(self):
        return False
    
    def get_id(self):
        return str(self.id)
    
    @classmethod
    def get_by_id(cls, user_id):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))
            row = cursor.fetchone()
            if row:
                return cls(*row)
        return None
    
    @classmethod
    def get_by_email(cls, email):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users WHERE email = ?', (email.lower(),))
            row = cursor.fetchone()
            if row:
                return cls(*row)
        return None
    
    @classmethod
    def get_all(cls):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM users ORDER BY created_at DESC')
            rows = cursor.fetchall()
            return [cls(*row) for row in rows]
    
    @classmethod
    def create(cls, email, role='user', is_active=False):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO users (email, role, is_active)
                VALUES (?, ?, ?)
            ''', (email.lower(), role, is_active))
            return cursor.lastrowid
    
    def set_password(self, password_hash):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users SET password_hash = ?, is_active = 1
                WHERE id = ?
            ''', (password_hash, self.id))
    
    def update_last_login(self):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users SET last_login = ?
                WHERE id = ?
            ''', (datetime.utcnow(), self.id))
    
    @classmethod
    def delete(cls, user_id):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM users WHERE id = ?', (user_id,))


# Invite token model
class InviteToken:
    def __init__(self, id, email, token, expires_at, used, created_at):
        self.id = id
        self.email = email
        self.token = token
        self.expires_at = expires_at
        self.used = bool(used)
        self.created_at = created_at
    
    @property
    def is_valid(self):
        if self.used:
            return False
        # Parse expires_at if it's a string
        if isinstance(self.expires_at, str):
            exp = datetime.fromisoformat(self.expires_at)
        else:
            exp = self.expires_at
        return datetime.utcnow() < exp
    
    @classmethod
    def get_by_token(cls, token):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM invite_tokens WHERE token = ?', (token,))
            row = cursor.fetchone()
            if row:
                return cls(*row)
        return None
    
    @classmethod
    def create(cls, email, token, expires_at):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO invite_tokens (email, token, expires_at)
                VALUES (?, ?, ?)
            ''', (email.lower(), token, expires_at))
            return cursor.lastrowid
    
    def mark_used(self):
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE invite_tokens SET used = 1
                WHERE id = ?
            ''', (self.id,))


# Initialize database on module import
if __name__ == '__main__':
    init_db()
    seed_admin()
