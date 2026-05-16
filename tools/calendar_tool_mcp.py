"""
tools/calendar_tool.py
Graphite tool that creates and deletes Google Calendar events
by calling the Calendar MCP server instead of the Google API directly.
"""

import json
import logging
from datetime import datetime
from typing import AsyncGenerator

from grafi.common.models.invoke_context import InvokeContext
from grafi.common.models.message import Message, Messages
from grafi.tools.tool import Tool
from openinference.semconv.trace import OpenInferenceSpanKindValues

from config import TEMP_FILE
from mcp_client import call_mcp

logger = logging.getLogger(__name__)

CALENDAR_MCP_URL = "http://localhost:8082/mcp"


class GoogleCalendarTool(Tool):
    oi_span_type: OpenInferenceSpanKindValues = OpenInferenceSpanKindValues.TOOL

    default_time:           str = "09:00"
    calendar_mcp_url:       str = CALENDAR_MCP_URL

    # ── helpers ──────────────────────────────────────────────────────

    def normalise(self, value: str | None) -> str:
        return (value or "").strip().lower()

    def parse_date(self, event: dict) -> str:
        date_value = event.get("date")
        if not date_value:
            raise ValueError("Missing event date")
        return date_value

    def parse_time(self, event: dict) -> str:
        t = (event.get("time") or self.default_time).strip()
        return self.default_time if t.upper() == "TBD" else t

    def include_next_meeting(self, data: dict):
        """Add the next_meeting to calendar_events if not already present."""
        next_meeting = data.get("next_meeting")
        events = data.setdefault("calendar_events", [])

        if not next_meeting:
            return
        if not next_meeting.get("date") or not next_meeting.get("time"):
            return

        next_title = self.normalise(next_meeting.get("title"))
        if any(self.normalise(e.get("title")) == next_title for e in events):
            return

        # Invite everyone in the meeting (attendees + absent)
        all_people = list(dict.fromkeys(
            (data.get("attendees") or []) + (data.get("absent_people") or [])
        ))
        invite_list = next_meeting.get("attendees") or all_people

        events.append({
            "title":     next_meeting.get("title"),
            "date":      next_meeting.get("date"),
            "time":      next_meeting.get("time"),
            "location":  next_meeting.get("location") or "TBD",
            "attendees": invite_list,
        })

    # ── MCP calls ────────────────────────────────────────────────────

    async def create_event(self, event: dict) -> dict:
        """Create a calendar event via the Calendar MCP server."""
        try:
            return await call_mcp(
                self.calendar_mcp_url,
                "create_calendar_event",
                {
                    "title":     event.get("title") or "Untitled event",
                    "date":      self.parse_date(event),
                    "time":      self.parse_time(event),
                    "location":  event.get("location") or "TBD",
                    "attendees": json.dumps(event.get("attendees") or []),
                },
            )
        except Exception as exc:
            logger.exception("Calendar MCP create_event failed")
            return {
                "title":  event.get("title"),
                "status": "failed",
                "error":  str(exc),
            }

    async def delete_event(self, cancelled_event: dict) -> list:
        """Delete a calendar event via the Calendar MCP server."""
        title = cancelled_event.get("title") or ""
        if not title.strip():
            return [{"title": title, "status": "failed", "error": "Missing title"}]

        try:
            result = await call_mcp(
                self.calendar_mcp_url,
                "delete_calendar_event",
                {"title": title},
            )
            # MCP returns a list; wrap if needed
            return result if isinstance(result, list) else [result]
        except Exception as exc:
            logger.exception("Calendar MCP delete_event failed")
            return [{"title": title, "status": "failed", "error": str(exc)}]

    # ── Graphite invoke ───────────────────────────────────────────────

    async def invoke(
        self,
        invoke_context: InvokeContext,
        input_data: Messages,
    ) -> AsyncGenerator[Messages, None]:

        try:
            data = json.loads(input_data[0].content)
        except json.JSONDecodeError:
            response = {
                "extracted_data": input_data[0].content,
                "calendar_result": {
                    "success": False,
                    "created_events": [],
                    "deleted_events": [],
                    "error": "Input was not valid JSON",
                },
            }
            yield [Message(role="assistant", content=json.dumps(response))]
            return

        created_events: list = []
        deleted_events: list = []

        self.include_next_meeting(data)

        for event in data.get("calendar_events", []):
            created_events.append(await self.create_event(event))

        for cancelled in data.get("cancelled_events", []):
            deleted_events.extend(await self.delete_event(cancelled))

        # Resolve next_meeting Google Meet link from created events
        next_meeting_link = None
        next_meeting = data.get("next_meeting")
        if next_meeting:
            next_title = self.normalise(next_meeting.get("title"))
            for evt in created_events:
                if self.normalise(evt.get("title")) == next_title:
                    next_meeting_link = evt.get("meet_link")
                    break

        pipeline_data = {
            "extracted_data": data,
            "calendar_result": {
                "success":        True,
                "created_events": created_events,
                "deleted_events": deleted_events,
            },
            "next_meeting_link": next_meeting_link,
        }

        with open(TEMP_FILE, "w", encoding="utf-8") as f:
            json.dump(pipeline_data, f, ensure_ascii=False, indent=2)

        yield [Message(role="assistant", content=json.dumps(pipeline_data))]
