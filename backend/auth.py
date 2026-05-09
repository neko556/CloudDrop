"""
CloudDrop — App-Level Cognito Authentication (Direct API)

Uses Cognito User Pool API directly (no Hosted UI redirect).
- Signup with email + password → email verification code
- Login with email + password → get tokens directly
- Sessions stored in Redis with 24-hour TTL

No callback URL needed — no HTTPS requirement.
"""

import secrets
import json
import logging
import hashlib
import hmac
import base64
import boto3

from config import settings

logger = logging.getLogger("clouddrop.auth")

# ── Cognito Client ──────────────────────────────────────────────────────────

_cognito_client = None


def _get_cognito():
    global _cognito_client
    if _cognito_client is None:
        _cognito_client = boto3.client(
            "cognito-idp",
            region_name=settings.COGNITO_REGION,
        )
    return _cognito_client


def _compute_secret_hash(username: str) -> str:
    """Compute the SECRET_HASH required when app client has a secret."""
    msg = username + settings.COGNITO_CLIENT_ID
    dig = hmac.new(
        settings.COGNITO_CLIENT_SECRET.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(dig).decode()


# ── Session Management (Redis) ──────────────────────────────────────────────

SESSION_PREFIX = "session:"
SESSION_TTL = 86400  # 24 hours


def _get_redis():
    """Reuse the Redis client from shortener module."""
    from shortener import _get_redis as get_redis
    return get_redis()


def create_session(email: str) -> str:
    """Create a new session for the given user email. Returns session token."""
    r = _get_redis()
    token = secrets.token_urlsafe(48)
    session_data = json.dumps({"email": email})
    r.setex(f"{SESSION_PREFIX}{token}", SESSION_TTL, session_data)
    logger.info(f"Session created for: {email}")
    return token


def get_session(token: str) -> dict | None:
    """Get session data from a session token."""
    if not token:
        return None
    r = _get_redis()
    raw = r.get(f"{SESSION_PREFIX}{token}")
    if raw is None:
        return None
    return json.loads(raw)


def delete_session(token: str):
    """Delete a session (logout)."""
    if not token:
        return
    r = _get_redis()
    r.delete(f"{SESSION_PREFIX}{token}")


def get_user_from_request(request) -> str | None:
    """Extract user email from session cookie."""
    token = request.cookies.get("clouddrop_session")
    if not token:
        return None
    session = get_session(token)
    if session is None:
        return None
    return session.get("email")


# ── Cognito API: Signup ─────────────────────────────────────────────────────

def signup_user(email: str, password: str) -> dict:
    """
    Register a new user with Cognito.
    Returns {"success": True} or {"success": False, "error": "message"}
    """
    client = _get_cognito()
    try:
        client.sign_up(
            ClientId=settings.COGNITO_CLIENT_ID,
            SecretHash=_compute_secret_hash(email),
            Username=email,
            Password=password,
            UserAttributes=[
                {"Name": "email", "Value": email},
            ],
        )
        return {"success": True, "message": "Verification code sent to your email."}
    except client.exceptions.UsernameExistsException:
        return {"success": False, "error": "An account with this email already exists."}
    except client.exceptions.InvalidPasswordException as e:
        return {"success": False, "error": "Password too weak. Use 8+ chars with uppercase, lowercase, number, and symbol."}
    except Exception as e:
        logger.error(f"Signup error: {e}")
        return {"success": False, "error": str(e)}


# ── Cognito API: Confirm Signup ─────────────────────────────────────────────

def confirm_signup(email: str, code: str) -> dict:
    """
    Confirm user signup with the verification code sent to email.
    """
    client = _get_cognito()
    try:
        client.confirm_sign_up(
            ClientId=settings.COGNITO_CLIENT_ID,
            SecretHash=_compute_secret_hash(email),
            Username=email,
            ConfirmationCode=code,
        )
        return {"success": True, "message": "Email verified! You can now log in."}
    except client.exceptions.CodeMismatchException:
        return {"success": False, "error": "Invalid verification code."}
    except client.exceptions.ExpiredCodeException:
        return {"success": False, "error": "Verification code has expired. Please sign up again."}
    except Exception as e:
        logger.error(f"Confirm signup error: {e}")
        return {"success": False, "error": str(e)}


# ── Cognito API: Login ──────────────────────────────────────────────────────

def login_user(email: str, password: str) -> dict:
    """
    Authenticate user with Cognito using USER_PASSWORD_AUTH flow.
    Returns {"success": True, "email": "..."} or {"success": False, "error": "..."}
    """
    client = _get_cognito()
    try:
        response = client.initiate_auth(
            ClientId=settings.COGNITO_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": email,
                "PASSWORD": password,
                "SECRET_HASH": _compute_secret_hash(email),
            },
        )
        # Check if there's a challenge (e.g., NEW_PASSWORD_REQUIRED)
        if "ChallengeName" in response:
            return {"success": False, "error": f"Auth challenge required: {response['ChallengeName']}"}

        return {"success": True, "email": email}

    except client.exceptions.NotAuthorizedException:
        return {"success": False, "error": "Incorrect email or password."}
    except client.exceptions.UserNotConfirmedException:
        return {"success": False, "error": "Email not verified. Please check your email for the verification code.", "needs_confirmation": True}
    except client.exceptions.UserNotFoundException:
        return {"success": False, "error": "No account found with this email."}
    except Exception as e:
        logger.error(f"Login error: {e}")
        return {"success": False, "error": str(e)}
