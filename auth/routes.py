"""
Authentication routes for Kagger AI.
Handles login, logout, password setting, and admin user management.
"""

import os
from flask import Blueprint, request, jsonify, redirect, url_for, session, render_template_string
from functools import wraps

from .database import User, InviteToken, init_db, seed_admin
from .utils import hash_password, verify_password, generate_invite_token, get_token_expiry
from .email_service import send_invite_email, send_welcome_email

# Create blueprint
auth_bp = Blueprint('auth', __name__)

# Session configuration
SESSION_COOKIE_NAME = 'kagger_session'
SESSION_EXPIRY_DAYS = 7


def login_required(f):
    """Decorator to protect routes that require authentication."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_id = session.get('user_id')
        if not user_id:
            # For API requests, return JSON error
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Authentication required'}), 401
            # For page requests, redirect to login
            return redirect('/login')
        
        # Load user
        user = User.get_by_id(user_id)
        if not user or not user.is_active:
            session.clear()
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Invalid session'}), 401
            return redirect('/login')
        
        # Add user to request context
        request.current_user = user
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    """Decorator to protect admin-only routes."""
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not request.current_user.is_admin:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Admin access required'}), 403
            return redirect('/app')
        return f(*args, **kwargs)
    return decorated_function


# ============== API Routes ==============

@auth_bp.route('/api/login', methods=['POST'])
def api_login():
    """Handle user login."""
    data = request.get_json()
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    
    if not email or not password:
        return jsonify({'error': 'Email and password required'}), 400
    
    user = User.get_by_email(email)
    if not user:
        return jsonify({'error': 'Invalid email or password'}), 401
    
    if not user.is_active:
        return jsonify({'error': 'Account not activated. Please check your invite email.'}), 401
    
    if not user.password_hash:
        return jsonify({'error': 'Password not set. Please use the invite link to set your password.'}), 401
    
    if not verify_password(password, user.password_hash):
        return jsonify({'error': 'Invalid email or password'}), 401
    
    # Set session
    session.permanent = True
    session['user_id'] = user.id
    session['user_email'] = user.email
    session['is_admin'] = user.is_admin
    
    # Update last login
    user.update_last_login()
    
    return jsonify({
        'success': True,
        'user': {
            'email': user.email,
            'is_admin': user.is_admin
        }
    })


@auth_bp.route('/api/logout', methods=['POST'])
def api_logout():
    """Handle user logout."""
    session.clear()
    return jsonify({'success': True})


@auth_bp.route('/api/me')
@login_required
def api_me():
    """Get current user info."""
    user = request.current_user
    return jsonify({
        'email': user.email,
        'is_admin': user.is_admin,
        'created_at': str(user.created_at)
    })


@auth_bp.route('/api/set-password', methods=['POST'])
def api_set_password():
    """Set password using invite token."""
    data = request.get_json()
    token = data.get('token', '')
    password = data.get('password', '')
    
    if not token or not password:
        return jsonify({'error': 'Token and password required'}), 400
    
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400
    
    # Find token
    invite = InviteToken.get_by_token(token)
    if not invite:
        return jsonify({'error': 'Invalid invite token'}), 400
    
    if not invite.is_valid:
        return jsonify({'error': 'Invite token expired or already used'}), 400
    
    # Find or create user
    user = User.get_by_email(invite.email)
    if not user:
        # Create user if doesn't exist
        user_id = User.create(invite.email, is_active=True)
        user = User.get_by_id(user_id)
    
    # Set password
    password_hash = hash_password(password)
    user.set_password(password_hash)
    
    # Mark token as used
    invite.mark_used()
    
    # Send welcome email
    base_url = request.host_url.rstrip('/')
    send_welcome_email(user.email, f"{base_url}/login")
    
    return jsonify({'success': True, 'message': 'Password set successfully. You can now login.'})


# ============== Admin API Routes ==============

@auth_bp.route('/api/admin/users')
@admin_required
def api_list_users():
    """List all users (admin only)."""
    users = User.get_all()
    return jsonify({
        'users': [{
            'id': u.id,
            'email': u.email,
            'role': u.role,
            'is_active': u.is_active,
            'created_at': str(u.created_at),
            'last_login': str(u.last_login) if u.last_login else None
        } for u in users]
    })


@auth_bp.route('/api/admin/invite', methods=['POST'])
@admin_required
def api_invite_user():
    """Send invite to a new user (admin only)."""
    data = request.get_json()
    email = data.get('email', '').lower().strip()
    
    if not email or '@' not in email:
        return jsonify({'error': 'Valid email required'}), 400
    
    # Check if user already exists and is active
    existing = User.get_by_email(email)
    if existing and existing.is_active:
        return jsonify({'error': 'User already exists and is active'}), 400
    
    # Create user record if doesn't exist
    if not existing:
        User.create(email, is_active=False)
    
    # Generate invite token
    token = generate_invite_token()
    expires_at = get_token_expiry(24)  # 24 hours
    InviteToken.create(email, token, expires_at)
    
    # Build invite URL
    base_url = request.host_url.rstrip('/')
    invite_url = f"{base_url}/set-password/{token}"
    
    # Send invite email
    email_sent = send_invite_email(email, invite_url)
    
    return jsonify({
        'success': True,
        'email': email,
        'invite_url': invite_url,  # Also return URL in case email fails
        'email_sent': email_sent
    })


@auth_bp.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
@admin_required
def api_delete_user(user_id):
    """Delete a user (admin only)."""
    user = User.get_by_id(user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    # Prevent deleting yourself
    if user.id == request.current_user.id:
        return jsonify({'error': 'Cannot delete your own account'}), 400
    
    User.delete(user_id)
    return jsonify({'success': True})


# ============== Initialize ==============

def init_auth(app):
    """Initialize auth module with Flask app."""
    # Configure session
    app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    app.config['SESSION_COOKIE_SECURE'] = not app.debug  # HTTPS only in production
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 24 * SESSION_EXPIRY_DAYS
    
    # Initialize database
    init_db()
    seed_admin()
    
    # Register blueprint
    app.register_blueprint(auth_bp)
    
    print("INFO: Auth module initialized")
