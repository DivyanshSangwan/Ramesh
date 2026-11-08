"""Load local settings without overriding environment variables supplied by hosting."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve project root directory and load .env file without overriding host environment variables.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)


# Read Gemini API key from environment variables.
def gemini_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
