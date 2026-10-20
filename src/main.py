"""Entry point: uv run python -m src.main

Logs in (password, CAPTCHA, then the OTP), opens the savings account's
Transactions tab, reads the table page by page (newest first), checks every
row against the running balance, and writes the output the Transactions
widget reads (sbi_transactions.json, sbi_account_balances.json,
sbi-fetch-status.json). It stops paging once it has seen a run of
transactions an earlier run already saved. The status file always ends as
done, error or cancelled (see guard.run_main)."""
import sys
from contextlib import suppress

from playwright.sync_api import sync_playwright

from . import browser, config, login, popups, record, transactions
from .guard import ActionNeeded, install_page_guards, run_main

LOGOUT_BUTTON = "button.logout"


def run() -> str:
    if not config.SBI_USERNAME or not config.SBI_PASSWORD:
        raise ActionNeeded("Set SBI_USERNAME and SBI_PASSWORD in the project's .env file.")
    config.SESSION_FILE.unlink(missing_ok=True)  # left over from older versions
    record.write_status("starting", "Opening SBI…")
    with sync_playwright() as p:
        b = browser.launch(p, record.write_status)
        context, page = browser.new_page(b)
        install_page_guards(context, page)
        logged_in = False
        try:
            login.open_login_page(page)
            record.write_status("logging-in", "Logging in to SBI…")
            login.login(page)
            logged_in = True
            page.locator(login.DASHBOARD_READY).wait_for(state="visible", timeout=45000)

            record.write_status("reading-transactions", "Reading SBI transactions…")
            transactions.open_transactions(page)
            last4 = transactions.account_last4(page)
            transactions.show_max_rows(page)

            known = record.known_ids(last4)
            rows = transactions.collect_all(
                page, is_known=lambda t: record.to_widget_row(t, last4)["id"] in known)
            transactions.reconcile(rows)
            # Newest first, so the top row carries the current balance.
            record.write_balance(last4, rows[0].balance if rows else None)
            added, total = record.write_transactions([record.to_widget_row(t, last4) for t in rows], rows, last4)
            if sys.stdout.isatty():  # the widget log gets counts only
                transactions.print_table([t for t in rows if record.to_widget_row(t, last4)["id"] not in known])
            return f"SBI: {added} new transactions, {total} saved.", added, total
        finally:
            if logged_in:
                with suppress(Exception):
                    page.locator(LOGOUT_BUTTON).first.click(timeout=5000)
            with suppress(Exception):
                context.close()
            with suppress(Exception):
                b.close()


if __name__ == "__main__":
    run_main(run, record.write_status, "SBI", config.OUTPUT_DIR)
