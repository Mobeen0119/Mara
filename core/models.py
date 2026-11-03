import re
from datetime import date
from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _check_hhmm(v):
    if v is not None and not _HHMM.match(v):
        raise ValueError("expected HH:MM (24h)")
    return v


def _check_deadline(v):
    try:
        d = date.fromisoformat(v)
    except (TypeError, ValueError):
        raise ValueError("deadline must be a real date, YYYY-MM-DD")
    if d.year < 2000 or d.year > 2100:
        raise ValueError("deadline out of range")
    return v


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class GuestLoginRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    # Accepted for old clients but ignored: a guest never claims an email, so a
    # guest can never be handed someone else's account.
    email: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)


class GoalCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    deadline: str
    reminder_time: str = "09:00"
    constraints: List[str] = Field(default_factory=list, max_length=20)

    _v_deadline = field_validator("deadline")(_check_deadline)
    _v_reminder = field_validator("reminder_time")(_check_hhmm)

    @field_validator("constraints")
    @classmethod
    def _cons(cls, v):
        return [c.strip()[:120] for c in v if c and c.strip()]


class GoalUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=120)
    deadline: Optional[str] = None
    reminder_time: Optional[str] = None
    constraints: Optional[List[str]] = Field(default=None, max_length=20)

    @field_validator("deadline")
    @classmethod
    def _dl(cls, v):
        return None if v is None else _check_deadline(v)

    @field_validator("reminder_time")
    @classmethod
    def _rt(cls, v):
        return _check_hhmm(v)


class ChatRequest(BaseModel):
    goal_id: Optional[int] = None
    message: str = Field(min_length=1, max_length=4000)


class QuickCommandRequest(BaseModel):
    command: str = Field(default="", max_length=40)


class TimezoneRequest(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)


class LinkRequest(BaseModel):
    message: str = Field(min_length=1)


class NudgeRequest(BaseModel):
    goal_id: int


class RegenPlanRequest(BaseModel):
    focus: Optional[str] = None


class CompleteGoalRequest(BaseModel):
    claimed_success: bool = True
    reason: str = Field(default="", max_length=500)


class DeleteGoalRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class AcknowledgeRequest(BaseModel):
    note: Optional[str] = None


class CheckinRespondRequest(BaseModel):
    done: bool
    note: Optional[str] = None


class LLMSettingsRequest(BaseModel):
    """Any OpenAI-compatible endpoint: paste a key + pick a model. Provider and
    base URL optional (a sensible default endpoint is used when blank)."""
    provider: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    enable_local: Optional[bool] = None
    # Forget the stored key and fall back to the environment (.env).
    clear_key: Optional[bool] = None
    # Legacy fields, still accepted for older clients.
    ollama_url: Optional[str] = None
    ollama_model: Optional[str] = None
    openrouter_model: Optional[str] = None
    openrouter_key: Optional[str] = None


class LLMTestRequest(BaseModel):
    """Probe the provider with a one-word generation. Any field left blank is
    resolved from saved settings, then .env — so you can test before saving."""
    provider: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None


class CheckinTimeRequest(BaseModel):
    checkin_time: str

    _v = field_validator("checkin_time")(_check_hhmm)


class ConstraintAddRequest(BaseModel):
    text: str = Field(default="", max_length=120)
    removeIndex: int | None = None


class BlockedWindowsRequest(BaseModel):
    blocked_windows: List[str] = Field(default_factory=list, max_length=30)


class PersonaRequest(BaseModel):
    persona: str = Field(default="", max_length=2000)


class LocationRequest(BaseModel):
    """Where the user actually lives — chat and plans only suggest services
    that exist there (Pakistan by default, change it in Settings)."""
    location: str = Field(default="", max_length=80)


class ReminderSetRequest(BaseModel):
    reminder_time: str

    _v = field_validator("reminder_time")(_check_hhmm)


class StakeCreateRequest(BaseModel):
    goal_id: int
    punishment: str = Field(min_length=1, max_length=200)


class StakeSettleRequest(BaseModel):
    reason: str = ""