from grafi.nodes.node import Node
from grafi.tools.llms.impl.openai_tool import OpenAITool
from grafi.topics.topic_impl.input_topic import InputTopic
from grafi.topics.topic_impl.output_topic import OutputTopic
from grafi.topics.topic_impl.topic import Topic
from grafi.workflows.impl.event_driven_workflow import EventDrivenWorkflow

from config import OPENAI_API_KEY
from prompts import EXTRACTOR_PROMPT, FORMATTER_PROMPT
from tools import MemoryTool, GoogleCalendarTool, EmailComposerTool, EmailSenderTool




input_topic     = InputTopic(name="input_topic")
extracted_topic = Topic(name="extracted_topic")
memory_topic    = Topic(name="memory_topic")
calendar_topic  = Topic(name="calendar_topic")
composed_topic  = Topic(name="composed_topic")
email_topic     = Topic(name="email_topic")
output_topic    = OutputTopic(name="output_topic")



extractor_node = (
    Node.builder()
    .name("ExtractorNode")
    .subscribe(input_topic)
    .tool(
        OpenAITool.builder()
        .name("ExtractorLLM")
        .api_key(OPENAI_API_KEY)
        .model("gpt-4o")
        .system_message(EXTRACTOR_PROMPT)
        .build()
    )
    .publish_to(extracted_topic)
    .build()
)

memory_node = (
    Node.builder()
    .name("MemoryNode")
    .subscribe(extracted_topic)
    .tool(MemoryTool(name="MemoryTool"))
    .publish_to(memory_topic)
    .build()
)

calendar_node = (
    Node.builder()
    .name("CalendarNode")
    .subscribe(memory_topic)
    .tool(GoogleCalendarTool(name="GoogleCalendarTool"))
    .publish_to(calendar_topic)
    .build()
)

email_composer_node = (
    Node.builder()
    .name("EmailComposerNode")
    .subscribe(calendar_topic)
    .tool(EmailComposerTool(name="EmailComposerTool"))
    .publish_to(composed_topic)
    .build()
)

email_sender_node = (
    Node.builder()
    .name("EmailSenderNode")
    .subscribe(composed_topic)
    .tool(EmailSenderTool(name="EmailSenderTool"))
    .publish_to(email_topic)
    .build()
)

formatter_node = (
    Node.builder()
    .name("FormatterNode")
    .subscribe(email_topic)
    .tool(
        OpenAITool.builder()
        .name("FormatterLLM")
        .api_key(OPENAI_API_KEY)
        .model("gpt-4o")
        .system_message(FORMATTER_PROMPT)
        .build()
    )
    .publish_to(output_topic)
    .build()
)



workflow = (
    EventDrivenWorkflow.builder()
    .name("MeetingNotesWorkflow")
    .node(extractor_node)
    .node(memory_node)
    .node(calendar_node)
    .node(email_composer_node)
    .node(email_sender_node)
    .node(formatter_node)
    .build()
)