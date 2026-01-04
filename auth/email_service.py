"""
Email service for Kagger AI using Resend.
Handles invite emails and welcome emails.
"""

import os
import resend

# Configure Resend
RESEND_API_KEY = os.getenv('RESEND_API_KEY')
if RESEND_API_KEY:
    resend.api_key = RESEND_API_KEY

# Email sender - use Resend's default domain for testing
FROM_EMAIL = "Kagger AI <hello@kagger.in>"


def send_invite_email(to_email: str, invite_url: str) -> bool:
    """
    Send an invite email to a new user.
    
    Args:
        to_email: The recipient's email address
        invite_url: The full URL to set password
        
    Returns:
        True if email sent successfully, False otherwise
    """
    if not RESEND_API_KEY:
        print(f"WARNING: RESEND_API_KEY not set. Would send invite to {to_email}")
        print(f"  Invite URL: {invite_url}")
        return False
    
    try:
        params = {
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": "You're Invited to Kagger AI!",
            "html": f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                <h1 style="color: #2563eb; margin-bottom: 20px;">Welcome to Kagger AI! 🎉</h1>
                
                <p style="font-size: 16px; color: #374151; line-height: 1.6;">
                    You've been invited to join <strong>Kagger AI</strong> - the AI-powered stock research platform.
                </p>
                
                <p style="font-size: 16px; color: #374151; line-height: 1.6;">
                    Click the button below to set your password and activate your account:
                </p>
                
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{invite_url}" 
                       style="background: linear-gradient(135deg, #3b82f6, #2563eb); 
                              color: white; 
                              padding: 14px 28px; 
                              text-decoration: none; 
                              border-radius: 8px; 
                              font-weight: bold;
                              font-size: 16px;
                              display: inline-block;">
                        Set Your Password
                    </a>
                </div>
                
                <p style="font-size: 14px; color: #6b7280; line-height: 1.6;">
                    This link will expire in 24 hours. If you didn't request this invite, 
                    you can safely ignore this email.
                </p>
                
                <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 30px 0;">
                
                <p style="font-size: 12px; color: #9ca3af; text-align: center;">
                    © 2024 Kagger AI - AI-Powered Stock Research
                </p>
            </div>
            """
        }
        
        response = resend.Emails.send(params)
        print(f"INFO: Invite email sent to {to_email}, ID: {response.get('id', 'unknown')}")
        return True
        
    except Exception as e:
        print(f"ERROR: Failed to send invite email to {to_email}: {e}")
        return False


def send_welcome_email(to_email: str, login_url: str) -> bool:
    """
    Send a welcome email after user sets their password.
    
    Args:
        to_email: The recipient's email address
        login_url: The URL to the login page
        
    Returns:
        True if email sent successfully, False otherwise
    """
    if not RESEND_API_KEY:
        print(f"WARNING: RESEND_API_KEY not set. Would send welcome to {to_email}")
        return False
    
    try:
        params = {
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": "Welcome to Kagger AI! 🚀",
            "html": f"""
            <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
                <h1 style="color: #10b981; margin-bottom: 20px;">You're All Set! 🎉</h1>
                
                <p style="font-size: 16px; color: #374151; line-height: 1.6;">
                    Congratulations! Your <strong>Kagger AI</strong> account is now active.
                </p>
                
                <p style="font-size: 16px; color: #374151; line-height: 1.6;">
                    You now have access to:
                </p>
                
                <ul style="font-size: 16px; color: #374151; line-height: 1.8;">
                    <li>🤖 <strong>AI Summaries</strong> - Instant analysis of analyst reports</li>
                    <li>📊 <strong>Technical Analysis</strong> - Charts, indicators, and signals</li>
                    <li>📰 <strong>AI-Enhanced News</strong> - Real-time market intelligence</li>
                    <li>🎯 <strong>Screener Insights</strong> - Fundamental analysis at a glance</li>
                </ul>
                
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{login_url}" 
                       style="background: linear-gradient(135deg, #10b981, #059669); 
                              color: white; 
                              padding: 14px 28px; 
                              text-decoration: none; 
                              border-radius: 8px; 
                              font-weight: bold;
                              font-size: 16px;
                              display: inline-block;">
                        Login to Kagger AI
                    </a>
                </div>
                
                <p style="font-size: 14px; color: #6b7280; line-height: 1.6;">
                    If you have any questions, feel free to reach out to the admin.
                </p>
                
                <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 30px 0;">
                
                <p style="font-size: 12px; color: #9ca3af; text-align: center;">
                    © 2024 Kagger AI - AI-Powered Stock Research
                </p>
            </div>
            """
        }
        
        response = resend.Emails.send(params)
        print(f"INFO: Welcome email sent to {to_email}, ID: {response.get('id', 'unknown')}")
        return True
        
    except Exception as e:
        print(f"ERROR: Failed to send welcome email to {to_email}: {e}")
        return False
