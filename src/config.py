"""Settings. Credentials are read from the environment (loaded from .env) and
are never printed or written anywhere by this project."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
SESSION_FILE = OUTPUT_DIR / "session.json"
load_dotenv(ROOT / ".env")

# The old retail.onlinesbi.sbi login now redirects here.
LOGIN_URL = os.environ.get("SBI_LOGIN_URL", "https://yonoretail.sbi.bank.in/registration/welcome")
SBI_USERNAME = os.environ.get("SBI_USERNAME", "")
SBI_PASSWORD = os.environ.get("SBI_PASSWORD", "")
