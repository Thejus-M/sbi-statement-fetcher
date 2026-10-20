"""SBI (YONO web) popups: which overlays count as popups and which must stop
the run. Markup confirmed from saved pages:

- Password expiry: dialog.bd-example-modal-lg with "I'll do it later" and
  "Change Password" (only the latter = forced change).
- Generic message box: dff-dialogue dialog.dialogue-container ("Sorry!" +
  message + EXIT), used for wrong password, locked user, wrong CAPTCHA.
- "Are you sure you want to exit?" YES/NO: always answer NO.
- Session timeout alert, feedback, YONO Lite and app prompts on the dashboard.
The OTP dialog (dff-mpin-dialog) and the transaction filter drawers are not
popups. The generic handling lives in guard.handle_popups."""
import re

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .guard import FetchError, StopRule, handle_popups

DIALOGS = ("dialog.dialogue-container, dialog.bd-example-modal-lg, dialog[open], .modal.show, "
           "[role=dialog][aria-modal=true], .cdk-overlay-pane mat-dialog-container, "
           "dff-session-timeout-alert .modal, app-feedback-popup-end-state .modal, "
           "app-rinb-popup .modal, app-yono-lite-dialog .modal")
NOT_POPUPS = "dff-mpin-dialog, app-duration-list, app-sort-list, app-filter-list"
EXIT_DIALOG = "dialog.dialogue-container:has-text('Are you sure you want to exit')"

LOCKED = r"\b(locked|blocked|disabled|suspended)\b"
BAD_CREDENTIALS = (r"invalid (user|login|password|credentials)|incorrect (user|password)|wrong password|"
                   r"username (and|or) password|password (is )?(invalid|incorrect)")
CAPTCHA_WRONG = r"captcha|security code|characters (shown|displayed)"
OTP_WRONG = r"(invalid|incorrect|wrong).{0,20}otp|otp.{0,30}(invalid|incorrect|wrong|does not match)"
OTP_EXPIRED = r"otp.{0,30}expired|expired.{0,20}otp"

STOP_RULES = [
    StopRule(LOCKED, "SBI says the user is locked. Unlock it with \"Lock/Unlock User\" on the SBI site, then fetch again."),
    StopRule(BAD_CREDENTIALS, "SBI rejected the username or password in .env. Stopped after one try so the account doesn't lock."),
    StopRule(r"password.{0,40}(expired|must (be )?change)|change (your )?password", 
             "SBI requires a password change. Change it on the SBI site, update .env, then fetch again.", soft=True),
    StopRule(r"under maintenance|temporarily unavailable|service unavailable|try (again )?later",
             "SBI is temporarily unavailable. Try again later.", exc=FetchError),
    StopRule(r"session (has )?(expired|timed out)|you have been logged out",
             "SBI ended the session. Fetch again.", exc=FetchError),
]
# Session about to expire: keep it. Already logged in elsewhere: usually our
# own earlier run, so continue here.
KEEPALIVE = r"^(continue|extend( session)?|stay( logged)? in|keep me logged in|continue here|terminate.*|logout other.*)$"


def dialog_text(page: Page) -> str:
    """Text of the visible SBI message boxes, without handling them."""
    texts = []
    boxes = page.locator("dialog.dialogue-container")
    for i in range(boxes.count()):
        try:
            if boxes.nth(i).is_visible():
                texts.append(" ".join(boxes.nth(i).inner_text().split()))
        except PlaywrightError:
            continue
    return " ".join(texts)


def dismiss(page: Page) -> list[str]:
    """Close nags and stop for blocking popups (see STOP_RULES)."""
    notes = []
    exit_box = page.locator(EXIT_DIALOG)
    if exit_box.count() and exit_box.first.is_visible():
        exit_box.first.get_by_role("button", name=re.compile(r"^\s*no\s*$", re.I)).click(timeout=5000)
        notes.append("exit prompt answered NO")
    return notes + handle_popups(page, DIALOGS, STOP_RULES, keepalive_re=KEEPALIVE, ignore_selector=NOT_POPUPS)
