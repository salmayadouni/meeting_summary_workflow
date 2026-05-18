"""
mcp_servers/gmail_server.py
Gmail MCP Server — exposes send_email tool via MCP protocol.

Run with:
    uv run python mcp_servers/gmail_server.py
"""

import base64
import os
import sys
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from auth import get_google_credentials      # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

mcp = FastMCP("Gmail MCP Server")


def _service():
    return build("gmail", "v1", credentials=get_google_credentials())


@mcp.tool()
async def send_email(
    to_address: str,
    subject: str,
    body: str,
    ics_content: str = "",
) -> dict:
    """
    Send an email via Gmail. If ics_content is provided it is attached as
    a .ics calendar invite so the recipient can add the event with one click.

    Args:
        to_address:  Recipient email address.
        subject:     Email subject line.
        body:        Plain-text email body.
        ics_content: Optional iCalendar (.ics) file content to attach.
    """
    service = _service()

    message = MIMEMultipart("mixed")
    message["to"]      = to_address
    message["subject"] = subject
    message.attach(MIMEText(body, "plain"))

    # Attach the .ics calendar invite if provided
    if ics_content.strip():
        ics_part = MIMEText(ics_content, "calendar", "utf-8")
        ics_part.add_header("Content-Disposition", 'attachment; filename="invite.ics"')
        ics_part.replace_header("Content-Type", 'text/calendar; method=REQUEST; charset="utf-8"')
        message.attach(ics_part)

    raw    = base64.urlsafe_b64encode(message.as_bytes()).decode()
    result = service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    return {
        "message_id":    result.get("id"),
        "status":        "sent",
        "to":            to_address,
        "has_ics":       bool(ics_content.strip()),
    }


if __name__ == "__main__":
    print("Starting Gmail MCP Server on http://0.0.0.0:8083/mcp")
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8083
    mcp.settings.streamable_http_path = "/mcp"
    mcp.run(transport="streamable-http")