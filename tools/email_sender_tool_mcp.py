import asyncio
import json
import logging
import os
from typing import AsyncGenerator

import openai
from openinference.semconv.trace import OpenInferenceSpanKindValues

from grafi.common.models.invoke_context import InvokeContext
from grafi.common.models.message import Message, Messages
from grafi.tools.tool import Tool

from mcp_client import call_mcp
from config import CONTACTS_FILE, OPENAI_API_KEY, TEMP_FILE
from tools.memory_tool import MemoryTool


logger = logging.getLogger(__name__)


class EmailSenderTool(Tool):
    oi_span_type: OpenInferenceSpanKindValues = OpenInferenceSpanKindValues.TOOL

    model: str = "gpt-4o"
    default_subject: str = "Meeting Summary"

    gmail_mcp_url: str = "http://localhost:8083/mcp"

    def openai_client(self):
        return openai.OpenAI(api_key=OPENAI_API_KEY)

    def load_contacts(self) -> dict:
        if not os.path.exists(CONTACTS_FILE):
            logger.warning("Contacts file not found: %s", CONTACTS_FILE)
            return {}

        with open(CONTACTS_FILE, "r", encoding="utf-8") as file:
            return json.load(file)

    def load_pipeline_data(self) -> dict:
        if not os.path.exists(TEMP_FILE):
            return {}

        with open(TEMP_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)

        extracted_data = data.get("extracted_data")

        if isinstance(extracted_data, str):
            try:
                data["extracted_data"] = json.loads(extracted_data)
            except json.JSONDecodeError:
                data["extracted_data"] = {}

        return data

    def parse_input(self, input_data: Messages) -> dict:
        raw = input_data[0].content.strip()

        if raw.startswith("```json"):
            raw = raw.removeprefix("```json").removesuffix("```").strip()
        elif raw.startswith("```"):
            raw = raw.removeprefix("```").removesuffix("```").strip()

        return json.loads(raw)

    async def send_email(self, to_address: str, subject: str, body: str) -> None:
        """Send email via the Gmail MCP server."""
        await call_mcp(
            self.gmail_mcp_url,
            "send_email",
            {"to_address": to_address, "subject": subject, "body": body},
        )

    # ── Terminal fallback ────────────────────────────────────────────
    async def ask_user_terminal(self, prompt: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, input, prompt)

    # ── UI-aware confirmation ────────────────────────────────────────
    async def ask_confirmation(
        self,
        name: str,
        recipient_type: str,
        subject: str,
        current_body: str,
        session_id: str | None,
    ) -> tuple[str, str]:
        """
        Returns (choice, feedback) where choice is 'Y', 'N', or 'C'.
        - If a UI session exists, streams the email preview to the browser
          and waits for the user to click Send / Revise / Cancel.
        - Otherwise falls back to the terminal Y/N/C prompt.
        """
        session = None
        if session_id:
            try:
                from ui_context import sessions
                session = sessions.get(session_id)
            except ImportError:
                pass

        if session is not None:
            await session.sse_queue.put({
                "type": "confirm_email",
                "name": name,
                "recipient_type": recipient_type,
                "subject": subject,
                "body": current_body,
            })
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            session.confirm_future = future
            response = await future
            return response.get("answer", "C").strip().upper(), response.get("feedback", "").strip()
        else:
            self.print_email_preview(name, recipient_type, subject, current_body)
            choice = (await self.ask_user_terminal("\nSend, revise, or cancel? [Y/N/C]: ")).strip().upper()
            if choice == "N":
                feedback = await self.ask_user_terminal("What should change? ")
                return "N", feedback
            return choice, ""

    def revise_email(
        self,
        original_body: str,
        feedback: str,
        name: str,
        recipient_type: str,
    ) -> str:
        prompt = f"""
You wrote this email for {name} ({recipient_type}):

{original_body}

The user gave this feedback:
{feedback}

Rewrite the email while keeping the same purpose.
Use plain text only.
Do not use markdown.
Do not use bullet points.
Keep it under 150 words.
End with: Best regards, Binome

Return only the email body.
""".strip()

        response = self.openai_client().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "user", "content": prompt},
            ],
            max_tokens=400,
        )

        return response.choices[0].message.content.strip()

    def print_email_preview(
        self,
        name: str,
        recipient_type: str,
        subject: str,
        body: str,
    ) -> None:
        separator = "-" * 60

        print()
        print(separator)
        print(f"Email preview for {name} ({recipient_type})")
        print(separator)
        print(f"To: {name}")
        print(f"Subject: {subject}")
        print(separator)
        print(body)
        print(separator)

    async def review_email(
        self,
        name: str,
        address: str,
        recipient_type: str,
        subject: str,
        body: str,
        session_id: str | None = None,
    ) -> dict:
        current_body = body

        while True:
            choice, feedback = await self.ask_confirmation(
                name, recipient_type, subject, current_body, session_id
            )

            if choice == "Y":
                await self.send_email(address, subject, current_body)
                return {"name": name, "email": address, "type": recipient_type, "status": "sent"}

            if choice == "N":
                if not feedback:
                    continue
                current_body = self.revise_email(current_body, feedback, name, recipient_type)
                continue

            if choice == "C":
                return {"name": name, "email": address, "status": "cancelled_by_user"}

    async def process_email(
        self,
        contacts: dict,
        subject: str,
        email_data: dict,
        memory_tool: MemoryTool,
        memory: dict,
        meeting_date: str,
        meeting_title: str = "",
        extracted_data: dict | None = None,
        session_id: str | None = None,
    ) -> tuple[str, dict]:
        name = email_data.get("recipient_name") or ""
        body = email_data.get("body") or ""
        recipient_type = email_data.get("recipient_type") or "attendee"
        address = contacts.get(name)

        if memory_tool.email_already_sent(memory, name, meeting_title, meeting_date, current_data=extracted_data):
            return "skipped", {
                "name": name,
                "status": "already_sent",
            }

        if not address:
            logger.warning("Contact not found for recipient: %s", name)

            return "failed", {
                "name": name,
                "reason": "not in contacts file",
            }

        try:
            result = await self.review_email(
                name,
                address,
                recipient_type,
                subject,
                body,
                session_id=session_id,
            )

            if result.get("status") == "sent":
                memory_tool.record_sent_email(memory, name, meeting_title, meeting_date, current_data=extracted_data)
                memory_tool.save_memory(memory)

                return "sent", result

            return "failed", result

        except Exception as exc:
            logger.exception("Could not send email to %s", name)

            return "failed", {
                "name": name,
                "email": address,
                "status": "failed",
                "error": str(exc),
            }

    def build_response(
        self,
        pipeline_data: dict,
        sent: list[dict],
        failed: list[dict],
        skipped: list[dict],
    ) -> dict:
        return {
            "extracted_data": pipeline_data.get("extracted_data", {}),
            "calendar_result": pipeline_data.get("calendar_result", {}),
            "next_meeting_link": pipeline_data.get("next_meeting_link"),
            "email_result": {
                "success": True,
                "sent": sent,
                "failed": failed,
                "skipped": skipped,
            },
        }

    async def invoke(
        self,
        invoke_context: InvokeContext,
        input_data: Messages,
    ) -> AsyncGenerator[Messages, None]:

        try:
            data = self.parse_input(input_data)
        except json.JSONDecodeError:
            response = {
                "email_result": {
                    "success": False,
                    "sent": [],
                    "failed": [],
                    "skipped": [],
                    "error": "Invalid JSON",
                }
            }

            yield [Message(role="assistant", content=json.dumps(response))]
            return

        pipeline_data = self.load_pipeline_data()
        extracted_data = pipeline_data.get("extracted_data") or {}
        meeting_date  = extracted_data.get("meeting_date")  or ""
        meeting_title = extracted_data.get("meeting_title") or ""

        # Use conversation_id as the UI session key (for email confirmation modal)
        session_id: str | None = getattr(invoke_context, "conversation_id", None)

        memory_tool = MemoryTool(name="MemoryToolRef")
        memory = memory_tool.load_memory()

        contacts = self.load_contacts()

        subject = data.get("subject") or self.default_subject
        emails = data.get("emails") or []

        sent = []
        failed = []
        skipped = []

        for email_data in emails:
            status, result = await self.process_email(
                contacts,
                subject,
                email_data,
                memory_tool,
                memory,
                meeting_date,
                meeting_title,
                extracted_data=extracted_data,
                session_id=session_id,
            )

            if status == "sent":
                sent.append(result)
            elif status == "skipped":
                skipped.append(result)
            else:
                failed.append(result)

        response = self.build_response(
            pipeline_data,
            sent,
            failed,
            skipped,
        )

        yield [Message(role="assistant", content=json.dumps(response))]