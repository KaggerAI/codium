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

        # User events table for insights
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event_type TEXT NOT NULL,
                event_data TEXT,
                session_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')

        # Portfolio holdings table for personalization
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS portfolio_holdings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                quantity REAL NOT NULL,
                avg_buy_price REAL NOT NULL,
                sector TEXT DEFAULT '',
                industry TEXT DEFAULT '',
                buy_date TEXT DEFAULT '',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id),
                UNIQUE(user_id, ticker)
            )
        ''')

        # Watchlist items table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS watchlist_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                stock_name TEXT NOT NULL DEFAULT '',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id),
                UNIQUE(user_id, ticker)
            )
        ''')

        # Migration: add buy_date column if not exists (for existing DBs)
        try:
            cursor.execute("ALTER TABLE portfolio_holdings ADD COLUMN buy_date TEXT DEFAULT ''")
        except Exception:
            pass  # Column already exists
        
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


# User Event model for Insights
class UserEvent:
    def __init__(self, id, user_id, event_type, event_data, session_id, created_at):
        self.id = id
        self.user_id = user_id
        self.event_type = event_type
        self.event_data = event_data
        self.session_id = session_id
        self.created_at = created_at

    @classmethod
    def log(cls, user_id, event_type, event_data=None, session_id=None):
        """Log a new user event."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO user_events (user_id, event_type, event_data, session_id)
                VALUES (?, ?, ?, ?)
            ''', (user_id, event_type, event_data, session_id))
            return cursor.lastrowid

    @classmethod
    def get_summary(cls, include_admins=True):
        """Get aggregated analytics for the admin dashboard."""
        with get_db() as conn:
            cursor = conn.cursor()
            
            # Base clauses for filtering
            join_clause = "JOIN users u ON e.user_id = u.id" if not include_admins else ""
            where_condition = "AND u.role != 'admin'" if not include_admins else ""

            # Daily Active Users (Last 7 days)
            cursor.execute(f'''
                SELECT date(e.created_at) as date, count(DISTINCT e.user_id) as dau
                FROM user_events e
                {join_clause}
                WHERE e.created_at > date('now', '-7 days') {where_condition}
                GROUP BY date(e.created_at)
                ORDER BY date DESC
            ''')
            daily_active = [dict(row) for row in cursor.fetchall()]

            # Top Sections (Last 30 days)
            cursor.execute(f'''
                SELECT e.event_data as section, count(*) as count
                FROM user_events e
                {join_clause}
                WHERE e.event_type = 'section_view' AND e.created_at > date('now', '-30 days') {where_condition}
                GROUP BY e.event_data
                ORDER BY count DESC
                LIMIT 10
            ''')
            top_sections = [dict(row) for row in cursor.fetchall()]

            # Recent Activity
            cursor.execute(f'''
                SELECT u.email, e.event_type, e.event_data, e.created_at
                FROM user_events e
                JOIN users u ON e.user_id = u.id
                WHERE 1=1 {"AND u.role != 'admin'" if not include_admins else ""}
                ORDER BY e.created_at DESC
                LIMIT 20
            ''')
            recent_activity = [dict(row) for row in cursor.fetchall()]

            return {
                'daily_active': daily_active,
                'top_sections': top_sections,
                'recent_activity': recent_activity
            }


# Portfolio holding model
class Portfolio:
    def __init__(self, id, user_id, ticker, stock_name, quantity, avg_buy_price, sector, industry, buy_date=None, added_at=None, **kwargs):
        self.id = id
        self.user_id = user_id
        self.ticker = ticker
        self.stock_name = stock_name
        self.quantity = quantity
        self.avg_buy_price = avg_buy_price
        self.sector = sector or ''
        self.industry = industry or ''
        self.buy_date = buy_date or ''
        self.added_at = added_at

    def to_dict(self):
        return {
            'id': self.id,
            'ticker': self.ticker,
            'stock_name': self.stock_name,
            'quantity': self.quantity,
            'avg_buy_price': self.avg_buy_price,
            'sector': self.sector,
            'industry': self.industry,
            'buy_date': self.buy_date,
            'added_at': str(self.added_at)
        }

    @classmethod
    def get_by_user(cls, user_id):
        """Get all portfolio holdings for a user."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT * FROM portfolio_holdings WHERE user_id = ? ORDER BY added_at DESC',
                (user_id,)
            )
            rows = cursor.fetchall()
            results = []
            for row in rows:
                d = dict(row)
                results.append(cls(**d))
            return results

    @classmethod
    def add_holding(cls, user_id, ticker, stock_name, quantity, avg_buy_price, sector='', industry='', buy_date=''):
        """Add or update a holding (UPSERT)."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO portfolio_holdings (user_id, ticker, stock_name, quantity, avg_buy_price, sector, industry, buy_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, ticker) DO UPDATE SET
                    stock_name = excluded.stock_name,
                    quantity = excluded.quantity,
                    avg_buy_price = excluded.avg_buy_price,
                    sector = excluded.sector,
                    industry = excluded.industry,
                    buy_date = excluded.buy_date
            ''', (user_id, ticker.upper(), stock_name, quantity, avg_buy_price, sector, industry, buy_date or ''))
            return cursor.lastrowid

    @classmethod
    def update_holding(cls, user_id, ticker, quantity, avg_buy_price, buy_date=None):
        """Update quantity, avg price, and optionally buy_date for an existing holding."""
        with get_db() as conn:
            cursor = conn.cursor()
            if buy_date is not None:
                cursor.execute('''
                    UPDATE portfolio_holdings
                    SET quantity = ?, avg_buy_price = ?, buy_date = ?
                    WHERE user_id = ? AND ticker = ?
                ''', (quantity, avg_buy_price, buy_date, user_id, ticker.upper()))
            else:
                cursor.execute('''
                    UPDATE portfolio_holdings
                    SET quantity = ?, avg_buy_price = ?
                    WHERE user_id = ? AND ticker = ?
                ''', (quantity, avg_buy_price, user_id, ticker.upper()))
            return cursor.rowcount > 0

    @classmethod
    def delete_holding(cls, user_id, ticker):
        """Delete a single holding."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'DELETE FROM portfolio_holdings WHERE user_id = ? AND ticker = ?',
                (user_id, ticker.upper())
            )
            return cursor.rowcount > 0

    @classmethod
    def delete_all(cls, user_id):
        """Clear all holdings for a user."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'DELETE FROM portfolio_holdings WHERE user_id = ?',
                (user_id,)
            )
            return cursor.rowcount

    @classmethod
    def get_all_user_ids(cls):
        """Get all unique user IDs that have portfolio holdings."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT DISTINCT user_id FROM portfolio_holdings')
            return [row[0] for row in cursor.fetchall()]


class Watchlist:
    """Model class for user watchlist items."""
    def __init__(self, id, user_id, ticker, stock_name='', added_at=None, **kwargs):
        self.id = id
        self.user_id = user_id
        self.ticker = ticker
        self.stock_name = stock_name or ''
        self.added_at = added_at

    def to_dict(self):
        return {
            'id': self.id,
            'ticker': self.ticker,
            'stock_name': self.stock_name,
            'added_at': str(self.added_at) if self.added_at else ''
        }

    @classmethod
    def get_by_user(cls, user_id):
        """Get all watchlist items for a user."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT * FROM watchlist_items WHERE user_id = ? ORDER BY added_at DESC',
                (user_id,)
            )
            rows = cursor.fetchall()
            return [cls(**dict(row)) for row in rows]

    @classmethod
    def add_item(cls, user_id, ticker, stock_name=''):
        """Add a ticker to the user's watchlist. Returns True if added, False if already exists."""
        with get_db() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute('''
                    INSERT INTO watchlist_items (user_id, ticker, stock_name)
                    VALUES (?, ?, ?)
                ''', (user_id, ticker.upper(), stock_name))
                return True
            except Exception:
                # UNIQUE constraint violation - already in watchlist
                return False

    @classmethod
    def remove_item(cls, user_id, ticker):
        """Remove a ticker from the user's watchlist. Returns True if deleted."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'DELETE FROM watchlist_items WHERE user_id = ? AND ticker = ?',
                (user_id, ticker.upper())
            )
            return cursor.rowcount > 0

    @classmethod
    def has_item(cls, user_id, ticker):
        """Check if a ticker is in the user's watchlist."""
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT 1 FROM watchlist_items WHERE user_id = ? AND ticker = ?',
                (user_id, ticker.upper())
            )
            return cursor.fetchone() is not None


# Initialize database on module import
if __name__ == '__main__':
    init_db()
    seed_admin()
