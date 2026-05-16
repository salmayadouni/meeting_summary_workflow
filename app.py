"""
app.py — FastAPI server using the MeetingAgent instead of the fixed pipeline.
"""

import asyncio
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ui_context import UISession, sessions

app = FastAPI(title="Meeting Workflow Agent", version="2.0.0")

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Models ──────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    meeting_notes: str

class ConfirmRequest(BaseModel):
    answer:   str
    feedback: str = ""


# ── Routes ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return HTMLResponse((static_dir / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/confirm/{session_id}")
async def confirm(session_id: str, body: ConfirmRequest):
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    future = session.confirm_future
    if future is None or future.done():
        raise HTTPException(status_code=409, detail="No pending confirmation.")
    future.set_result({"answer": body.answer.upper(), "feedback": body.feedback})
    return {"status": "ok"}


@app.post("/run")
async def run_workflow(request: RunRequest):
    """
    Run the meeting agent and stream all events as SSE.

    Event types:
      {"type": "session",        "session_id": "..."}
      {"type": "agent_thinking", "content": "..."}
      {"type": "tool_call",      "tool": "...", "args": {...}}
      {"type": "tool_result",    "tool": "...", "result": {...}}
      {"type": "confirm_email",  "name":..., "subject":..., "body":..., "recipient_type":...}
      {"type": "result",         "content": "..."}
      {"type": "error",          "content": "..."}
      {"type": "done"}
    """
    notes = request.meeting_notes.strip()
    if not notes:
        raise HTTPException(status_code=400, detail="meeting_notes is empty.")

    session_id = uuid.uuid4().hex
    session    = UISession(session_id=session_id)
    sessions[session_id] = session

    async def event_stream():
        try:
            yield f"data: {json.dumps({'type': 'session', 'session_id': session_id})}\n\n"

            from agent import MeetingAgent
            agent = MeetingAgent(session_id=session_id)

            # Run the agent as a background task so we can drain the queue
            task = asyncio.create_task(agent.run(notes))

            # Drain the SSE queue until the agent signals "done"
            while True:
                event = await asyncio.wait_for(session.sse_queue.get(), timeout=120)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break

            # Ensure the task is finished
            await task

        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'type': 'error', 'content': 'Agent timed out.'})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        finally:
            sessions.pop(session_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
