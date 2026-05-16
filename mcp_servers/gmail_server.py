"""
mcp_servers/gmail_server.py
Gmail MCP Server — exposes send_email tool via MCP protocol.

Run with:
    uv run python mcp_servers/gmail_server.py
"""

import base64
import os
import sys
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
async def send_email(to_address: str, subject: str, body: str) -> dict:
    """
    Send a plain-text email via the authenticated Gmail account.

    Args:
        to_address: Recipient email address.
        subject:    Email subject line.
        body:       Plain-text email body.

    Returns a dict with message_id, status, and recipient.
    """
    service = _service()

    message = MIMEMultipart()
    message["to"]      = to_address
    message["subject"] = subject
    message.attach(MIMEText(body, "plain"))

    raw    = base64.urlsafe_b64encode(message.as_bytes()).decode()
    result = service.users().messages().send(
        userId="me",
        body={"raw": raw},
    ).execute()

    return {
        "message_id": result.get("id"),
        "status":     "sent",
        "to":         to_address,
    }


if __name__ == "__main__":
    print("Starting Gmail MCP Server on http://0.0.0.0:8083/mcp")
    mcp.settings.host = "0.0.0.0"
    mcp.settings.port = 8083
    mcp.settings.streamable_http_path = "/mcp"
    mcp.run(transport="streamable-http")
