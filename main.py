import asyncio
import sys
import uuid
import logging

from loguru import logger

from grafi.common.models.message import Message
from grafi.common.models.invoke_context import InvokeContext
from tools.memory_tool import MemoryTool
from grafi.common.events.topic_events.publish_to_topic_event import PublishToTopicEvent

logging.getLogger("grafi").setLevel(logging.WARNING)
logger.disable("grafi")

from workflow import workflow, input_topic


def read_from_file(path: str) -> str:
    """Reads meeting notes from a .txt or .md file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            print(f" File '{path}' is empty.")
            sys.exit(1)
        print(f"Loaded notes from '{path}' ({len(content)} characters).\n")
        return content
    except FileNotFoundError:
        print(f" File not found: '{path}'")
        sys.exit(1)


async def read_from_input() -> str:
  
    print("Write your meeting notes below.")
    print("  When you're done, press Enter twice to continue.\n")

    lines = []
    loop  = asyncio.get_event_loop()

    while True:
        line = await loop.run_in_executor(None, input, "")
        if line == "" and lines and lines[-1] == "":
            break
        lines.append(line)

    return "\n".join(lines).strip()




async def run_workflow(meeting_notes: str) -> None:
    invoke_context = InvokeContext(
        user_id=uuid.uuid4().hex,
        conversation_id=uuid.uuid4().hex,
        invoke_id=uuid.uuid4().hex,
        assistant_request_id=uuid.uuid4().hex,
    )

    message = Message(role="user", content=meeting_notes)

    input_event = PublishToTopicEvent(
        event_id=uuid.uuid4().hex,
        invoke_context=invoke_context,
        publisher_name="user",
        publisher_type="external",
        topic_name=input_topic.name,
        data=[message],
        consumed_events=[],
    )

    input_event = await input_topic.publish_data(input_event)

    async for result in workflow.invoke(input_event, is_sequential=False):
        for output_message in result.data:
            print("\n" + "=" * 60)
            print("   FINAL SUMMARY")
            print("=" * 60)
            print(output_message.content)
            print()




async def main() -> None:
    print("\n" + "=" * 60)
    print("   MEETING NOTES WORKFLOW ")
    print("=" * 60 + "\n")

    if len(sys.argv) > 1:
        meeting_notes = read_from_file(sys.argv[1])
    else:
        meeting_notes = await read_from_input()
   
    if not meeting_notes:
        print(" No meeting notes provided. Exiting.")
        return

    print(f"Notes ready ({len(meeting_notes)} characters). Starting workflow...\n")
    await run_workflow(meeting_notes)


if __name__ == "__main__":
    asyncio.run(main())