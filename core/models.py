from pydantic import BaseModel
from typing import Optional, List


class SignupRequest(BaseModel):
    name: str
    email: str
    password: str


class GuestRequest(BaseModel):
    name: str
    email: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TaskCreateRequest(BaseModel):
    goal: str
    deadline: str
    reminder_time: str
    constraints: List[str] = []


class ConstraintUpdateRequest(BaseModel):
    constraints: List[str]


class CheckinRequest(BaseModel):
    completed: bool


class ChatRequest(BaseModel):
    message: str


class NoteRequest(BaseModel):
    kind: str
    title: str
    content: Optional[str] = None


class CancelReasonRequest(BaseModel):
    reason: str = ""
