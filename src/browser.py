"""Headless Chromium, set up exactly like the HDFC fetcher's (which works
headless): Chromium's full headless mode, automation flag off, a regular
desktop Chrome user-agent, a desktop viewport. No window opens.
SBI_HEADED=1 shows the window, e.g. to watch a run or debug a page."""
import os

from playwright.sync_api import Browser, Playwright

from .guard import launch_with_install

# Same desktop user-agent as the HDFC fetcher: sites refuse Playwright's
# default headless one.
_DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
HEADED = os.environ.get("SBI_HEADED") == "1"


def launch(playwright: Playwright, on_status=None) -> Browser:
    # channel="chromium" is Chromium's full headless mode. launch_with_install
    # downloads the browser build first when a rebuilt .venv has a newer
    # playwright than the one the cached build belongs to.
    return launch_with_install(lambda: playwright.chromium.launch(
        headless=not HEADED,
        channel="chromium",
        args=["--disable-blink-features=AutomationControlled"],
    ), on_status)


def new_page(browser: Browser):
    context = browser.new_context(
        user_agent=_DESKTOP_USER_AGENT,
        viewport={"width": 1366, "height": 900},
        device_scale_factor=3,
    )
    return context, context.new_page()
