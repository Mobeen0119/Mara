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
    ollama_url: Optional[str] = None
    ollama_model: Optional[str] = None
    openrouter_model: Optional[str] = None
    openrouter_key: Optional[str] = None


class CheckinTimeRequest(BaseModel):
    checkin_time: str


class ConstraintAddRequest(BaseModel):
    text: str = Field(min_length=1)


class ReminderSetRequest(BaseModel):
    reminder_time: str


class StakeCreateRequest(BaseModel):
    goal_id: int
    punishment: str = Field(min_length=1, max_length=200)


class StakeSettleRequest(BaseModel):
    reason: str = ""