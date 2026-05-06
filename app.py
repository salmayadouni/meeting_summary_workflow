"""
app.py — FastAPI web server for the Meeting Summary Workflow
Run with: uv run uvicorn app:app --reload --port 8000
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from loguru import logger

logging.getLogger("grafi").setLevel(logging.WARNING)
logger.disable("grafi")

try:
    from grafi.common.models.message import Message
    from grafi.common.models.invoke_context import InvokeContext
    from grafi.common.events.topic_events.publish_to_topic_event import PublishToTopicEvent
    from workflow import workflow, input_topic
    WORKFLOW_AVAILABLE = True
except ImportError:
    WORKFLOW_AVAILABLE = False
    print("⚠  grafi not installed — run: uv add grafi")

from ui_context import UISession, sessions


app = FastAPI(title="Meeting Summary Workflow", version="1.0.0")

static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ── Models ──────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    meeting_notes: str

class ConfirmRequest(BaseModel):
    answer: str          # "Y", "N", or "C"
    feedback: str = ""   # only relevant when answer == "N"


# ── Routes ──────────────────────────────────────────────────────────


def summarize_node_output(node_name: str, content: str) -> str:
    """Generate a one-line human-readable summary of a node's output."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        lines = [l for l in content.strip().split("\n") if l.strip()]
        return f"Summary ready · {len(lines)} lines"

    if node_name == "ExtractorNode":
        attendees = data.get("attendees", [])
        absent    = data.get("absent_people", [])
        actions   = data.get("action_items", [])
        decisions = data.get("decisions", [])
        cal_events= data.get("calendar_events", [])
        parts = []
        if attendees: parts.append(f"{len(attendees)} attendee(s)")
        if absent:    parts.append(f"{len(absent)} absent")
        if actions:   parts.append(f"{len(actions)} action item(s)")
        if decisions: parts.append(f"{len(decisions)} decision(s)")
        if cal_events:parts.append(f"{len(cal_events)} calendar event(s)")
        return " · ".join(parts) or "Extraction complete"

    if node_name == "MemoryNode":
        attendees = data.get("attendees", [])
        contacts  = data.get("contacts_found", [])
        if contacts:
            return f"Contacts resolved for {len(contacts)} people"
        return f"Memory enriched for {len(attendees)} people"

    if node_name == "CalendarNode":
        cal     = data.get("calendar_result", {})
        created = cal.get("created_events", [])
        deleted = cal.get("deleted_events", [])
        parts = []
        if created: parts.append(f"{len(created)} event(s) created")
        if deleted: parts.append(f"{len(deleted)} cancelled")
        return " · ".join(parts) or "No calendar changes"

    if node_name == "EmailComposerNode":
        emails = data.get("emails", [])
        return f"{len(emails)} email(s) composed" if emails else "No emails to compose"

    if node_name == "EmailSenderNode":
        er     = data.get("email_result", {})
        sent   = len(er.get("sent", []))
        skip   = len(er.get("skipped", []))
        failed = len(er.get("failed", []))
        parts  = []
        if sent:   parts.append(f"{sent} sent")
        if skip:   parts.append(f"{skip} skipped")
        if failed: parts.append(f"{failed} failed")
        return " · ".join(parts) or "No emails processed"

    if node_name == "FormatterNode":
        lines = [l for l in content.strip().split("\n") if l.strip()]
        return f"Summary ready · {len(lines)} lines"

    return "Completed"

@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    html_path = static_dir / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/health")
async def health():
    return {"status": "ok", "workflow_available": WORKFLOW_AVAILABLE}


@app.post("/confirm/{session_id}")
async def confirm(session_id: str, body: ConfirmRequest):
    """Called by the frontend when the user clicks Send / Revise / Cancel."""
    session = sessions.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found or already closed.")

    future = session.confirm_future
    if future is None or future.done():
        raise HTTPException(status_code=409, detail="No pending confirmation for this session.")

    future.set_result({"answer": body.answer.upper(), "feedback": body.feedback})
    return {"status": "ok"}


@app.post("/run")
async def run_workflow(request: RunRequest):
    """
    Streams SSE events:
      {"type": "session",       "session_id": "..."}
      {"node": "...",           "status": "pending|running|done"}
      {"type": "confirm_email", "name":..., "subject":..., "body":..., "recipient_type":...}
      {"type": "result",        "content": "..."}
      {"type": "error",         "content": "..."}
    """
    if not WORKFLOW_AVAILABLE:
        raise HTTPException(status_code=503, detail="grafi not installed.")

    meeting_notes = request.meeting_notes.strip()
    if not meeting_notes:
        raise HTTPException(status_code=400, detail="meeting_notes is empty.")

    NODES = [
        "ExtractorNode", "MemoryNode", "CalendarNode",
        "EmailComposerNode", "EmailSenderNode", "FormatterNode",
    ]

    conversation_id = uuid.uuid4().hex
    session = UISession(session_id=conversation_id)
    sessions[conversation_id] = session

    async def event_stream():
        try:
            yield f"data: {json.dumps({'type': 'session', 'session_id': conversation_id})}\n\n"
            for node in NODES:
                yield f"data: {json.dumps({'node': node, 'status': 'pending'})}\n\n"

            invoke_context = InvokeContext(
                user_id=uuid.uuid4().hex,
                conversation_id=conversation_id,
                invoke_id=uuid.uuid4().hex,
                assistant_request_id=uuid.uuid4().hex,
            )
            message = Message(role="user", content=meeting_notes)
            input_event = PublishToTopicEvent(
                event_id=uuid.uuid4().hex,
                invoke_context=invoke_context,
                publisher_name="user",
                publisher_type="external",
                topic_name=input_topic.name,
                data=[message],
                consumed_events=[],
            )
            input_event = await input_topic.publish_data(input_event)

            result_queue: asyncio.Queue = asyncio.Queue()

            async def _run():
                try:
                    node_idx = 0
                    async for result in workflow.invoke(input_event, is_sequential=False):
                        messages = list(result.data)

                        # Mark the corresponding node as running → done
                        if node_idx < len(NODES):
                            node_name = NODES[node_idx]
                            await result_queue.put(("node_running", node_name))
                            for msg in messages:
                                summary = summarize_node_output(node_name, msg.content)
                                await result_queue.put(("node_summary", (node_name, summary)))
                            await result_queue.put(("node_done", node_name))
                            node_idx += 1

                        # Always forward the latest output to the frontend —
                        # the frontend keeps only the last one (FormatterNode's text)
                        for msg in messages:
                            await result_queue.put(("result", msg.content))

                except Exception as exc:
                    await result_queue.put(("error", str(exc)))
                finally:
                    await result_queue.put(("done", None))

            asyncio.create_task(_run())

            while True:
                # Flush SSE events from tools (e.g. confirm_email previews)
                while not session.sse_queue.empty():
                    event = session.sse_queue.get_nowait()
                    yield f"data: {json.dumps(event)}\n\n"

                try:
                    kind, data = result_queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.05)
                    continue

                if kind == "done":
                    while not session.sse_queue.empty():
                        event = session.sse_queue.get_nowait()
                        yield f"data: {json.dumps(event)}\n\n"
                    break
                elif kind == "error":
                    yield f"data: {json.dumps({'type': 'error', 'content': data})}\n\n"
                    break
                elif kind == "node_running":
                    yield f"data: {json.dumps({'node': data, 'status': 'running'})}\n\n"
                elif kind == "node_done":
                    yield f"data: {json.dumps({'node': data, 'status': 'done'})}\n\n"
                elif kind == "node_summary":
                    node_name, summary = data
                    yield f"data: {json.dumps({'type': 'node_summary', 'node': node_name, 'summary': summary})}\n\n"
                elif kind == "result":
                    yield f"data: {json.dumps({'type': 'result', 'content': data})}\n\n"

        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'content': str(exc)})}\n\n"
        finally:
            sessions.pop(conversation_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )