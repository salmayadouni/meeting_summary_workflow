import json
import logging
import os
from datetime import datetime
from typing import AsyncGenerator

from openinference.semconv.trace import OpenInferenceSpanKindValues

from grafi.common.models.invoke_context import InvokeContext
from grafi.common.models.message import Message, Messages
from grafi.tools.tool import Tool

from config import MEMORY_FILE


logger = logging.getLogger(__name__)


class MemoryTool(Tool):
    oi_span_type: OpenInferenceSpanKindValues = OpenInferenceSpanKindValues.TOOL

    def default_memory(self) -> dict:
        return {
            "decisions": [],
            "action_items": [],
            "created_calendar_events": [],
            "sent_emails": [],
        }

    def load_memory(self) -> dict:
        if not os.path.exists(MEMORY_FILE):
            return self.default_memory()

        with open(MEMORY_FILE, "r", encoding="utf-8") as file:
            memory = json.load(file)

        memory.setdefault("decisions", [])
        memory.setdefault("action_items", [])
        memory.setdefault("created_calendar_events", [])
        memory.setdefault("sent_emails", [])  

        return memory

    def save_memory(self, memory: dict) -> None:
        with open(MEMORY_FILE, "w", encoding="utf-8") as file:
            json.dump(memory, file, ensure_ascii=False, indent=2)

    def normalise(self, value: str | None) -> str:
        return (value or "").strip().lower()

    def event_exists(self, memory: dict, title: str | None, date: str | None) -> bool:
        expected_title = self.normalise(title)

        return any(
            self.normalise(event.get("title")) == expected_title
            and event.get("date") == date
            for event in memory.get("created_calendar_events", [])
        )

    def store_decisions(self, memory: dict, decisions: list[dict], meeting_date: str) -> None:
        for decision in decisions:
            memory["decisions"].append({
                "meeting_date": meeting_date,
                "what": decision.get("what"),
                "why": decision.get("why"),
            })

    def store_action_items(
        self,
        memory: dict,
        action_items: list[dict],
        meeting_date: str,
    ) -> None:
        for item in action_items:
            memory["action_items"].append({
                "meeting_date": meeting_date,
                "owner": item.get("owner"),
                "task": item.get("task"),
                "deadline": item.get("deadline"),
            })

    def filter_new_calendar_events(
        self,
        memory: dict,
        calendar_events: list[dict],
    ) -> tuple[list[dict], list[str]]:
        new_events = []
        skipped_events = []

        for event in calendar_events:
            title = event.get("title")
            date = event.get("date")

            if self.event_exists(memory, title, date):
                skipped_events.append(title or "")
                continue

            new_events.append(event)
            memory["created_calendar_events"].append({
                "title": title,
                "date": date,
            })

        return new_events, skipped_events
    def email_already_sent(
        self,
        memory: dict,
        name: str,
        meeting_title: str,
        meeting_date: str,
    ) -> bool:
        """
        Returns True only if an email was already sent to `name` for the
        specific meeting identified by (meeting_title + meeting_date).

        This allows multiple meetings on the same day (e.g. "Sprint Planning"
        and "Design Review" both on May 4) to each send their own emails,
        while still preventing duplicates if the same meeting is processed
        more than once.
        """
        expected_name  = self.normalise(name)
        expected_title = self.normalise(meeting_title)
        return any(
            self.normalise(e.get("name")) == expected_name
            and self.normalise(e.get("meeting_title", "")) == expected_title
            and e.get("meeting_date") == meeting_date
            for e in memory.get("sent_emails", [])
        )

    def record_sent_email(
        self,
        memory: dict,
        name: str,
        meeting_title: str,
        meeting_date: str,
    ) -> None:
        memory["sent_emails"].append({
            "name":          name,
            "meeting_title": meeting_title,
            "meeting_date":  meeting_date,
        })

    async def invoke(
        self,
        invoke_context: InvokeContext,
        input_data: Messages,
    ) -> AsyncGenerator[Messages, None]:

        try:
            data = json.loads(input_data[0].content)
        except json.JSONDecodeError:
            yield [Message(role="assistant", content=input_data[0].content)]
            return

        memory = self.load_memory()
        meeting_date = data.get("meeting_date") or str(datetime.utcnow().date())

        self.store_decisions(
            memory,
            data.get("decisions") or [],
            meeting_date,
        )

        self.store_action_items(
            memory,
            data.get("action_items") or [],
            meeting_date,
        )

        filtered_events, skipped_events = self.filter_new_calendar_events(
            memory,
            data.get("calendar_events") or [],
        )

        data["calendar_events"] = filtered_events
        data["skipped_events"] = skipped_events

        self.save_memory(memory)

        yield [Message(role="assistant", content=json.dumps(data))]