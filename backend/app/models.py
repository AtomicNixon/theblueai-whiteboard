"""Pydantic models for request/response shapes."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CanvasCreate(BaseModel):
    title: str = ""


class CanvasOut(BaseModel):
    id: str
    owner_did: str
    title: str
    status: str
    created_at: str


class ElementIn(BaseModel):
    kind: Literal["text", "mark"]
    data: dict[str, Any] = Field(default_factory=dict)


class ElementsIn(BaseModel):
    """Bulk create — a vectorized image arrives as hundreds of rectangles."""
    elements: list[ElementIn] = Field(default_factory=list)


class ElementUpdate(BaseModel):
    data: dict[str, Any]


class ElementOut(BaseModel):
    id: str
    canvas_id: str
    kind: str
    owner_did: str
    data: dict[str, Any]
    created_at: str
    updated_at: str


class SnapshotOut(BaseModel):
    canvas: CanvasOut
    elements: list[ElementOut]
    me: str


class WsOp(BaseModel):
    """Live op broadcast to connected clients."""
    op: Literal["snapshot", "add", "update", "delete", "error"]
    canvas: CanvasOut | None = None
    elements: list[ElementOut] | None = None
    me: str | None = None
    element: ElementOut | None = None
    element_id: str | None = None
    message: str | None = None
