"""One-off probe for building the transaction extractor: logs in, then saves
each page you visit so its markup can be read.

    uv run python -m src.probe_pages

After login, click through to your account's transactions/statement page in
the browser. Each time you press Enter in the terminal, the current page is
saved to output/debug_page_<n>.html with its URL. Type q and Enter to finish.
The saved pages contain your account details and stay in output/ (git-ignored)."""
from playwright.sync_api import Page, sync_playwright

from . import browser, config, login


def _save(page: Page, n: int) -> None:
    path = config.OUTPUT_DIR / f"debug_page_{n}.html"
    path.write_text(f"<!-- URL: {page.url} -->\n{page.content()}", encoding="utf-8")
    print(f"Saved {path.relative_to(config.ROOT)}  ({page.url})", flush=True)


def run() -> None:
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        b = browser.launch(p)
        context, page = browser.new_page(b)
        login.open_login_page(page)
        login.login(page)
        page.locator(login.DASHBOARD_READY).wait_for(state="visible", timeout=45000)

        n = 1
        _save(page, n)
        print("Navigate to the transactions page in the browser. Press Enter to save "
              "the current page, or type q and Enter to finish.", flush=True)
        while input("> ").strip().lower() != "q":
            n += 1
            _save(page, n)

        context.close()
        b.close()


if __name__ == "__main__":
    run()
