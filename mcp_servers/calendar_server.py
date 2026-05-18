"""
mcp_servers/calendar_server.py
Google Calendar MCP Server — exposes create/delete event tools via MCP protocol.

Run with:
    uv run python mcp_servers/calendar_server.py
"""

import json
import os
import sys
import uuid
from datetime import datetime, timedelta

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import get_google_credentials          
from config import CONTACTS_FILE                 
from googleapiclient.discovery import build      

mcp = FastMCP("Google Calendar MCP Server")

CALENDAR_ID    = "primary"
TIMEZONE       = "Europe/Zurich"
DURATION_HOURS = 1


# ── helpers ────────────────────────────────────────────────────────────

def _service():
    return build("calendar", "v3", credentials=get_google_credentials())


def _load_contacts() -> dict:
    if not os.path.exists(CONTACTS_FILE):
        return {}
    with open(CONTACTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def _resolve_emails(names: list, contacts: dict) -> list[dict]:
    """Convert a list of names into Google Calendar attendee dicts."""
    result, seen = [], set()
    for name in names:
        email = name if "@" in name else contacts.get(name, "")
        if email and email not in seen:
            result.append({"email": email.strip()})
            seen.add(email)
    return result


def _wants_meet_link(location: str) -> bool:
    return (location or "").strip().lower() in {"zoom", "google meet", "meet", "online"}


# ── tools ──────────────────────────────────────────────────────────────

@mcp.tool()
async def create_calendar_event(
    title: str,
    date: str,
    time: str,
    location: str,
    attendees: str,
) -> dict:
    """
    Create a Google Calendar event and send invitations to all attendees.

    Args:
        title:     Event title.
        date:      Date in YYYY-MM-DD format.
        time:      Start time in HH:MM format.
        location:  Location string (e.g. "Zoom", "Office").
        attendees: JSON array of attendee names, e.g. '["Maryem", "Reda"]'.

    Returns a dict with status, Google Calendar link, and Meet link (if any).
    """
    import time as time_module

    service  = _service()
    contacts = _load_contacts()

    names         = json.loads(attendees) if attendees else []
    attendee_list = _resolve_emails(names, contacts)

    start_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    end_dt   = start_dt + timedelta(hours=DURATION_HOURS)

    payload: dict = {
        "summary":  title,
        "location": location or "TBD",
        "start":    {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end":      {"dateTime": end_dt.isoformat(),   "timeZone": TIMEZONE},
        "attendees": attendee_list,
    }

    if _wants_meet_link(location):
        payload["conferenceData"] = {
            "createRequest": {
                "requestId":           uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    result = service.events().insert(
        calendarId=CALENDAR_ID,
        body=payload,
        conferenceDataVersion=1,
        sendUpdates="all",    
    ).execute()

    if _wants_meet_link(location):
        time_module.sleep(1)
        result = service.events().get(
            calendarId=CALENDAR_ID,
            eventId=result["id"],
        ).execute()

    meet_link = next(
        (
            e["uri"]
            for e in result.get("conferenceData", {}).get("entryPoints", [])
            if e.get("entryPointType") == "video"
        ),
        None,
    )

    return {
        "title":     title,
        "date":      date,
        "time":      time,
        "location":  location,
        "status":    "created",
        "link":      result.get("htmlLink"),
        "meet_link": meet_link,
    }


@mcp.tool()
async def delete_calendar_event(title: str) -> list:
    """
    Delete all upcoming Google Calendar events whose title matches exactly.

    Args:
        title: The event title to search for and delete.

    Returns a list of deleted events or [{"title": ..., "status": "not_found"}].
    """
    service = _service()
    now     = datetime.utcnow().isoformat() + "Z"

    response = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now,
        maxResults=100,
        singleEvents=True,
        orderBy="startTime",
        q=title,
    ).execute()

    matches = [
        e for e in response.get("items", [])
        if e.get("summary", "").strip().lower() == title.strip().lower()
    ]

    if not matches:
        return [{"title": title, "status": "not_found"}]

    deleted = []
    for event in matches:
        service.events().delete(
            calendarId=CALENDAR_ID,
            eventId=event["id"],
        ).execute()
        deleted.append({
            "title":  event.get("summary"),
            "was_at": event.get("start", {}).get("dateTime"),
            "status": "deleted",
        })

    return deleted


# ── entrypoint ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Starting Google Calendar MCP Server on http://0.0.0.0:8082/mcp")
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8082
    mcp.settings.streamable_http_path = "/mcp"
    mcp.run(transport="streamable-http")