"""Authentication request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.enums import UserRole
from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class RegisterRequest(BaseModel):
    email: EmailStr
    # 12 rather than the customary 8. Length dominates character-class rules for
    # real-world resistance to guessing, so this enforces length and does not
    # impose composition rules that mainly push people toward "Password1!".
    password: str = Field(min_length=12, max_length=256)
    full_name: str = Field(min_length=1, max_length=120)
    role: UserRole = UserRole.PATIENT

    @field_validator("role")
    @classmethod
    def _no_self_service_admin(cls, v: UserRole) -> UserRole:
        """Self-registration may never mint an admin.

        Admin is the primary agent user and can see every patient's data.
        Creating one is an administrative act, not a signup form. Enforced in
        the schema so no route can forget it.
        """
        if v is UserRole.ADMIN:
            raise ValueError("admin accounts cannot be self-registered")
        return v


class UserOut(ORMModel):
    id: int
    email: str
    role: UserRole
    full_name: str | None
    is_active: bool


class SessionOut(ORMModel):
    """What the client learns after login.

    Deliberately contains no tokens: both tokens are set as httpOnly cookies
    and are never readable by JavaScript. Returning them in the body too would
    defeat the entire point of httpOnly.
    """

    user: UserOut
