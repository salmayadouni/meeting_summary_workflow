"""
ui_context.py — Shared session registry between app.py and the tools.

When running via the web UI, each /run request creates a UISession.
The EmailSenderTool looks up the session by conversation_id (from InvokeContext)
to stream email previews to the browser and await the user's Y/N/C choice.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class UISession:
    session_id: str
    sse_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    confirm_future: Optional[asyncio.Future] = None


# Global registry:  conversation_id  →  UISession
# Populated by app.py before invoking the workflow,
# cleaned up after the SSE stream closes.
sessions: dict[str, UISession] = {}