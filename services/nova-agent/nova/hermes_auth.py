"""
Hermes Core JWT authentication helper.

Generates JWT tokens for authenticating with Hermes Core API.
Supports multi-tenant architecture by accepting user_id parameter.
"""

import os
import jwt
from datetime import datetime, timedelta, timezone


def generate_hermes_jwt(user_id: str | None = None, user_email: str | None = None) -> str:
    """Generate JWT token for Hermes Core authentication.
    
    Args:
        user_id: User ID for multi-tenant isolation. If None, uses environment default.
        user_email: User email. If None, uses environment default.
    
    Returns:
        JWT token string for Authorization header.
    
    Note:
        For proper multi-tenant support, callers should pass the authenticated
        user's ID from the WebRTC session context.
    """
    jwt_secret = os.getenv("HERMES_JWT_SECRET", "uJ4+zxl0TxA+KifORrMIVZItAPEX+I9WEL9VbSkZC3k=")
    jwt_algorithm = os.getenv("HERMES_JWT_ALGORITHM", "HS256")
    jwt_issuer = os.getenv("HERMES_JWT_ISSUER", "cig")
    
    # Use provided user_id or fall back to environment/default
    # CRITICAL: Default must match the userId used during email ingestion
    effective_user_id = user_id or os.getenv(
        "HERMES_USER_ID", 
        "dfd9379f-a9cd-4241-99e7-140f5e89e3cd"  # Primary user UUID
    )
    effective_email = user_email or os.getenv(
        "HERMES_USER_EMAIL", 
        "eleazarf@icloud.com"
    )
    user_name = os.getenv("HERMES_USER_NAME", "Nova Agent")
    
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=30)
    
    payload = {
        'sub': effective_user_id,
        'userId': effective_user_id,
        'email': effective_email,
        'name': user_name,
        'roles': ['admin'],
        'permissions': ['*'],
        'platformRole': 'admin',
        'iat': now,
        'exp': exp,
        'iss': jwt_issuer,
    }
    
    return jwt.encode(payload, jwt_secret, algorithm=jwt_algorithm)
