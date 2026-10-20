"""YONO SBI web login: username and password from .env, the CAPTCHA read by
OCR (src/captcha.py; typed at the terminal after a few misses when there is
one), then the OTP typed at the terminal or sent on stdin by the widget.
Selectors confirmed from saved pages (output/debug_*.html).

Safety rules:
- Only a wrong CAPTCHA is retried. A rejected password or a locked user
  stops at once, so the account is never locked by retries.
- At most 3 wrong OTPs; an OTP nobody enters within 5 minutes ends the run;
  the widget can send "resend" for a new OTP.
- The password is only typed into an SBI host.
- Login counts as done only once the dashboard's Accounts menu is visible.

Usage (login only, for testing): .venv/bin/python -m src.login"""
import base64
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from . import config, popups, record
from .captcha import solve as solve_captcha
from .guard import ActionNeeded, FetchError, drain_stdin, fail, first_line, read_line

# Angular Material page: the mat-input-N ids are not stable, so match on labels.
LOGIN_TAB = 'button:has-text("Username / Password")'
USERNAME_INPUT = "input[aria-label='UserName']"
PASSWORD_INPUT = "mat-form-field:has-text('Password') input"
CAPTCHA_INPUT = "input[aria-label='enterCaptchaLabel']"
CAPTCHA_IMAGE = "img[src^='data:image']"
CAPTCHA_REFRESH = "app-captcha-input button.iconbtn:has(img[alt='refresh'])"
LOGIN_BUTTON = 'button:text-is("Login")'
INLINE_ERRORS = "mat-error:visible, simple-snack-bar:visible, .mat-mdc-snack-bar-label:visible, [role='alert']:visible"
# OTP modal: six one-digit boxes, "Proceed" (disabled until full), a
# "Resend OTP" link (class "disabled" while its timer runs). The modal's
# greeting contains the user's name, so only the subheading is shown.
OTP_DIALOG = "dff-mpin-dialog .otp-mpin-modal-container"
OTP_HEADER = "dff-mpin-dialog #dialogue_heading, dff-mpin-dialog .mpinSubHeader"
OTP_INPUTS = "dff-mpin-dialog dff-mpin input.input-value-otp"
OTP_SUBMIT_BUTTON = "dff-mpin-dialog button.otp-submit-btn"
OTP_RESEND = "dff-mpin-dialog .d-inline:has-text('Resend OTP'):not(.disabled)"
DASHBOARD_READY = "a.menu-header-text[aria-label='Accounts']"
SBI_HOSTS = (".sbi.bank.in", ".onlinesbi.sbi", ".sbi")

MAX_AUTO_CAPTCHA_ATTEMPTS = 3
MAX_AUTO_CAPTCHA_ATTEMPTS_NO_TTY = 8
MAX_SUBMITS = 10
MAX_WRONG_OTPS = 3
MAX_RESENDS = 2
OTP_WAIT_S = 300


def _visible(page: Page, selector: str) -> bool:
    loc = page.locator(selector)
    return loc.count() > 0 and loc.first.is_visible()


def _check_host(page: Page) -> None:
    host = urlparse(page.url).hostname or ""
    if not host.endswith(SBI_HOSTS):
        fail(page, config.OUTPUT_DIR, "login-host", f"The login page is on an unexpected site ({host}); stopped before typing the password")


def open_login_page(page: Page) -> None:
    """Opens the login page (retrying network failures twice) and shows the
    username/password form."""
    pauses = (10, 30)
    for attempt in range(len(pauses) + 1):
        last = attempt == len(pauses)
        try:
            response = page.goto(config.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightError as exc:
            if last:
                raise FetchError(f"Can't reach SBI ({first_line(exc, 80)}).")
            time.sleep(pauses[attempt])
            continue
        if response is not None and response.status >= 500:
            raise FetchError(f"SBI returned HTTP {response.status}. Try again later.")
        try:
            page.locator(f"{USERNAME_INPUT}, {LOGIN_TAB}").first.wait_for(state="visible", timeout=60000)
            break
        except PlaywrightTimeoutError:
            if re.search(r"maintenance|temporarily unavailable", page.locator("body").inner_text(), re.I):
                raise FetchError("SBI is under maintenance. Try again later.")
            if last:
                fail(page, config.OUTPUT_DIR, "login-page", "The SBI login page didn't load")
            time.sleep(pauses[attempt])
    popups.dismiss(page)
    if not _visible(page, USERNAME_INPUT):
        page.locator(LOGIN_TAB).first.click()
        page.locator(USERNAME_INPUT).wait_for(state="visible", timeout=20000)
    _check_host(page)


def login(page: Page) -> None:
    _enter_password_and_captcha(page)
    _enter_otp(page)
    record.write_status("logging-in", "Logged in. Opening your account…")
    popups.dismiss(page)  # offers, feedback, app prompts after login


def _captcha_png(page: Page, previous_src: str | None) -> tuple[bytes, str]:
    """The current CAPTCHA image, waiting for a new one after a failed try
    (refreshing it if it doesn't change on its own)."""
    image = page.locator(CAPTCHA_IMAGE).first
    image.wait_for(state="visible")
    if previous_src is not None:
        for refresh in (False, True):
            if refresh and _visible(page, CAPTCHA_REFRESH):
                page.locator(CAPTCHA_REFRESH).first.click()
            try:
                page.wait_for_function("([el, before]) => el.getAttribute('src') !== before",
                                       arg=[image.element_handle(), previous_src], timeout=5000)
                break
            except PlaywrightTimeoutError:
                continue
    src = image.get_attribute("src") or ""
    return base64.b64decode(src.split(",", 1)[1].strip()), src


def _login_outcome(page: Page) -> tuple[str, str]:
    """After clicking Login: ("otp", ""), ("captcha", text) for a wrong
    CAPTCHA, or ("unknown", text). Raises for a rejected password, a locked
    user, a forced password change or maintenance."""
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if _visible(page, OTP_INPUTS):
            return "otp", ""
        text = popups.dialog_text(page)
        inline = page.locator(INLINE_ERRORS)
        if not text and inline.count():
            text = " ".join(" ".join(inline.nth(i).inner_text().split()) for i in range(inline.count()))
        if text:
            if re.search(popups.LOCKED, text, re.I):
                raise ActionNeeded(popups.STOP_RULES[0].message)
            if re.search(popups.BAD_CREDENTIALS, text, re.I):
                raise ActionNeeded(popups.STOP_RULES[1].message)
            kind = "captcha" if re.search(popups.CAPTCHA_WRONG, text, re.I) else "unknown"
            popups.dismiss(page)  # closes the message box (EXIT); raises for blocking ones
            return kind, text
        if page.locator(popups.DIALOGS).count():
            popups.dismiss(page)  # e.g. password-expiry reminder: "I'll do it later"
        page.wait_for_timeout(300)
    return "unknown", ""


def _enter_password_and_captcha(page: Page) -> None:
    max_auto = MAX_AUTO_CAPTCHA_ATTEMPTS if sys.stdin.isatty() else MAX_AUTO_CAPTCHA_ATTEMPTS_NO_TTY
    auto_attempts = unknown = 0
    previous_src = None
    message = "Type the CAPTCHA shown in the image."
    for _ in range(MAX_SUBMITS):
        _check_host(page)
        page.locator(USERNAME_INPUT).fill(config.SBI_USERNAME)
        page.locator(PASSWORD_INPUT).fill(config.SBI_PASSWORD)
        png, previous_src = _captcha_png(page, previous_src)

        if auto_attempts < max_auto:
            auto_attempts += 1
            record.write_status("logging-in", f"Reading the CAPTCHA (try {auto_attempts})…")
            captcha = solve_captcha(png)
            print(f"CAPTCHA auto-read attempt {auto_attempts}: {captcha or 'unreadable'}", flush=True)
            if not captcha:
                previous_src = None
                page.locator(CAPTCHA_REFRESH).first.click() if _visible(page, CAPTCHA_REFRESH) else page.reload()
                if not _visible(page, USERNAME_INPUT):
                    page.locator(LOGIN_TAB).first.click()
                continue
        else:
            if not sys.stdin.isatty():
                raise FetchError(f"The CAPTCHA could not be read in {max_auto} tries. Fetch again.")
            # Headless: no window to look at, so show the image file.
            image = config.OUTPUT_DIR / "captcha_raw.png"
            image.write_bytes(png)
            print(f"{message} Opening {image.relative_to(config.ROOT)}", flush=True)
            subprocess.Popen(["xdg-open", str(image)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            captcha = read_line("CAPTCHA: ", hidden=False, timeout=OTP_WAIT_S)
            if not captcha:
                raise FetchError("No CAPTCHA was entered.")
        page.locator(CAPTCHA_INPUT).fill(captcha)
        page.locator(LOGIN_BUTTON).click()

        kind, text = _login_outcome(page)
        if kind == "otp":
            return
        if kind == "captcha":
            message = "The CAPTCHA was wrong. Type the new one."
            continue
        unknown += 1
        print(f"Login not accepted: {text[:100] or 'no message'}", flush=True)
        if unknown >= 2:
            fail(page, config.OUTPUT_DIR, "login-result",
                 f"SBI didn't accept the login{': ' + text[:80] if text else ''}")
    raise FetchError("Too many login attempts; stopped so the account doesn't lock.")


def _otp_digits(page: Page) -> int:
    count = page.locator(OTP_INPUTS).count()
    if not 4 <= count <= 8:
        fail(page, config.OUTPUT_DIR, "otp-boxes", "The OTP boxes look different than expected")
    return count


def _otp_result(page: Page) -> str:
    """"ok" once the dashboard shows; "wrong" / "expired" if refused;
    "kicked" if SBI closed the OTP dialog and showed the login form again."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _visible(page, DASHBOARD_READY):
            return "ok"
        text = popups.dialog_text(page)
        inline = page.locator(f"dff-mpin-dialog .error:visible, dff-mpin-dialog mat-error:visible, dff-mpin-dialog [role=alert]:visible")
        if not text and inline.count():
            text = inline.first.inner_text()
        if text:
            if re.search(popups.LOCKED, text, re.I) or re.search(r"attempts|exceeded", text, re.I):
                raise ActionNeeded("Too many wrong OTPs; SBI ended the login. Wait a while, then fetch again.")
            kind = "expired" if re.search(popups.OTP_EXPIRED, text, re.I) else "wrong"
            popups.dismiss(page)
            return kind
        if not _visible(page, OTP_DIALOG) and _visible(page, USERNAME_INPUT):
            return "kicked"
        if page.locator(popups.DIALOGS).count():
            popups.dismiss(page)  # password-expiry reminder after the OTP
        page.wait_for_timeout(500)
    return "wrong" if _visible(page, OTP_DIALOG) else "kicked"


def _resend(page: Page) -> bool:
    if _visible(page, OTP_RESEND):
        page.locator(OTP_RESEND).first.click()
        return True
    return False


def _enter_otp(page: Page) -> None:
    digits = _otp_digits(page)
    header = page.locator(OTP_HEADER)
    sent_to = " ".join(header.first.inner_text().split()) if header.count() else ""
    message = sent_to or "Enter the OTP sent to your registered mobile number."
    wrong = resends = 0
    while True:
        record.write_status("otp-needed", message, digits=digits)
        print(message, flush=True)
        drain_stdin()
        otp = read_line("OTP (or 'resend'): ", hidden=True, timeout=OTP_WAIT_S)
        if otp is None:
            raise FetchError("No OTP was entered within 5 minutes.")
        if otp.lower() in ("r", "resend"):
            if resends < MAX_RESENDS and _resend(page):
                resends += 1
                message = "A new OTP was requested. Enter it when it arrives."
            else:
                message = "A new OTP can't be requested yet (wait for the timer). Enter the current one."
            continue
        if not otp.isdigit() or len(otp) != digits:
            message = f"Enter the {digits}-digit OTP. The last entry was empty or not {digits} digits."
            continue
        boxes = page.locator(OTP_INPUTS)
        for i, digit in enumerate(otp):
            boxes.nth(i).fill(digit)
        del otp
        submit = page.locator(OTP_SUBMIT_BUTTON).first
        try:
            page.wait_for_function("el => !el.disabled && el.getAttribute('disabled') !== 'true'",
                                   arg=submit.element_handle(), timeout=5000)
        except PlaywrightTimeoutError:
            for i in range(digits):
                boxes.nth(i).fill("")
            message = "The OTP boxes didn't take the digits. Enter the OTP again."
            continue
        submit.click()
        record.write_status("otp-checking", "Checking the OTP…")
        result = _otp_result(page)
        if result == "ok":
            return
        if result == "kicked":
            fail(page, config.OUTPUT_DIR, "after-otp", "SBI closed the OTP step and returned to the login form. Fetch again")
        for i in range(page.locator(OTP_INPUTS).count()):
            page.locator(OTP_INPUTS).nth(i).fill("")
        if result == "expired":
            if resends < MAX_RESENDS and _resend(page):
                resends += 1
                message = "That OTP expired. A new one was sent; enter it."
                continue
            raise FetchError("The OTP expired. Fetch again.")
        wrong += 1
        if wrong >= MAX_WRONG_OTPS:
            raise ActionNeeded(f"{wrong} wrong OTPs; stopped so SBI doesn't lock the login. Fetch again later.")
        message = f"That OTP was wrong (try {wrong} of {MAX_WRONG_OTPS}). Enter it again."


def run() -> str:
    """Login only (for testing); doesn't save cookies."""
    from contextlib import suppress
    from playwright.sync_api import sync_playwright
    from . import browser
    from .guard import install_page_guards
    if not config.SBI_USERNAME or not config.SBI_PASSWORD:
        raise ActionNeeded("Set SBI_USERNAME and SBI_PASSWORD in the project's .env file.")
    with sync_playwright() as p:
        b = browser.launch(p)
        context, page = browser.new_page(b)
        install_page_guards(context, page)
        try:
            open_login_page(page)
            login(page)
            page.locator(DASHBOARD_READY).wait_for(state="visible", timeout=45000)
            return "Logged in."
        finally:
            with suppress(Exception):
                context.close()
            with suppress(Exception):
                b.close()


if __name__ == "__main__":
    from .guard import run_main
    run_main(run, record.write_status, "SBI", config.OUTPUT_DIR)
