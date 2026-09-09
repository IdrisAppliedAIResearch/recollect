"""Conversation-owned task controls and immutable artifact downloads."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .limits import validate_identifier


class TaskMessage(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    operation: Literal["steer", "cancel", "continue", "quiet"]
    text: str = Field(default="", max_length=16_000)
    quiet: bool | None = None
    reply_to: str | None = Field(default=None, max_length=300)

    @field_validator("request_id")
    @classmethod
    def valid_id(cls, value):
        return validate_identifier(value)


def install_task_routes(app, state) -> None:
    @app.get("/api/sessions/{session_id}/tasks")
    async def tasks(session_id: str):
        current = state()
        try:
            await asyncio.to_thread(current.sessions.get_session, session_id)
            return await current.tasks.snapshot(session_id)
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.post("/api/sessions/{session_id}/tasks/{task_id}/messages")
    async def message(session_id: str, task_id: str, body: TaskMessage):
        try:
            return await state().tasks.command(
                session_id,
                task_id,
                body.request_id,
                body.operation,
                body.text,
                body.quiet,
                body.reply_to,
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.get("/api/sessions/{session_id}/tasks/{task_id}/artifacts/{artifact_id}")
    async def artifact(session_id: str, task_id: str, artifact_id: str):
        try:
            item = await asyncio.to_thread(
                state().task_store.artifact,
                session_id,
                task_id,
                artifact_id,
            )
            return FileResponse(
                item["path"],
                media_type=item["media_type"],
                filename=item["filename"].split("/")[-1],
                headers={
                    "Cache-Control": "private, no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    @app.delete("/api/sessions/{session_id}/tasks/{task_id}")
    async def delete(session_id: str, task_id: str):
        try:
            await state().tasks.delete(session_id, task_id)
            return {"deleted": True}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/sessions/{session_id}/tasks/reset")
    async def reset(session_id: str):
        try:
            await state().tasks.reset(session_id)
            return {"reset": True}
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
