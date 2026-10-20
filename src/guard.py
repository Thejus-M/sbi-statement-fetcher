"""Safety helpers shared by every step of a fetch: error types with short
messages for the widget, atomic file writes, debug dumps, popup handling,
timed stdin reads, a single-run lock, and the run wrapper that guarantees
the status file always ends as done / error / cancelled.

The same file is used by the Axis, SBI and HDFC fetchers; only the popup
rules and messages passed in differ."""
import fcntl
import getpass
import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import traceback
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError


# --- errors -----------------------------------------------------------------

class FetchError(Exception):
    """A failure with a one-line message that is safe to show in the widget."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ActionNeeded(FetchError):
    """The bank needs the user to do something (locked account, wrong
    password, forced password change). Never retried automatically, since
    retries can lock the account."""


class Cancelled(BaseException):
    """Cancel from the widget (SIGTERM), Ctrl+C, or OTP input closed.
    A BaseException so `except Exception` blocks never swallow it."""


def first_line(exc: BaseException, limit: int = 160) -> str:
    text = str(exc).strip().splitlines()
    return (text[0] if text else type(exc).__name__)[:limit]


def mask_digits(text: str) -> str:
    """Account numbers and amounts never go into status messages or logs."""
    return re.sub(r"\d", "•", text)


# --- files ------------------------------------------------------------------

def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write via a temp file and rename, so a crash or Cancel mid-write never
    leaves a half-written file, and the widget never reads one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_json(path: Path, data, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode)


def read_json_list(path: Path) -> list:
    """A missing file is an empty list. A corrupt one is moved aside and the
    run stops: treating it as empty would overwrite the saved history."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = None
    if not isinstance(data, list):
        aside = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}")
        os.replace(path, aside)
        raise FetchError(f"{path.name} was corrupt; moved it to {aside.name}. Restore it or delete it, then fetch again.")
    return data


def backup(path: Path) -> None:
    """One-generation backup of a data file before it is replaced."""
    if path.exists():
        with suppress(OSError):
            atomic_write_text(path.with_name(path.name + ".bak"), path.read_text(encoding="utf-8"))


def dump(page: Page, output_dir: Path, step: str, keep: int = 20) -> str:
    """Save the page (HTML + screenshot) for a failed step. Files are private
    (they contain account data) and only the newest `keep` are kept."""
    folder = output_dir / "debug"
    base = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '-', step.lower()).strip('-')}"
    with suppress(Exception):
        atomic_write_text(base.with_suffix(".html"), f"<!-- URL: {page.url} -->\n{page.content()}")
    with suppress(Exception):
        page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        os.chmod(base.with_suffix(".png"), 0o600)
    with suppress(Exception):
        files = sorted(folder.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep * 2:]:
            old.unlink()
    return f"debug/{base.name}.html"


def fail(page: Page, output_dir: Path, step: str, message: str, exc=FetchError):
    """Dump the page and raise one clear error for a step that didn't work."""
    raise exc(f"{message} (page saved to output/{dump(page, output_dir, step)})")


# --- browser ------------------------------------------------------------------

# Playwright ties each release to one browser build and refuses to launch
# without it: "Executable doesn't exist at .../chromium-1248/chrome-linux64/chrome".
_MISSING_BROWSER_RE = re.compile(r"executable doesn'?t exist|playwright install", re.I)


def launch_with_install(launch, on_status=None, timeout: float = 1800):
    """Call `launch()`, and when the browser build playwright wants is not
    downloaded yet, download it once and call `launch()` again.

    Rebuilding a .venv with `uv pip install -r requirements.txt` installs the
    newest playwright, which expects a newer browser build than the one in
    ~/.cache/ms-playwright. The browsers are not part of the package, so every
    fetch then failed at launch until someone ran `playwright install` by hand.
    The download is about 170 MB and happens once per playwright upgrade;
    after that this adds one failed launch attempt and nothing else."""
    try:
        return launch()
    except PlaywrightError as exc:
        if not _MISSING_BROWSER_RE.search(str(exc)):
            raise
    if on_status:
        on_status("starting", "Downloading the browser (one-off, takes a few minutes)...")
    print("Playwright browser build missing; running `playwright install chromium`.", flush=True)
    command = [sys.executable, "-m", "playwright", "install", "chromium"]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FetchError("Downloading the browser took too long. Run "
                         "`.venv/bin/python -m playwright install chromium` in this project, then fetch again.")
    # Progress bars are one line per 10%; keep the real lines only.
    lines = [line for line in result.stdout.splitlines() if line.strip() and "%" not in line]
    print("\n".join(lines[-5:]), flush=True)
    if result.returncode != 0:
        raise FetchError("Couldn't download the browser Playwright needs. Run "
                         "`.venv/bin/python -m playwright install chromium` in this project, then fetch again.")
    return launch()


# --- popups -----------------------------------------------------------------

# Buttons that close a nag without agreeing to anything.
DISMISS_RE = re.compile(
    r"^(remind me later|remind later|i'?ll do it later|i will do it later|do it later|maybe later|later|"
    r"skip( for now)?|not now|no,? thanks?|no thank you|close|dismiss|cancel|got it|ok|okay|exit|x|×)$", re.I)
# Never clicked when dismissing: these start flows or agree to things.
NEVER_RE = re.compile(
    r"update|proceed|submit|confirm|download|apply|verify|pay\b|change password|log ?out|yes|accept|register|buy", re.I)
CLOSE_ICONS = ("[mat-dialog-close], button[aria-label*='close' i], [aria-label='Close'], "
               ".btn-close, button.close, .close-icon, .cross-icon")


class StopRule:
    """A popup that must stop the run. `soft` rules (e.g. "password expires
    in 5 days") are only fatal when the popup offers no way to dismiss it."""

    def __init__(self, pattern: str, message: str, soft: bool = False, exc=ActionNeeded):
        self.pattern = re.compile(pattern, re.I)
        self.message = message
        self.soft = soft
        self.exc = exc


def _buttons(dialog):
    return dialog.locator("button, a, [role=button], input[type=button], input[type=submit]")


def _find_button(dialog, pattern: re.Pattern, allow_never: bool = False):
    buttons = _buttons(dialog)
    for i in range(min(buttons.count(), 40)):
        b = buttons.nth(i)
        try:
            if not b.is_visible():
                continue
            label = " ".join((b.inner_text(timeout=1000) or b.get_attribute("value") or "").split())
        except PlaywrightError:
            continue
        if pattern.search(label) and (allow_never or not NEVER_RE.search(label)):
            return b
    return None


def _dialog_text(dialog) -> str:
    """The dialog's text with its button labels set apart: innerText runs a
    message and an inline button together ("User is lockedEXIT"), which
    breaks word matching."""
    text = dialog.inner_text(timeout=3000)
    labels = dialog.locator("button, a, [role=button]").evaluate_all(
        "els => els.map(e => e.innerText.trim()).filter(Boolean)")
    for label in sorted(set(labels), key=len, reverse=True):
        text = text.replace(label, f" {label} ")
    return " ".join(text.split())


def handle_popups(page: Page, dialog_selector: str, stop_rules=(), keepalive_re: str | None = None,
                  ignore_selector: str | None = None, max_rounds: int = 5) -> list[str]:
    """Close nags (offers, KYC/profile updates, surveys, app download, "remind
    me later" password expiry) and raise for popups that need the user.
    Returns a short, digit-masked note per popup handled."""
    notes: list[str] = []
    last_text = None
    for _ in range(max_rounds):
        dialogs = page.locator(dialog_selector)
        visible = []
        for i in range(min(dialogs.count(), 15)):
            d = dialogs.nth(i)
            with suppress(PlaywrightError):
                if d.is_visible() and not (ignore_selector and d.evaluate(
                        "(el, sel) => !!el.closest(sel) || !!el.querySelector(sel)", ignore_selector)):
                    visible.append(d)
        if not visible:
            return notes
        d = visible[-1]
        try:
            text = _dialog_text(d)
        except PlaywrightError:
            continue
        can_dismiss = _find_button(d, DISMISS_RE) is not None
        for rule in stop_rules:
            if rule.pattern.search(text) and not (rule.soft and can_dismiss):
                raise rule.exc(rule.message)
        if text == last_text:
            raise FetchError(f"A popup could not be closed: \"{mask_digits(text[:80])}\"")
        last_text = text
        button = (_find_button(d, re.compile(keepalive_re, re.I), allow_never=True) if keepalive_re else None) \
            or _find_button(d, DISMISS_RE)
        if button is None:
            icons = d.locator(CLOSE_ICONS)
            button = icons.first if icons.count() and icons.first.is_visible() else None
        if button is not None:
            with suppress(PlaywrightError):
                button.click(timeout=5000)
        else:
            page.keyboard.press("Escape")
        with suppress(PlaywrightError):
            d.wait_for(state="hidden", timeout=3000)
        note = mask_digits(text[:60])
        notes.append(note)
        print(f"popup handled: {note}", flush=True)
    return notes


def install_page_guards(context, page: Page) -> None:
    """Native alert/confirm dialogs are dismissed (so they never block a
    step) and extra tabs from ads or links are closed."""
    def on_dialog(dialog):
        print(f"browser dialog dismissed: {mask_digits(dialog.message[:80])}", flush=True)
        with suppress(PlaywrightError):
            dialog.dismiss()
    page.on("dialog", on_dialog)
    context.on("page", lambda p: p.close() if p != page else None)
    page.set_default_timeout(20000)
    page.set_default_navigation_timeout(60000)


# --- stdin --------------------------------------------------------------------

def drain_stdin() -> None:
    """Drop lines the widget sent before this prompt (e.g. a short retry), so
    a stale code is never used for a new prompt."""
    if sys.stdin.isatty():
        return
    while select.select([sys.stdin], [], [], 0)[0]:
        if not sys.stdin.readline():
            return


def read_line(prompt: str, hidden: bool, timeout: float) -> str | None:
    """One line from the terminal (hidden if asked) or, from the widget, from
    stdin. None after `timeout` seconds. Closed input means cancelled."""
    if sys.stdin.isatty():
        result: list[str] = []
        reader = threading.Thread(
            target=lambda: result.append(getpass.getpass(prompt) if hidden else input(prompt)), daemon=True)
        reader.start()
        reader.join(timeout)
        return result[0].strip() if result else None
    print(prompt, flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if not ready:
        return None
    line = sys.stdin.readline()
    if not line:
        raise Cancelled("Input closed")
    return line.strip()


# --- run lifecycle ------------------------------------------------------------

def _on_signal(signum, frame):
    # Second signal: stop at once, even if cleanup is stuck.
    signal.signal(signum, lambda *_: os._exit(143))
    raise Cancelled("Fetch cancelled.")


def run_main(run, write_status, bank: str, output_dir: Path) -> None:
    """Run `run()` (returns the done message) so that the status file always
    ends as done / error / cancelled, only one fetch runs at a time, and the
    pid file the widget uses for Cancel is removed when this run ends."""
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGHUP, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    output_dir.mkdir(exist_ok=True)
    lock = open(output_dir / ".fetch.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"Another {bank} fetch is already running.", file=sys.stderr)
        sys.exit(1)

    run_id = None
    with suppress(Exception):  # run history is nice to have, never a reason to fail
        from . import store
        run_id = store.ingest.start_run(store.conn(), bank.upper())

    final, code = ("error", f"{bank} fetch failed."), 1
    added = total = None
    try:
        # run() returns the done message, or (message, rows added, rows saved).
        result = run()
        message, added, total = result if isinstance(result, tuple) else (result, None, None)
        final, code = ("done", message), 0
        # Every statement prints a running balance beside each transaction.
        # Those have to agree with the amounts between them, and the newest
        # one with the account's current balance — if they don't, the scrape
        # read something wrong and the widget would show a wrong balance
        # without ever saying so. Checked here, while the run is still the
        # thing the user is looking at, and reported in its own done message.
        with suppress(Exception):
            from . import store
            mismatch = store.queries.reconcile_summary(store.conn(), bank.upper())
            if mismatch:
                final = ("done", f"{message}  \u00b7  {mismatch}")
                print(f"WARNING: {mismatch}", file=sys.stderr)
    except Cancelled:
        final, code = ("cancelled", "Fetch cancelled."), 143
    except FetchError as exc:
        final = ("error", f"{bank}: {exc.message}")
        code = 2 if isinstance(exc, ActionNeeded) else 1
        print(f"ERROR: {exc.message}", file=sys.stderr)
    except PlaywrightTimeoutError as exc:
        traceback.print_exc()
        final = ("error", f"{bank}: the page took too long to respond ({first_line(exc, 100)}).")
    except PlaywrightError as exc:
        traceback.print_exc()
        closed = re.search(r"closed|crash|disconnected", str(exc), re.I)
        final = ("error", f"{bank}: the browser window was closed or crashed." if closed
                 else f"{bank}: browser error: {first_line(exc)}")
    except Exception as exc:
        traceback.print_exc()
        final = ("error", f"{bank} fetch failed: {type(exc).__name__}: {first_line(exc)}")
    finally:
        with suppress(Exception):
            write_status(*final)
        with suppress(Exception):
            from . import store
            if run_id is not None:
                store.ingest.finish_run(store.conn(), run_id, final[0], final[1], added, total)
            if final[0] == "done":
                print(f"Backup: {store.backup(store.conn())}", flush=True)
        with suppress(OSError):
            pid_file = output_dir / "fetch.pid"
            if pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
    print(f"{final[0]}: {final[1]}", flush=True)
    sys.exit(code)
