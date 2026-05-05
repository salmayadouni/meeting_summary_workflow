import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import AsyncGenerator

from googleapiclient.discovery import build
from openinference.semconv.trace import OpenInferenceSpanKindValues

from grafi.common.models.invoke_context import InvokeContext
from grafi.common.models.message import Message, Messages
from grafi.tools.tool import Tool

from auth import get_google_credentials
from config import TEMP_FILE


logger = logging.getLogger(__name__)


class GoogleCalendarTool(Tool):
    oi_span_type: OpenInferenceSpanKindValues = OpenInferenceSpanKindValues.TOOL

    calendar_id: str = "primary"
    timezone: str = "Europe/Zurich"
    default_time: str = "09:00"
    default_duration_hours: int = 1

    def calendar_service(self):
        return build("calendar", "v3", credentials=get_google_credentials())

    def normalise(self, value: str | None) -> str:
        return (value or "").strip().lower()

    def should_create_meet_link(self, location: str | None) -> bool:
        return self.normalise(location) in {"zoom", "google meet", "meet", "online"}

    def parse_start_time(self, event: dict) -> datetime:
        date_value = event.get("date")
        time_value = event.get("time") or self.default_time

        if not date_value:
            raise ValueError("Missing event date")

        if str(time_value).strip().upper() == "TBD":
            time_value = self.default_time

        return datetime.strptime(f"{date_value} {time_value}", "%Y-%m-%d %H:%M")

    def valid_attendees(self, event: dict) -> list[dict]:
        attendees = event.get("attendees") or []

        return [
            {"email": email.strip()}
            for email in attendees
            if isinstance(email, str) and "@" in email
        ]

    def build_event_payload(self, event: dict, start_time: datetime) -> dict:
        end_time = start_time + timedelta(hours=self.default_duration_hours)
        location = event.get("location") or "TBD"

        payload = {
            "summary": event.get("title") or "Untitled event",
            "location": location,
            "start": {
                "dateTime": start_time.isoformat(),
                "timeZone": self.timezone,
            },
            "end": {
                "dateTime": end_time.isoformat(),
                "timeZone": self.timezone,
            },
            "attendees": self.valid_attendees(event),
        }

        if self.should_create_meet_link(location):
            payload["conferenceData"] = {
                "createRequest": {
                    "requestId": uuid.uuid4().hex,
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }

        return payload

    def extract_video_link(self, calendar_event: dict) -> str | None:
        conference_data = calendar_event.get("conferenceData") or {}

        for entry in conference_data.get("entryPoints", []):
            if entry.get("entryPointType") == "video":
                return entry.get("uri")

        return None

    def find_matching_events(self, service, title: str) -> list[dict]:
        now = datetime.utcnow().isoformat() + "Z"
        expected = self.normalise(title)

        response = service.events().list(
            calendarId=self.calendar_id,
            timeMin=now,
            maxResults=100,
            singleEvents=True,
            orderBy="startTime",
            q=title,
        ).execute()

        return [
            event for event in response.get("items", [])
            if self.normalise(event.get("summary")) == expected
        ]

    def include_next_meeting(self, data: dict):
        next_meeting = data.get("next_meeting")
        events = data.setdefault("calendar_events", [])

        if not next_meeting:
            return

        if not next_meeting.get("date") or not next_meeting.get("time"):
            return

        next_title = self.normalise(next_meeting.get("title"))

        exists = any(
            self.normalise(event.get("title")) == next_title
            for event in events
        )

        if exists:
            return

        events.append({
            "title": next_meeting.get("title"),
            "date": next_meeting.get("date"),
            "time": next_meeting.get("time"),
            "location": next_meeting.get("location") or "TBD",
            "attendees": next_meeting.get("attendees") or [],
        })

    def create_event(self, service, event: dict) -> dict:
        start_time = self.parse_start_time(event)
        payload = self.build_event_payload(event, start_time)

        result = service.events().insert(
            calendarId=self.calendar_id,
            body=payload,
            conferenceDataVersion=1,
        ).execute()

        meet_link = None

        if self.should_create_meet_link(event.get("location")):
            meet_link = self.extract_video_link(result)

        return {
            "title": payload["summary"],
            "date": event.get("date"),
            "time": event.get("time") or self.default_time,
            "location": payload["location"],
            "status": "created",
            "link": result.get("htmlLink"),
            "meet_link": meet_link,
        }

    def delete_event(self, service, cancelled_event: dict) -> list[dict]:
        title = cancelled_event.get("title") or ""

        if not title.strip():
            return [{
                "title": title,
                "status": "failed",
                "error": "Missing title",
            }]

        matches = self.find_matching_events(service, title)

        if not matches:
            return [{
                "title": title,
                "status": "not_found",
            }]

        deleted = []

        for event in matches:
            service.events().delete(
                calendarId=self.calendar_id,
                eventId=event["id"],
            ).execute()

            deleted.append({
                "title": event.get("summary"),
                "was_at": event.get("start", {}).get("dateTime"),
                "status": "deleted",
            })

        return deleted

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

        service = self.calendar_service()
        created_events = []
        deleted_events = []

        self.include_next_meeting(data)

        for event in data.get("calendar_events", []):
            try:
                created_events.append(self.create_event(service, event))
            except Exception as exc:
                logger.exception("Failed to create calendar event")
                created_events.append({
                    "title": event.get("title"),
                    "status": "failed",
                    "error": str(exc),
                })

        for cancelled_event in data.get("cancelled_events", []):
            try:
                deleted_events.extend(
                    self.delete_event(service, cancelled_event)
                )
            except Exception as exc:
                logger.exception("Failed to delete calendar event")
                deleted_events.append({
                    "title": cancelled_event.get("title"),
                    "status": "failed",
                    "error": str(exc),
                })

        next_meeting_link = None
        next_meeting = data.get("next_meeting")

        if next_meeting:
            next_title = self.normalise(next_meeting.get("title"))

            for event in created_events:
                if self.normalise(event.get("title")) == next_title:
                    next_meeting_link = event.get("meet_link")
                    break

        pipeline_data = {
            "extracted_data": data,
            "calendar_result": {
                "success": True,
                "created_events": created_events,
                "deleted_events": deleted_events,
            },
            "next_meeting_link": next_meeting_link,
        }

        with open(TEMP_FILE, "w", encoding="utf-8") as file:
            json.dump(pipeline_data, file, ensure_ascii=False, indent=2)

        yield [Message(role="assistant", content=json.dumps(pipeline_data))]