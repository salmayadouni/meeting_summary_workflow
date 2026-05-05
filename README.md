# Meeting Notes Workflow

An agentic pipeline that turns raw meeting notes into structured actions — automatically.

Paste your notes, and the workflow extracts decisions, creates calendar events with Google Meet links, writes personalised emails for every attendee and absent person, and delivers a summary. Every email is reviewed by a human before it is sent.

---

## Pipeline Overview

```
Raw meeting notes (input)
       │
       ▼
┌─────────────────┐
│  ExtractorNode  │  LLM (GPT-4o) — parses notes into structured JSON
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   MemoryNode    │  Tool — saves decisions & tasks, avoid to. have deduplicated events
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  CalendarNode   │  Tool — creates Google Calendar events
│                 │         generates a real Meet link for Zoom events
│                 │         deletes cancelled events by exact title
└────────┬────────┘
         │
         ▼
┌──────────────────────┐
│  EmailComposerNode   │  Tool — writes one personalised email per person
│                      │         (attendees + absent people)
│                      │         injects Meet link if available
└──────────┬───────────┘
           │
           ▼
┌──────────────────────┐
│  EmailSenderNode     │  Tool — shows each email as a preview
│                      │         Y → send  |  N → revise  |  C → cancel
└──────────┬───────────┘
           │
           ▼
┌─────────────────┐
│  FormatterNode  │  LLM (GPT-4o) — writes final summary to show
└────────┬────────┘
         │
         ▼
    summary (ouptut)
```

---

## Project Structure

```
meeting_workflow/
│
├── main.py                      # Entry point — reads notes, runs the workflow, prints summary
├── workflow.py                  # Assembles all nodes, topics, and the EventDrivenWorkflow
├── config.py                    # All constants: file paths, Google scopes, API key
├── auth.py                      # Shared Google OAuth helper (Calendar + Gmail)
├── prompts.py                   # System prompts for ExtractorNode and FormatterNode
│
└── tools/
    ├── __init__.py              # Package exports (clean imports in workflow.py)
    ├── memory_tool.py           # Persists decisions & tasks, deduplicates calendar events
    ├── calendar_tool.py         # Google Calendar: create events, Meet links, delete cancelled
    ├── email_composer_tool.py   # Writes one email per person via OpenAI
    └── email_sender_tool.py     # Human-in-the-loop review + Gmail API send
```



## Data Flow

The six nodes communicate through **Graphite topics** (event-driven message buses).  
Each node subscribes to one topic and publishes to the next.

```
input_topic → extracted_topic → memory_topic → calendar_topic → composed_topic → email_topic → output_topic
```

One additional file bridges nodes that are not adjacent in the pipeline:

**`temp_pipeline_data.json`** — written by `CalendarNode`, read by both `EmailComposerNode` and `EmailSenderNode`. It carries the full extracted data and the Google Meet link so those nodes don't have to re-parse upstream messages.

---


## How It Works — Key Behaviours

**Zoom → Google Meet**  
If a calendar event's location is exactly `"Zoom"`, the Calendar API automatically generates a real Google Meet link. That link is stored in `temp_pipeline_data.json` and injected into every email and the final summary.

**Memory & deduplication**  
`MemoryNode` reads `company_memory.json` before creating any event. If an event with the same title and date already exists in memory, it is skipped. This means running the workflow twice on the same notes will not create duplicate calendar events.

**Human-in-the-loop emails**  
Before each email is sent, the full preview is printed to the terminal:
- **Y** — send immediately via Gmail API
- **N** — provide feedback in plain text, OpenAI rewrites the email, preview shown again
- **C** — skip this recipient entirely

**Absent people**  
Anyone mentioned in the notes but not listed as present is treated as absent. They receive a slightly different email: "you missed today's meeting — here's what happened and what you need to do."

**Next meeting**  
If a next meeting is mentioned in the notes, it is automatically added to the calendar events list (if not already there) and its Meet link (if Zoom) is included in every email and the final summary.

---



## Example Input

```
Binome team meeting — May 4, 2026

We decided to split the workflow presentation into two parts because we have limited time during the demo.
Reda was absent but he will finish the pipeline documentation by May 6. Marwa too was absent but she needs to update the README with the final structure by May 9.Maryem will prepare the slides for the demo by May 7.Imane needs to review the eval results and send her feedback by May 8.

We also decided to cancel the Weekly Check-in scheduled for this week since we are all focused on the final deliverable.

Next team sync: May 11 at 10:00 on Zoom.
```

## Example Output 

```
Meeting Summary: Binome Team Meeting on May 4, 2026

Attendees: Maryem, Reda
Absent: Imane, Marwa

Key Decisions:
- The workflow presentation will be divided into two segments due to time constraints during the demo.

Action Items:
- Reda: Complete the pipeline documentation by May 6.
- Maryem: Prepare slides for the demo by May 7.
- Imane: Review evaluation results and send feedback by May 8.
- Marwa: Update the README with the final structure by May 9.

Calendar Updates:
- Event Created: Team Sync on May 11, 2026, at 10:00 AM via Zoom. Meet Link: https://meet.google.com/sjx-pnhr-jbc
- Cancelled Event: Weekly Check-in for this week.

Emails Sent: None

Next Meeting: May 11, 2026, at 10:00 AM on Zoom. Meet link: https://meet.google.com/sjx-pnhr-jbc


```