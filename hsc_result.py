import random
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, Error as BrowserError

URL = "https://www.educationboardresults.gov.bd/v2/home"

BOARDS = [
    "Barisal", "Chittagong", "Comilla", "Dhaka", "Dinajpur",
    "Jessore", "Madrasah", "Mymensingh", "Rajshahi",
    "Sylhet", "Technical",
]


def ask_digits(message, default=""):
    while True:
        value = input(message).strip() or default
        if value.isascii() and value.isdigit():
            return value
        print("Please enter English digits only.")


def wait_before_retry(attempt, server_wait=0):
    # Starts around 30 seconds; increases to about 5 minutes.
    delay = max(
        min(30 * 2 ** min(attempt - 1, 4), 300),
        server_wait,
    ) + random.randint(0, 5)
    print(f"Waiting {delay:.0f} seconds before retrying. Ctrl+C stops.")
    time.sleep(delay)


def main():
    print("\nHSC RESULT CHECKER")
    print("Keep the browser and this terminal open.")
    print("You will enter CAPTCHAs yourself. Ctrl+C stops.\n")

    for number, board in enumerate(BOARDS, 1):
        print(f"{number:2}. {board}")

    while True:
        choice = input("\nBoard number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(BOARDS):
            board = BOARDS[int(choice) - 1]
            break
        print("Choose a number from the list.")

    year = ask_digits("Exam year [2026]: ", "2026")
    roll = ask_digits("Roll number: ")
    registration = ask_digits("Registration number: ")

    print(f"\nBoard: {board} | Year: {year}")
    print(f"Roll: {roll} | Registration: {registration}")
    if input("Are these correct? Type YES: ").strip().upper() != "YES":
        return

    # Files are saved beside this Python script.
    output = Path(__file__).resolve().parent / "hsc_results"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(15000)

        # Respect Retry-After on rate-limit / unavailable responses.
        state = {"retry_at": 0.0, "blocked": False}

        def on_response(response):
            if not response.url.startswith(
                "https://www.educationboardresults.gov.bd/"
            ):
                return
            if response.status == 403:
                state["blocked"] = True
            if response.status in (429, 503):
                value = response.headers.get("retry-after", "")
                seconds = 300 if response.status == 429 else 60
                try:
                    seconds = max(seconds, float(value))
                except ValueError:
                    try:
                        date = parsedate_to_datetime(value)
                        if date.tzinfo is None:
                            date = date.replace(tzinfo=timezone.utc)
                        seconds = max(seconds, date.timestamp() - time.time())
                    except (ValueError, TypeError, OverflowError):
                        pass
                state["retry_at"] = max(
                    state["retry_at"], time.time() + seconds
                )

        def on_dialog(dialog):
            print("\nWebsite message:", dialog.message)
            dialog.dismiss()

        page.on("response", on_response)
        page.on("dialog", on_dialog)

        attempt = 0
        try:
            while True:
                attempt += 1
                print(f"\nAttempt {attempt}: opening the result page...")

                if state["retry_at"] > time.time():
                    time.sleep(state["retry_at"] - time.time())

                state["blocked"] = False
                try:
                    page.goto(
                        URL, wait_until="domcontentloaded", timeout=60000
                    )
                    # Allows the site's normal browser check to finish.
                    page.get_by_role(
                        "combobox", name="Name of Board", exact=True
                    ).wait_for(state="visible", timeout=45000)

                    page.get_by_role(
                        "combobox", name="Name of Board", exact=True
                    ).select_option(label=board)

                    page.get_by_role(
                        "combobox", name="Name of Examination", exact=True
                    ).select_option(label="HSC/Alim/Equivalent")

                    page.get_by_role(
                        "combobox", name="Year of Examination", exact=True
                    ).select_option(label=year)

                    page.get_by_role(
                        "combobox", name="Type of Result", exact=True
                    ).select_option(label="Individual/Detailed Result")

                    page.get_by_label(
                        "Roll Number of Examinee", exact=True
                    ).fill(roll)

                    page.get_by_label(
                        "Registration Number of Examinee", exact=True
                    ).fill(registration)

                    if state["retry_at"] > time.time():
                        raise RuntimeError("The server requested a wait.")

                    print("Read the CAPTCHA in the browser window.")
                    captcha = input(
                        "Enter CAPTCHA here, or Q to quit: "
                    ).strip()
                    if captcha.lower() == "q":
                        break

                    page.get_by_role(
                        "spinbutton",
                        name=re.compile("Robot Prevention Technique"),
                    ).fill(captcha)

                    page.get_by_role(
                        "button", name="View Result", exact=True
                    ).click()

                    print("Submitted. Waiting up to 60 seconds...")
                    # Polls the already-loaded browser DOM, not the server.
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        page.wait_for_timeout(2000)
                        text = page.locator("body").inner_text()
                        has_roll = bool(
                            re.search(
                                rf"(?<!\d){re.escape(roll)}(?!\d)", text
                            )
                        )
                        if has_roll and re.search(
                            r"\bGPA\b|\bGrade Point\b", text, re.I
                        ):
                            break
                        if state["blocked"] or (
                            state["retry_at"] > time.time()
                        ):
                            break

                    # Human confirmation prevents saving an error page
                    # or another student's result as a successful result.
                    print("\nInspect the browser.")
                    answer = input(
                        "Is the correct student's result visible?\n"
                        "Y = save and stop | Enter = retry | Q = quit: "
                    ).strip().lower()

                    if answer == "q":
                        break
                    if answer == "y":
                        output.mkdir(exist_ok=True)
                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        base = output / f"HSC_{year}_{roll}_{stamp}"

                        base.with_suffix(".txt").write_text(
                            page.locator("body").inner_text(),
                            encoding="utf-8",
                        )
                        try:
                            page.screenshot(
                                path=str(base.with_suffix(".png")),
                                full_page=True,
                            )
                        except BrowserError:
                            print("Screenshot failed; text was saved.")

                        print(f"\nRESULT SAVED in: {output}")
                        print("\a", end="", flush=True)
                        input("Press Enter when finished viewing the browser.")
                        break

                except (BrowserError, RuntimeError) as error:
                    if page.is_closed():
                        print("Browser closed. Stopping.")
                        break
                    print("Attempt did not complete:", str(error).splitlines()[0])

                # Do not repeatedly retry an access / verification block.
                try:
                    body = page.locator("body").inner_text(timeout=5000).lower()
                except BrowserError:
                    body = ""

                if state["blocked"] or any(phrase in body for phrase in (
                    "verification failed",
                    "verify you are human",
                    "verifying you are not a robot",
                    "access denied",
                )):
                    print("Access or browser verification needs your attention.")
                    input(
                        "Inspect the browser. Press Enter to try again, "
                        "or Ctrl+C to stop."
                    )

                wait_before_retry(
                    attempt, max(0, state["retry_at"] - time.time())
                )
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as error:
        print(f"\nStopped because of an error: {error}")
        input("Press Enter to close.")
