"""
services/auth_service.py
========================
Authentication and Role-Based Access Control (RBAC) service for Smart Canteen.
Handles credential verification, secure password hashing, student registration,
and server-side role enforcement.
"""

from typing import Dict, Any, Optional
from werkzeug.security import generate_password_hash, check_password_hash

from config import (
    ROLE_CUSTOMER,
    ROLE_STAFF,
    ROLE_ADMIN,
)
from database import (
    get_user_by_email,
    get_user_by_id,
    create_user,
    update_user_last_active,
)


class AuthService:
    """Authentication and session verification service."""

    @staticmethod
    def login(email: str, password: str, required_role: Optional[str] = None) -> Dict[str, Any]:
        """
        Authenticate user with email and password.
        If required_role is specified (e.g. ROLE_ADMIN), ensures user has that role.
        For Admin, rejects invalid credentials with generic 'Invalid admin credentials.'
        without revealing whether the email or password was wrong.
        """
        if not email or not password:
            msg = "Invalid admin credentials." if required_role == ROLE_ADMIN else "Email and password are required."
            return {"success": False, "message": msg}

        user = get_user_by_email(email.strip())
        if not user:
            msg = "Invalid admin credentials." if required_role == ROLE_ADMIN else "Invalid email or password."
            return {"success": False, "message": msg}

        # Verify password hash
        if not check_password_hash(user["password_hash"], password):
            msg = "Invalid admin credentials." if required_role == ROLE_ADMIN else "Invalid email or password."
            return {"success": False, "message": msg}

        # Role verification if required
        if required_role and user.get("role") != required_role:
            msg = "Invalid admin credentials." if required_role == ROLE_ADMIN else "Unauthorized role access."
            return {"success": False, "message": msg}

        # Update last active timestamp
        update_user_last_active(user["id"])

        user_info = {
            "id": user["id"],
            "name": user["name"],
            "email": user["email"],
            "role": user["role"],
            "phone": user.get("phone", ""),
            "station_code": user.get("station_code", ""),
        }
        return {"success": True, "user": user_info}

    @staticmethod
    def register(
        name: str,
        email: str,
        password: str,
        phone: str = "",
        role: str = ROLE_CUSTOMER,
    ) -> Dict[str, Any]:
        """Register a new customer/student account."""
        if not name or not name.strip():
            return {"success": False, "message": "Name is required."}
        if not email or "@" not in email:
            return {"success": False, "message": "Valid email address is required."}
        if not password or len(password) < 4:
            return {"success": False, "message": "Password must be at least 4 characters."}

        existing = get_user_by_email(email.strip())
        if existing:
            return {
                "success": False,
                "message": "Email is already registered. Please sign in instead.",
            }

        hashed = generate_password_hash(password)
        try:
            user_id = create_user(
                name=name.strip(),
                email=email.strip(),
                password_hash=hashed,
                role=role,
                phone=phone.strip(),
            )
            return {
                "success": True,
                "user": {
                    "id": user_id,
                    "name": name.strip(),
                    "email": email.strip().lower(),
                    "role": role,
                    "phone": phone.strip(),
                },
                "message": "Account created successfully.",
            }
        except Exception as e:
            return {"success": False, "message": f"Registration error: {str(e)}"}
