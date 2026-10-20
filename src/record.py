"""Saves SBI data into the shared transactions database (see src/store.py
and omarchy-transactions/txdb), and writes the progress file the widget
shows while a fetch runs."""
import hashlib
import re
from datetime import datetime

from . import config
from . import store
from .guard import atomic_write_json
from .transactions import Txn

STATUS_FILE = "sbi-fetch-status.json"


def to_widget_row(t: Txn, last4: str) -> dict:
    iso = datetime.strptime(t.date, "%d/%m/%Y").strftime("%Y-%m-%d")
    # The running balance makes two identical transfers on one day distinct.
    key = f"{iso}|{t.description}|{t.amount:.2f}|{t.balance:.2f}|{last4}"
    return {
        "id": "sbi-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16],
        "date": iso,
        "merchant": t.description,
        "amount": t.amount,
        "currency": "INR",
        "bank": "SBI",
        "accountType": "checking",
        "last4": last4,
        "source": "bank-scrape",
        "rawStatementRowId": t.raw_amount,
        "tracked": False,
    }


def _account(last4: str) -> str:
    return store.ingest.ensure_account(store.conn(), "SBI", "checking", last4)


def known_ids(last4: str) -> set[str]:
    return store.ingest.known_ids(store.conn(), _account(last4))


def write_transactions(rows: list[dict], txns: list[Txn], last4: str) -> tuple[int, int]:
    """Saves the rows just read (newest first, as on the page), with SBI's
    own fields in the per-bank table. Returns (added, total saved)."""
    n = len(rows)
    prepared = []
    for k, (row, t) in enumerate(zip(rows, txns)):
        upi = re.search(r"/(?:DR|CR)/(\d{9,})", t.description)
        prepared.append({**row, "balance": t.balance, "seq": n - k, "raw": {
            "date_text": t.date, "description": t.description, "amount_text": t.raw_amount,
            "balance_text": f"{t.balance:.2f}", "sign_source": "icon" if t.sign_certain else "balance",
            "upi_ref": upi.group(1) if upi else None}})
    added = store.ingest.upsert_rows(store.conn(), _account(last4), prepared)
    return added, store.ingest.count(store.conn(), "SBI")


def write_balance(last4: str, balance: float | None) -> None:
    """Leaves the saved balance alone when the new one couldn't be read."""
    store.ingest.set_balance(store.conn(), _account(last4), balance)


def write_status(stage: str, message: str = "", digits: int | None = None) -> None:
    status = {"stage": stage, "message": message, "qr": "", "card": "", "step": "",
              "updated": datetime.now().isoformat(timespec="milliseconds")}
    if digits is not None:
        status["digits"] = digits
    atomic_write_json(config.OUTPUT_DIR / STATUS_FILE, status, mode=0o644)
