"""Opens the savings account's Transactions tab and reads the table page by
page. Markup confirmed from a saved page (output/debug_page_2.html):

- The top-nav "Accounts" link opens Relationship Overview > Transaction
  Accounts, which shows the savings account with its Transactions tab.
- table.transaction-table: each row has four cells: date (dd/mm/yyyy),
  description, amount, balance. The amount cell has no sign; its status icon
  is up_arrow_new.svg for money out and down_arrow_new.svg for money in
  (checked against the running balance).
- Pagination: numbered buttons plus "Next" (button.nav, class nav-disabled
  when unusable) and a select#rowsPerPage with 10-50."""
import re
from dataclasses import dataclass

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from . import config, popups
from .guard import FetchError, fail

ACCOUNTS_NAV = "a.menu-header-text[aria-label='Accounts']"
TRANSACTIONS_TAB = "button[role='tab']:has(.tab-label:text-is('Transactions'))"
TABLE = "table.transaction-table"
ROWS = "table.transaction-table tbody tr"
ROWS_PER_PAGE = "select#rowsPerPage"
NEXT_BUTTON = ".pagination button:has-text('Next')"
ACTIVE_PAGE = ".pagination button.active-page"
SELECTED_ACCOUNT = "app-transaction-account-summary .accountStore"
ACCOUNT_NUMBER_RE = re.compile(r"X+(\d{4})\b")
_AMOUNT_RE = re.compile(r"(-)?\s*₹?\s*([\d,]+\.\d{2})\s*(Dr|Cr)?", re.I)
# Direction written in the description: "UPI/DR/", "TRANSFER TO" = money out.
_DEBIT_TEXT = re.compile(r"/DR/|\bTRANSFER TO\b|\bDEBIT\b", re.I)
_CREDIT_TEXT = re.compile(r"/CR/|\bTRANSFER FROM\b|\bCREDIT\b|\bREVERSAL\b", re.I)
# Stop paging after this many saved rows in a row (a late-posted row can sit
# below a saved one, so one known row isn't enough).
KNOWN_STREAK = 10


@dataclass
class Txn:
    date: str          # dd/mm/yyyy, as shown
    description: str
    amount: float      # negative = money out
    balance: float
    raw_amount: str
    sign_certain: bool = True


def _clean(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _money(text: str) -> float | None:
    """Unsigned amount, or None. A leading "-" or trailing "Dr" makes it
    negative (only seen on balances, e.g. an overdrawn account)."""
    match = _AMOUNT_RE.search(text)
    if not match:
        return None
    value = float(match.group(2).replace(",", ""))
    return -value if match.group(1) or (match.group(3) or "").lower() == "dr" else value


def open_transactions(page: Page) -> None:
    popups.dismiss(page)
    try:
        page.locator(ACCOUNTS_NAV).first.click(timeout=15000)
    except PlaywrightTimeoutError:
        popups.dismiss(page)
        fail(page, config.OUTPUT_DIR, "accounts-link", "Couldn't click Accounts in the SBI top menu")
    for attempt in range(2):
        try:
            page.locator(TABLE).wait_for(state="visible", timeout=15000)
            return
        except PlaywrightTimeoutError:
            popups.dismiss(page)
            # The account page opened on another tab (e.g. Account Summary).
            tab = page.locator(TRANSACTIONS_TAB)
            if tab.count():
                tab.first.click(timeout=10000)
    fail(page, config.OUTPUT_DIR, "transactions-table", "The SBI transactions table didn't appear")


def account_last4(page: Page) -> str:
    """Last four digits of the account being shown, from its masked number
    (XXXXXXX1234) in the selected-account header. The full number is never
    shown or stored. A miss stops the run: an empty value would change every
    saved id and duplicate every row."""
    header = page.locator(SELECTED_ACCOUNT)
    text = header.first.inner_text() if header.count() else page.locator("body").inner_text()
    match = ACCOUNT_NUMBER_RE.search(text)
    if not match:
        fail(page, config.OUTPUT_DIR, "account-number", "Couldn't read the SBI account number")
    return match.group(1)


def show_max_rows(page: Page) -> None:
    """50 rows per page when offered; otherwise carry on with the default."""
    select = page.locator(ROWS_PER_PAGE)
    if not select.count():
        return
    try:
        first = page.locator(ROWS).first.inner_text()
        select.first.select_option("50")
        page.wait_for_function(
            "([sel, before]) => { const r = document.querySelectorAll(sel);"
            " return r.length > 10 || (r[0] && r[0].innerText !== before) || r.length < 10; }",
            arg=[ROWS, first], timeout=10000)
    except Exception as exc:
        print(f"Couldn't switch to 50 rows per page ({type(exc).__name__}); reading 10 at a time.", flush=True)


def parse_rows(page: Page) -> list[Txn]:
    """Every row on the current page. The sign comes from the amount's arrow
    icon (up = money out); when the icon is missing or disagrees with the
    description ("/DR/", "TRANSFER TO"), reconcile() decides from the
    running balance. A row whose amounts can't be read stops the run."""
    data = page.locator(ROWS).evaluate_all("""rows => rows.map(r => {
        const c = [...r.querySelectorAll('td')];
        const icon = c[2] && c[2].querySelector('img.statusIcon');
        return [c.map(x => x.innerText), icon ? icon.getAttribute('src') || '' : ''];
    })""")
    out: list[Txn] = []
    for cells, icon_src in data:
        if len(cells) < 4:
            continue
        date = _clean(cells[0])
        if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", date):
            continue
        value, balance = _money(cells[2]), _money(cells[3])
        if value is None or balance is None:
            fail(page, config.OUTPUT_DIR, "transaction-row", "A transaction row had an unexpected amount format")
        description = _clean(cells[1])
        by_icon = -1 if "up_arrow" in icon_src else (1 if "down_arrow" in icon_src else 0)
        by_text = -1 if _DEBIT_TEXT.search(description) else (1 if _CREDIT_TEXT.search(description) else 0)
        sign = by_icon or by_text or -1
        out.append(Txn(
            date=date, description=description, amount=sign * abs(value), balance=balance,
            raw_amount=_clean(cells[2]),
            sign_certain=bool(by_icon) and (not by_text or by_icon == by_text),
        ))
    return out


def reconcile(txns: list[Txn]) -> None:
    """Rows are newest first, so newer.balance - older.balance is the newer
    row's signed amount. That fixes any sign the icon and description
    couldn't settle, and a mismatch in size means a row or page is missing,
    so the run stops without saving."""
    for newer, older in zip(txns, txns[1:]):
        delta = round(newer.balance - older.balance, 2)
        if abs(abs(delta) - abs(newer.amount)) < 0.005:
            if delta != round(newer.amount, 2) and abs(delta) > 0:
                print(f"Sign corrected from the balance on {newer.date}.", flush=True)
            newer.amount = delta if delta else newer.amount
        else:
            raise FetchError(f"Balances don't add up between {older.date} and {newer.date} "
                             "(a page may have been skipped); nothing was saved.")
    unsure = [t for t in txns[-1:] if not t.sign_certain]
    if unsure:
        print(f"Sign of the oldest row ({unsure[0].date}) taken from its icon/description only.", flush=True)


def collect_all(page: Page, is_known, max_pages: int = 500) -> list[Txn]:
    """Reads the table page by page, newest first, clicking Next until it is
    disabled. Each page change is waited for (first row changes), so a slow
    page is never read twice and the next one skipped. Stops early after
    KNOWN_STREAK saved rows in a row: everything older was saved before."""
    seen: set[tuple] = set()
    out: list[Txn] = []
    streak = 0
    for _ in range(max_pages):
        popups.dismiss(page)
        for t in parse_rows(page):
            key = (t.date, t.description, t.raw_amount, t.balance)
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
            streak = streak + 1 if is_known(t) else 0
            if streak >= KNOWN_STREAK:
                return out
        next_button = page.locator(NEXT_BUTTON).first
        if next_button.count() == 0 or "nav-disabled" in (next_button.get_attribute("class") or ""):
            return out
        before_row = page.locator(ROWS).first.inner_text()
        before_page = page.locator(ACTIVE_PAGE).first.inner_text() if page.locator(ACTIVE_PAGE).count() else ""
        next_button.click()
        try:
            page.wait_for_function(
                "([sel, before]) => { const r = document.querySelector(sel); return r && r.innerText !== before; }",
                arg=[ROWS, before_row], timeout=20000)
        except PlaywrightTimeoutError:
            popups.dismiss(page)
            fail(page, config.OUTPUT_DIR, "pagination", f"The page after {before_page.strip() or 'this one'} didn't load")
        print(f"  page {page.locator(ACTIVE_PAGE).first.inner_text().strip() if page.locator(ACTIVE_PAGE).count() else '?'}", flush=True)
    raise FetchError(f"Stopped after {max_pages} pages; the list may be incomplete.")


def print_table(txns: list[Txn]) -> None:
    if not txns:
        print("No new transactions.")
        return
    width_desc = min(max(len(t.description) for t in txns), 70)
    header = f"{'Date':<10}  {'Description':<{width_desc}}  {'Amount':>14}"
    print(header)
    print("-" * len(header))
    for t in txns:
        desc = t.description if len(t.description) <= width_desc else t.description[: width_desc - 1] + "…"
        print(f"{t.date:<10}  {desc:<{width_desc}}  {t.amount:>14,.2f}")
    credits = sum(t.amount for t in txns if t.amount > 0)
    debits = sum(t.amount for t in txns if t.amount < 0)
    print("-" * len(header))
    print(f"{len(txns)} transactions   money in: {credits:,.2f}   money out: {debits:,.2f}")
