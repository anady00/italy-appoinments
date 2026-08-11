"""
Prenot@Mi Availability Watcher
--------------------------------
Logs into prenotami.esteri.it, checks every service listed on the account's
/Services page for available appointment slots, and sends an email when a
service that had NO availability now shows a green ("Disponibile") date.

Environment variables required (set as GitHub Actions secrets):
    PRENOTAMI_USER        - login email/username for prenotami.esteri.it
    PRENOTAMI_PASS         - login password
    GMAIL_USER              - the gmail address used to SEND the notification
    GMAIL_APP_PASSWORD      - the 16-char Gmail App Password (NOT your normal password)
    NOTIFY_EMAIL            - (optional) where to send alerts, defaults to GMAIL_USER

Optional environment variables:
    DEBUG=1  - saves screenshots + HTML of each page into ./debug/ for troubleshooting

State is kept in state.json (committed back to the repo by the GitHub Action)
so the script only emails you about *new* availability, not the same slot
over and over.
"""

import os
import re
import json
import sys
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_URL = "https://prenotami.esteri.it"
SERVICES_URL = f"{BASE_URL}/Services"
STATE_FILE = "state.json"
DEBUG = os.environ.get("DEBUG") == "1"

PRENOTAMI_USER = os.environ["PRENOTAMI_USER"]
PRENOTAMI_PASS = os.environ["PRENOTAMI_PASS"]
GMAIL_USER = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", GMAIL_USER)


def debug_dump(page, name):
    if not DEBUG:
        return
    os.makedirs("debug", exist_ok=True)
    try:
        page.screenshot(path=f"debug/{name}.png", full_page=True)
        with open(f"debug/{name}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
    except Exception as e:
        print(f"[debug] could not dump {name}: {e}")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_email(subject, body):
    msg = MIMEMultipart()
    msg["From"] = GMAIL_USER
    msg["To"] = NOTIFY_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)
    print(f"[email] sent: {subject}")


def login(page):
    """Log into prenotami.esteri.it. This walks through the public login
    form; if esteri.it changes their login page this is the part that will
    need updating (run with DEBUG=1 and check debug/login.html)."""
    page.goto(f"{BASE_URL}/Home", wait_until="domcontentloaded")
    debug_dump(page, "01_home")

    # Click "Effettuare il Login" which redirects to the OAuth (iam.esteri.it) page
    page.click("text=Effettuare il Login")
    page.wait_for_load_state("domcontentloaded")
    debug_dump(page, "02_oauth_page")

    # Try common selectors for the identity-provider login form.
    user_selectors = ["input[type='email']", "input[name='username']", "#username", "input#Username"]
    pass_selectors = ["input[type='password']", "input[name='password']", "#password", "input#Password"]

    filled_user = False
    for sel in user_selectors:
        if page.locator(sel).count() > 0:
            page.fill(sel, PRENOTAMI_USER)
            filled_user = True
            break
    if not filled_user:
        debug_dump(page, "02b_no_user_field")
        raise RuntimeError(
            "Could not find the username field on the login page. "
            "Re-run with DEBUG=1, inspect debug/02_oauth_page.html and update user_selectors."
        )

    filled_pass = False
    for sel in pass_selectors:
        if page.locator(sel).count() > 0:
            page.fill(sel, PRENOTAMI_PASS)
            filled_pass = True
            break
    if not filled_pass:
        debug_dump(page, "02c_no_pass_field")
        raise RuntimeError(
            "Could not find the password field on the login page. "
            "Re-run with DEBUG=1, inspect debug/02_oauth_page.html and update pass_selectors."
        )

    # Submit
    submit_selectors = ["button[type='submit']", "input[type='submit']", "text=Login", "text=Accedi"]
    for sel in submit_selectors:
        if page.locator(sel).count() > 0:
            page.click(sel)
            break

    page.wait_for_load_state("networkidle", timeout=30000)
    debug_dump(page, "03_after_login")

    if "Services" not in page.url and page.locator("text=ahmed nady").count() == 0:
        # Not a hard failure -- some accounts land elsewhere -- but flag it.
        print(f"[login] warning: unexpected post-login URL: {page.url}")


def get_service_links(page):
    """Visit /Services and collect every 'BOOK' link on the table."""
    page.goto(SERVICES_URL, wait_until="domcontentloaded")
    page.wait_for_selector("table", timeout=20000)
    debug_dump(page, "04_services_list")

    links = page.eval_on_selector_all(
        "table a[href*='/Services/Booking/'], table a:has-text('BOOK')",
        "els => els.map(e => ({href: e.href, text: e.closest('tr') ? e.closest('tr').innerText : ''}))",
    )

    # Dedupe by href, keep first row text as a rough service name
    seen = {}
    for item in links:
        href = item["href"]
        if href not in seen:
            # First line of the row text is usually the service type/name
            name = item["text"].split("\n")[0:2]
            seen[href] = " / ".join(name).strip()
    return seen  # {url: label}


def check_service_availability(page, url):
    """Open a booking calendar page and look for green ('Disponibile') dates."""
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector(".calendar, table.ui-datepicker-calendar, td", timeout=15000)
    except PWTimeout:
        pass
    debug_dump(page, f"cal_{re.sub(r'[^A-Za-z0-9]+', '_', url)[-40:]}")

    # Heuristic: look for calendar day cells that are marked available.
    # Adjust these selectors after inspecting debug/*.html if they don't match.
    available_cells = page.query_selector_all(
        ".ui-datepicker-calendar td.available, "
        "td.disponibile, "
        "td[class*='available' i], "
        "a.day.available"
    )
    if available_cells:
        dates = []
        for cell in available_cells:
            txt = cell.inner_text().strip()
            if txt:
                dates.append(txt)
        return True, dates

    # Fallback: some pages render availability as green-background inline styles
    green_cells = page.eval_on_selector_all(
        "td, a, span",
        """els => els
            .filter(e => {
                const bg = window.getComputedStyle(e).backgroundColor;
                return bg === 'rgb(0, 128, 0)' || bg === 'rgb(40, 167, 69)' || bg.includes('green');
            })
            .map(e => e.innerText.trim())
            .filter(t => t.length > 0 && t.length < 4)
        """,
    )
    if green_cells:
        return True, green_cells

    # Also treat an explicit "no availability" text as a confirmed *not available*
    return False, []


def main():
    state = load_state()
    new_state = {}
    newly_available = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        try:
            login(page)
        except Exception as e:
            print(f"[fatal] login failed: {e}")
            browser.close()
            sys.exit(1)

        try:
            services = get_service_links(page)
        except Exception as e:
            print(f"[fatal] could not read /Services page: {e}")
            browser.close()
            sys.exit(1)

        print(f"[info] found {len(services)} bookable services")

        for url, label in services.items():
            try:
                available, dates = check_service_availability(page, url)
            except Exception as e:
                print(f"[warn] failed to check {url}: {e}")
                continue

            new_state[url] = {"available": available, "dates": dates, "label": label}

            was_available = state.get(url, {}).get("available", False)
            if available and not was_available:
                newly_available.append((label, url, dates))

            status = "AVAILABLE" if available else "no slots"
            print(f"[check] {label[:60]:60} -> {status}")

        browser.close()

    save_state(new_state)

    if newly_available:
        lines = []
        for label, url, dates in newly_available:
            date_str = ", ".join(dates) if dates else "(see page)"
            lines.append(f"- {label}\n  {url}\n  dates: {date_str}")
        body = (
            f"في مواعيد جديدة متاحة على Prenot@Mi ({datetime.now(timezone.utc).isoformat()}):\n\n"
            + "\n\n".join(lines)
        )
        send_email(f"🟢 Prenot@Mi: {len(newly_available)} خدمة فيها مواعيد جديدة", body)
    else:
        print("[info] no new availability this run")


if __name__ == "__main__":
    main()
