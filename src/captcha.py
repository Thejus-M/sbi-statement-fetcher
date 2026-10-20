"""Solves the login CAPTCHA with the ddddocr + EasyOCR + Tesseract ensemble
in src/captha.py. The image comes straight from the page as PNG bytes; it is
saved to output/captcha_raw.png because the solver reads from a file.

Debug a saved CAPTCHA:
    uv run python -m src.captha output/captcha_raw.png --debug"""
import re

from . import config
from .captha import N_CHARS, decode


def solve(png: bytes) -> str | None:
    """Return the CAPTCHA text, or None when the solver found no plausible
    answer."""
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    path = config.OUTPUT_DIR / "captcha_raw.png"
    path.write_bytes(png)
    text = decode(str(path))
    if not re.fullmatch(rf"[A-Z0-9]{{{N_CHARS}}}", text):
        print(f"CAPTCHA solver rejected: read {text!r}, need {N_CHARS} letters or digits", flush=True)
        return None
    return text
