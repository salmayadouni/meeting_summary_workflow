import json
import logging
import os
from typing import AsyncGenerator

import openai
from openinference.semconv.trace import OpenInferenceSpanKindValues

from grafi.common.models.invoke_context import InvokeContext
from grafi.common.models.message import Message, Messages
from grafi.tools.tool import Tool

from config import OPENAI_API_KEY


logger = logging.getLogger(__name__)


class EmailComposerTool(Tool):
    oi_span_type: OpenInferenceSpanKindValues = OpenInferenceSpanKindValues.TOOL

    model: str = "gpt-4o"
    default_subject: str = "Meeting Summary"

    def openai_client(self):
        return openai.OpenAI(api_key=OPENAI_API_KEY)

    def normalise(self, value: str | None) -> str:
        return (value or "").strip().lower()

    def parse_pipeline_data(self, input_data: Messages) -> tuple[dict, str | None]:
        """
        Parse extracted_data and next_meeting_link directly from the message
        passed by CalendarNode — avoids reading stale data from TEMP_FILE.
        """
        try:
            raw = input_data[0].content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            data = json.loads(raw)
        except (json.JSONDecodeError, IndexError):
            return {}, None

        extracted_data = data.get("extracted_data") or {}
        if isinstance(extracted_data, str):
            try:
                extracted_data = json.loads(extracted_data)
            except json.JSONDecodeError:
                extracted_data = {}

        return extracted_data, data.get("next_meeting_link")

    def tasks_for_person(self, name: str, action_items: list[dict]) -> list[dict]:
        person = self.normalise(name)

        return [
            item for item in action_items
            if self.normalise(item.get("owner")) == person
        ]

    def format_decisions(self, decisions: list[dict]) -> str:
        if not decisions:
            return "No major decisions."

        lines = []

        for decision in decisions:
            what = decision.get("what") or "Decision not specified"
            why = decision.get("why")

            if why:
                lines.append(f"- {what} (reason: {why})")
            else:
                lines.append(f"- {what}")

        return "\n".join(lines)

    def format_tasks(self, name: str, action_items: list[dict]) -> str:
        tasks = self.tasks_for_person(name, action_items)

        if not tasks:
            return "No specific tasks assigned to you from this meeting."

        lines = []

        for task in tasks:
            task_name = task.get("task") or "Task not specified"
            deadline = task.get("deadline")

            if deadline:
                lines.append(f"- {task_name} by {deadline}")
            else:
                lines.append(f"- {task_name}")

        return "\n".join(lines)

    def format_next_meeting(self, next_meeting: dict | None, meeting_link: str | None) -> str:
        if not next_meeting:
            return ""

        title = next_meeting.get("title") or "Next meeting"
        date = next_meeting.get("date") or "TBD"
        time = next_meeting.get("time") or "TBD"
        location = next_meeting.get("location") or "TBD"

        text = f"Next meeting: {title} on {date} at {time} at {location}."

        if meeting_link:
            text += f"\nJoin here: {meeting_link}"

        return text

    def email_opening(self, name: str, recipient_type: str) -> str:
        if recipient_type == "absent":
            return (
                f"Hi {name}, you missed today's meeting — "
                "here's what happened and what you need to do."
            )

        return f"Hi {name}, here's a quick summary of today's meeting."

    def build_email_prompt(
        self,
        name: str,
        recipient_type: str,
        extracted_data: dict,
        next_meeting_link: str | None,
    ) -> str:
        decisions = extracted_data.get("decisions") or []
        action_items = extracted_data.get("action_items") or []
        next_meeting = extracted_data.get("next_meeting")

        opening = self.email_opening(name, recipient_type)
        decisions_text = self.format_decisions(decisions)
        tasks_text = self.format_tasks(name, action_items)
        next_meeting_text = self.format_next_meeting(
            next_meeting,
            next_meeting_link,
        )

        return f"""
Write a short professional email body in plain text.

Use this opening line exactly:
{opening}

Decisions made:
{decisions_text}

Tasks for {name}:
{tasks_text}

{next_meeting_text}

Keep it warm and professional.
Use short paragraphs.
Do not use markdown.
Do not use bullet points in the final email.
Keep it under 150 words.
End with: Best regards, Binome 
""".strip()

    def write_email(
        self,
        name: str,
        recipient_type: str,
        extracted_data: dict,
        next_meeting_link: str | None,
    ) -> str:
        client = self.openai_client()
        prompt = self.build_email_prompt(
            name,
            recipient_type,
            extracted_data,
            next_meeting_link,
        )

        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
        )

        return response.choices[0].message.content.strip()

    def email_subject(self, extracted_data: dict) -> str:
        meeting_title = extracted_data.get("meeting_title") or "Meeting"
        meeting_date = extracted_data.get("meeting_date") or ""

        if meeting_date:
            return f"Meeting Summary — {meeting_title} — {meeting_date}"

        return f"Meeting Summary — {meeting_title}"

    def compose_for_people(
        self,
        names: list[str],
        recipient_type: str,
        extracted_data: dict,
        next_meeting_link: str | None,
    ) -> list[dict]:
        emails = []

        for name in names:
            try:
                body = self.write_email(
                    name,
                    recipient_type,
                    extracted_data,
                    next_meeting_link,
                )

                emails.append({
                    "recipient_name": name,
                    "recipient_type": recipient_type,
                    "body": body,
                })

            except Exception as exc:
                logger.exception("Failed to compose email")

                emails.append({
                    "recipient_name": name,
                    "recipient_type": recipient_type,
                    "body": "",
                    "status": "failed",
                    "error": str(exc),
                })

        return emails

    async def invoke(
        self,
        invoke_context: InvokeContext,
        input_data: Messages,
    ) -> AsyncGenerator[Messages, None]:

        extracted_data, next_meeting_link = self.parse_pipeline_data(input_data)

        if not extracted_data:
            response = {
                "subject": self.default_subject,
                "emails": [],
            }

            yield [Message(role="assistant", content=json.dumps(response))]
            return

        attendees = extracted_data.get("attendees") or []
        absent_people = extracted_data.get("absent_people") or []

        emails = []
        emails.extend(
            self.compose_for_people(
                attendees,
                "attendee",
                extracted_data,
                next_meeting_link,
            )
        )
        emails.extend(
            self.compose_for_people(
                absent_people,
                "absent",
                extracted_data,
                next_meeting_link,
            )
        )

        response = {
            "subject": self.email_subject(extracted_data),
            "emails": emails,
        }

        yield [Message(role="assistant", content=json.dumps(response))]