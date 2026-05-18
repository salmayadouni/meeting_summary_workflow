"""
agent.py — LLM agent that orchestrates the meeting workflow.

Instead of a fixed pipeline, the agent receives the meeting notes and
decides itself which tools to call, in which order, based on the content.
"""

import asyncio
import json
import logging
import os

import openai

from config import OPENAI_API_KEY, CONTACTS_FILE, MEMORY_FILE
from mcp_client import call_mcp
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


# ── ICS helper ────────────────────────────────────────────────────────

def _generate_ics(title: str, date_str: str, time_str: str, location: str) -> str:
    """
    Generate a minimal iCalendar (.ics) string for the given event so the
    recipient can add it to their calendar with a single click.
    Handles multiple date formats: YYYY-MM-DD, "May 21, 2026", "May 21st", etc.
    """
    import uuid as _uuid
    import re
    from datetime import datetime, timedelta

    # Normalise ordinal suffixes: "21st" → "21", "12th" → "12"
    date_clean = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", date_str).strip()

    FORMATS = [
        ("%Y-%m-%d %H:%M", f"{date_clean} {time_str}"),
        ("%B %d, %Y %H:%M", f"{date_clean} {time_str}"),
        ("%b %d, %Y %H:%M", f"{date_clean} {time_str}"),
        ("%B %d %Y %H:%M", f"{date_clean} {time_str}"),
        ("%Y-%m-%d", date_clean),
        ("%B %d, %Y", date_clean),
        ("%b %d, %Y", date_clean),
        ("%B %d %Y", date_clean),
    ]

    dt = None
    for fmt, value in FORMATS:
        try:
            dt = datetime.strptime(value.strip(), fmt)
            break
        except ValueError:
            continue

    if dt is None:
        return ""

    dtstart = dt.strftime("%Y%m%dT%H%M%S")
    dtend   = (dt + timedelta(hours=1)).strftime("%Y%m%dT%H%M%S")
    uid     = str(_uuid.uuid4())

    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Meeting Workflow//EN\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:REQUEST\r\n"
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{dtstart}\r\n"
        f"DTEND:{dtend}\r\n"
        f"SUMMARY:{title}\r\n"
        f"LOCATION:{location}\r\n"
        "DESCRIPTION:Meeting invitation from the Meeting Workflow\r\n"
        f"UID:{uid}@meeting-workflow\r\n"
        "STATUS:CONFIRMED\r\n"
        "SEQUENCE:0\r\n"
        "BEGIN:VALARM\r\n"
        "TRIGGER:-PT15M\r\n"
        "ACTION:DISPLAY\r\n"
        "DESCRIPTION:Reminder\r\n"
        "END:VALARM\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR"
    )

CALENDAR_MCP = "http://localhost:8082/mcp"
GMAIL_MCP    = "http://localhost:8083/mcp"


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def normalize_text(text: str) -> str:
    return " ".join(text.lower().replace("—", "-").split())


def meeting_fingerprint(data: dict) -> str:
    parts = [
        data.get("meeting_title", ""),
        " ".join(data.get("attendees", [])),
        " ".join(data.get("absentees", [])),
        " ".join(data.get("key_decisions", [])),
        " ".join(data.get("action_items", [])),
        data.get("next_meeting", ""),
    ]
    return normalize_text(" ".join(parts))


SYSTEM_PROMPT = """You are a meeting summary agent for the Binome team.

Given raw meeting notes, you must complete these steps IN ORDER:

1. For each person mentioned (attendees + absent):
   - Call check_email_sent to see if they already received an email for this meeting
   - If not already sent: call send_email_with_confirmation to send them a summary
   - After successful send: call record_email_sent to remember it

2. For calendar:
   - If there's a next meeting mentioned: call create_calendar_event
   - If an event was cancelled: call delete_calendar_event

3. Finally: call format_summary with a clean, structured meeting summary

Important rules:
- Always check memory before sending emails
- Send emails one by one (not all at once) so the user can review each one
- If an email is cancelled by the user, still continue with the others
- Be concise in reasoning between tool calls
- The summary should include: meeting title, date, attendees, decisions, action items, calendar updates
- IMPORTANT: when calling send_email_with_confirmation, ALWAYS fill in next_meeting_title,
  next_meeting_date (YYYY-MM-DD), next_meeting_time (HH:MM), and next_meeting_location if a
  next meeting is mentioned in the notes. This attaches a calendar invite (.ics) to the email
  so the recipient can add the event to their calendar with one click.
- This applies to EVERY person — both attendees AND absent people must receive the calendar
  invite so they can all add the next meeting to their calendar.
"""



TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_email_sent",
            "description": "Check if an email was already sent to a person for this specific meeting. Always call this before send_email_with_confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":          {"type": "string", "description": "Full name of the person"},
                    "meeting_title": {"type": "string", "description": "Title of the meeting"},
                    "meeting_date":  {"type": "string", "description": "Date of the meeting (YYYY-MM-DD or as written)"},
                },
                "required": ["name", "meeting_title", "meeting_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email_with_confirmation",
            "description": "Compose and send a personalised email to a meeting participant. The user will review and approve it before sending.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient_name":  {"type": "string"},
                    "recipient_type":  {"type": "string", "enum": ["attendee", "absent"]},
                    "subject":         {"type": "string"},
                    "body":            {"type": "string", "description": "Plain text email body, max 150 words, end with 'Best regards, The Binome Team'"},
                    "meeting_title":        {"type": "string"},
                    "meeting_date":         {"type": "string"},
                    "next_meeting_title":   {"type": "string", "description": "Title of the NEXT meeting to attach as calendar invite (optional)"},
                    "next_meeting_date":    {"type": "string", "description": "Date of the next meeting in YYYY-MM-DD format (optional)"},
                    "next_meeting_time":    {"type": "string", "description": "Time of the next meeting in HH:MM format (optional)"},
                    "next_meeting_location":{"type": "string", "description": "Location of the next meeting (optional)"},
                },
                "required": ["recipient_name", "recipient_type", "subject", "body", "meeting_title", "meeting_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "record_email_sent",
            "description": "Record that an email was successfully sent. Call this after every successful send.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name":           {"type": "string"},
                    "meeting_title":  {"type": "string"},
                    "meeting_date":   {"type": "string"},
                    "attendees":      {"type": "array", "items": {"type": "string"}},
                    "absent_people":  {"type": "array", "items": {"type": "string"}},
                    "decisions":      {"type": "array", "items": {"type": "string"}},
                    "action_items":   {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "meeting_title", "meeting_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create a Google Calendar event and send invitations to all attendees.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title":     {"type": "string"},
                    "date":      {"type": "string", "description": "YYYY-MM-DD"},
                    "time":      {"type": "string", "description": "HH:MM"},
                    "location":  {"type": "string"},
                    "attendees": {"type": "array", "items": {"type": "string"}, "description": "List of attendee names"},
                },
                "required": ["title", "date", "time", "location", "attendees"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Delete a Google Calendar event by title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "format_summary",
            "description": "Return the final formatted meeting summary. Call this as the very last step.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Complete structured meeting summary"},
                },
                "required": ["summary"],
            },
        },
    },
]


# ── Agent ─────────────────────────────────────────────────────────────

class MeetingAgent:
    def __init__(self, session_id: str | None = None):
        self.session_id = session_id
        self.client     = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

    # ── Memory helpers ────────────────────────────────────────────────

    def _load_contacts(self) -> dict:
        if not os.path.exists(CONTACTS_FILE):
            return {}
        with open(CONTACTS_FILE, encoding="utf-8") as f:
            return json.load(f)

    def _load_memory(self) -> dict:
        if not os.path.exists(MEMORY_FILE):
            return {"sent_emails": [], "created_calendar_events": []}
        with open(MEMORY_FILE, encoding="utf-8") as f:
            return json.load(f)

    def _save_memory(self, memory: dict) -> None:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)

    # ── UI communication ──────────────────────────────────────────────

    async def _session(self):
        if not self.session_id:
            return None
        from ui_context import sessions
        return sessions.get(self.session_id)

    async def emit(self, event: dict) -> None:
        """Push an SSE event to the browser."""
        session = await self._session()
        if session:
            await session.sse_queue.put(event)

    async def _ask_confirmation(
        self, name: str, recipient_type: str, subject: str, body: str
    ) -> tuple[str, str]:
        """Show email confirmation modal, wait for Send/Revise/Cancel."""
        session = await self._session()

        if session:
            await session.sse_queue.put({
                "type":           "confirm_email",
                "name":           name,
                "recipient_type": recipient_type,
                "subject":        subject,
                "body":           body,
            })
            loop   = asyncio.get_running_loop()
            future = loop.create_future()
            session.confirm_future = future
            resp = await future
            return resp.get("answer", "C").upper(), resp.get("feedback", "")
        else:
            # Terminal fallback (main.py)
            print(f"\n{'─'*50}\nEmail for {name} ({recipient_type})\nSubject: {subject}\n{body}\n{'─'*50}")
            choice = input("\nSend, revise, or cancel? [Y/N/C]: ").strip().upper()
            if choice == "N":
                return "N", input("What should change? ")
            return choice, ""

    # ── Tool execution ────────────────────────────────────────────────

    async def _execute(self, tool_name: str, args: dict) -> dict | list:

        # ── check_email_sent ──────────────────────────────────────────
        if tool_name == "check_email_sent":
            memory = self._load_memory()
            name = args["name"].strip().lower()

            current_data = {
                "meeting_title": args.get("meeting_title", ""),
                "attendees": args.get("attendees", []),
                "absentees": args.get("absent_people", []),
                "key_decisions": args.get("decisions", []),
                "action_items": args.get("action_items", []),
                "next_meeting": args.get("next_meeting", ""),
            }

            current_fp = meeting_fingerprint(current_data)

            best_score = 0
            best_match = None

            for e in memory.get("sent_emails", []):
                if e.get("name", "").strip().lower() != name:
                    continue

                old_fp = e.get("fingerprint", "")
                if not old_fp:
                    old_data = {
                        "meeting_title": e.get("meeting_title", ""),
                        "attendees": e.get("snapshot", {}).get("attendees", []),
                        "absentees": e.get("snapshot", {}).get("absent_people", []),
                        "key_decisions": e.get("snapshot", {}).get("decisions", []),
                        "action_items": e.get("snapshot", {}).get("action_items", []),
                    }
                    old_fp = meeting_fingerprint(old_data)

                score = similarity(current_fp, old_fp)

                if score > best_score:
                    best_score = score
                    best_match = e

            already = best_score >= 0.75

            return {
                "already_sent": already,
                "similarity": round(best_score, 2),
                "name": args["name"],
                "reason": "Similar meeting already processed" if already else "No similar previous email found",
                "best_match": best_match,
            }

        elif tool_name == "send_email_with_confirmation":
            name           = args["recipient_name"]
            recipient_type = args["recipient_type"]
            subject        = args["subject"]
            meeting_title  = args["meeting_title"]
            meeting_date   = args["meeting_date"]
            current_body   = args["body"]

            contacts = self._load_contacts()
            address  = contacts.get(name)
            if not address:
                return {"status": "failed", "reason": f"{name} not found in contacts"}

            while True:
                choice, feedback = await self._ask_confirmation(
                    name, recipient_type, subject, current_body
                )

                if choice == "Y":
                    # Generate .ics calendar invite for the next meeting.
                    # Try the fields the LLM passed first; fall back to memory file.
                    ics = ""
                    nm_title    = args.get("next_meeting_title", "").strip()
                    nm_date     = args.get("next_meeting_date", "").strip()
                    nm_time     = args.get("next_meeting_time", "09:00").strip() or "09:00"
                    nm_location = args.get("next_meeting_location", "TBD").strip() or "TBD"

                    # Fallback: read next_meeting from the memory file's
                    # last created_calendar_events entry if the LLM forgot to pass it
                    if not (nm_title and nm_date):
                        try:
                            memory = self._load_memory()
                            events = memory.get("created_calendar_events", [])
                            if events:
                                last = events[-1]
                                nm_title    = nm_title    or last.get("title", "")
                                nm_date     = nm_date     or last.get("date",  "")
                                nm_time     = nm_time     or last.get("time",  "09:00")
                                nm_location = nm_location or last.get("location", "TBD")
                        except Exception:
                            pass

                    if nm_title and nm_date:
                        ics = _generate_ics(nm_title, nm_date, nm_time, nm_location)

                    await call_mcp(GMAIL_MCP, "send_email", {
                        "to_address":  address,
                        "subject":     subject,
                        "body":        current_body,
                        "ics_content": ics,
                    })
                    return {
                        "status":    "sent",
                        "name":      name,
                        "to":        address,
                        "calendar_invite_attached": bool(ics),
                    }

                elif choice == "N" and feedback:
                    await self.emit({"type": "agent_thinking", "content": f"Revising email for {name}…"})
                    resp = await self.client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content":
                            f"Rewrite this email based on the feedback.\n\n"
                            f"Original:\n{current_body}\n\n"
                            f"Feedback: {feedback}\n\n"
                            f"Return only the email body, plain text, under 150 words, "
                            f"ending with 'Best regards, The Binome Team'."
                        }],
                        max_tokens=400,
                    )
                    current_body = resp.choices[0].message.content.strip()
                    continue

                elif choice == "C":
                    return {"status": "cancelled_by_user", "name": name}

                else:
                    continue

        elif tool_name == "record_email_sent":
            memory = self._load_memory()

            meeting_data = {
                "meeting_title": args.get("meeting_title", ""),
                "attendees": args.get("attendees", []),
                "absentees": args.get("absent_people", []),
                "key_decisions": args.get("decisions", []),
                "action_items": args.get("action_items", []),
            }

            memory["sent_emails"].append({
                "name": args["name"],
                "meeting_title": args["meeting_title"],
                "meeting_date": args.get("meeting_date", ""),
                "fingerprint": meeting_fingerprint(meeting_data),
                "snapshot": {
                    "attendees": args.get("attendees", []),
                    "absent_people": args.get("absent_people", []),
                    "decisions": args.get("decisions", []),
                    "action_items": args.get("action_items", []),
                },
            })

            self._save_memory(memory)
            return {"status": "recorded", "name": args["name"]}

        # ── create_calendar_event ─────────────────────────────────────
        elif tool_name == "create_calendar_event":
            result = await call_mcp(CALENDAR_MCP, "create_calendar_event", {
                "title":     args["title"],
                "date":      args["date"],
                "time":      args["time"],
                "location":  args["location"],
                "attendees": json.dumps(args.get("attendees", [])),
            })
            # Cache event details in memory so send_email can attach the ICS
            try:
                memory = self._load_memory()
                memory.setdefault("created_calendar_events", []).append({
                    "title":    args["title"],
                    "date":     args["date"],
                    "time":     args["time"],
                    "location": args["location"],
                })
                self._save_memory(memory)
            except Exception:
                pass
            return result

        # ── delete_calendar_event ─────────────────────────────────────
        elif tool_name == "delete_calendar_event":
            result = await call_mcp(CALENDAR_MCP, "delete_calendar_event",
                                    {"title": args["title"]})
            return result if isinstance(result, list) else [result]

        # ── format_summary ────────────────────────────────────────────
        elif tool_name == "format_summary":
            return {"summary": args["summary"]}

        return {"error": f"Unknown tool: {tool_name}"}

    # ── Main loop ─────────────────────────────────────────────────────

    async def run(self, meeting_notes: str) -> None:
        """
        Run the agent. All events are pushed to the session's SSE queue.
        Blocks until the agent finishes or an error occurs.
        """
        messages: list = [
            {"role": "system",  "content": SYSTEM_PROMPT},
            {"role": "user",    "content": meeting_notes},
        ]

        try:
            while True:
                response = await self.client.chat.completions.create(
                    model="gpt-4o",
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                )

                msg    = response.choices[0].message
                reason = response.choices[0].finish_reason

                # Append assistant turn to history
                messages.append(msg)

                # Agent finished
                if reason == "stop":
                    if msg.content:
                        await self.emit({"type": "result", "content": msg.content})
                    break

                # No tool calls — shouldn't happen but guard anyway
                if not msg.tool_calls:
                    break

                # Stream any reasoning text before tool calls
                if msg.content:
                    await self.emit({"type": "agent_thinking", "content": msg.content})

                tool_results = []

                for tc in msg.tool_calls:
                    name = tc.function.name
                    args = json.loads(tc.function.arguments)

                    # Notify UI that a tool is being called
                    await self.emit({"type": "tool_call", "tool": name, "args": args})

                    result = await self._execute(name, args)

                    # Notify UI of the result
                    await self.emit({"type": "tool_result", "tool": name, "result": result})

                    # format_summary → also emit the final result
                    if name == "format_summary":
                        await self.emit({"type": "result", "content": args.get("summary", "")})

                    tool_results.append({
                        "role":         "tool",
                        "tool_call_id": tc.id,
                        "content":      json.dumps(result),
                    })

                messages.extend(tool_results)

        except Exception as exc:
            logger.exception("Agent error")
            await self.emit({"type": "error", "content": str(exc)})

        finally:
            await self.emit({"type": "done"})