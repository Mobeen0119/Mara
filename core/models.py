from typing import List, Optional

from pydantic import BaseModel, EmailStr, Field


class SignupRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: EmailStr
    password: str = Field(min_length=6)


class GuestLoginRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    email: Optional[EmailStr] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoalCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    deadline: str
    reminder_time: str = "09:00"
    constraints: List[str] = Field(default_factory=list)


class GoalUpdateRequest(BaseModel):
    title: Optional[str] = None
    deadline: Optional[str] = None
    reminder_time: Optional[str] = None
    constraints: Optional[List[str]] = None


class ChatRequest(BaseModel):
    goal_id: Optional[int] = None
    message: str = Field(min_length=1)


class LinkRequest(BaseModel):
    message: str = Field(min_length=1)


class NudgeRequest(BaseModel):
    goal_id: int


class RegenPlanRequest(BaseModel):
    focus: Optional[str] = None


class CompleteGoalRequest(BaseModel):
    claimed_success: bool = True
    reason: str = ""


class DeleteGoalRequest(BaseModel):
    reason: str = ""


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


class ConstraintAddRequest(BaseModel):
    text: str = ""
    removeIndex: int | None = None


class BlockedWindowsRequest(BaseModel):
    blocked_windows: List[str] = Field(default_factory=list)


class PersonaRequest(BaseModel):
    persona: str = Field(default="", max_length=2000)


class LocationRequest(BaseModel):
    """Where the user actually lives — chat and plans only suggest services
    that exist there (Pakistan by default, change it in Settings)."""
    location: str = Field(default="", max_length=80)


class ReminderSetRequest(BaseModel):
    reminder_time: str


class StakeCreateRequest(BaseModel):
    goal_id: int
    punishment: str = Field(min_length=1, max_length=200)


class StakeSettleRequest(BaseModel):
    reason: str = ""