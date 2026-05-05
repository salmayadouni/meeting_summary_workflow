# ============================================================
# config.py — Constants shared across the entire project
# ============================================================

import os

# ── File paths ───────────────────────────────────────────────
MEMORY_FILE   = "company_memory.json"
CONTACTS_FILE = "contacts.json"
TOKEN_FILE    = "token.json"
TEMP_FILE     = "temp_pipeline_data.json"

# ── Google OAuth scopes ──────────────────────────────────────
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
]

# ── API keys ─────────────────────────────────────────────────
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")