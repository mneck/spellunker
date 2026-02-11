#!/usr/bin/env python3
"""
Scrape vocabulary from Duolingo's Practice Hub words page, save it under `data/`,
and insert it into the database.

Usage:
    # 1) Install dependencies and browsers:
    #    pip install -r requirements.txt
    #    python -m playwright install
    #
    # 2) Ensure DATABASE_URL is set in your environment (.env is supported).
    #
    # 3) Run the scraper from the project root:
    #    python -m scraping.duolingo_scraper
    #
    # On the first run, a Chromium window will open and you should:
    #   - Log in to Duolingo using "Continue with Google" (SSO).
    #   - Navigate automatically to the Practice Hub words page.
    #   - Once you see your words list fully loaded, return to the terminal
    #     and press Enter when prompted so the authenticated session can be saved.
    #
    # The scraped word list will be written to:
    #   data/duolingo_words.csv
    #
    # Subsequent runs will reuse the saved auth state, so you won't need to log in again
    # unless the session expires.
"""

import asyncio
import csv
import os
import re
from pathlib import Path
from typing import Dict, Tuple

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, Language, Term


DUOLINGO_WORDS_URL = "https://www.duolingo.com/practice-hub/words"


async def _ensure_logged_in(page: Page, state_path: Path) -> None:
    """
    Ensure the user is logged into Duolingo.

    On first run (no auth state file), this function will:
      - Open the Duolingo Practice Hub words page.
      - Ask the user to complete login via the real browser window (Google SSO).
      - Wait for the words list to appear.
      - Save the authenticated storage state to disk.
    On subsequent runs, the context is already created with storage_state so no extra work is needed.
    """
    # Navigate to the words page; if not logged in, Duolingo will redirect to login.
    await page.goto(DUOLINGO_WORDS_URL, wait_until="networkidle")

    if state_path.exists():
        # Auth state is already loaded in the context; nothing else to do.
        return

    print(
        "\nA browser window should now be open.\n"
        "1) Log in to Duolingo (use 'Continue with Google' for SSO).\n"
        "2) After login, you should end up on the Practice Hub 'Words' page.\n"
        "3) Wait until the list of words is visible.\n"
        "4) Then return here and press Enter to continue so the session can be saved.\n"
    )
    # Blocking input is acceptable here since this script is run interactively.
    input("Press Enter here once you have finished logging in and see your words list... ")

    # Save the authenticated storage state for reuse.
    await page.context.storage_state(path=str(state_path))
    print(f"Saved Duolingo auth state to {state_path}")


async def _get_expected_word_count(page: Page) -> int:
    """
    Read the total number of words from the h2 heading, e.g. '427 words'.
    """
    # Grab all h2 elements and look for one containing 'words'
    headers = page.locator("h2")
    count = await headers.count()
    for i in range(count):
        text = await headers.nth(i).text_content()
        if not text:
            continue
        # Look for something like "427 words"
        if "word" in text.lower():
            m = re.search(r"(\d+)", text)
            if m:
                value = int(m.group(1))
                print(f"Expected number of words from header: {value}")
                return value

    raise RuntimeError("Could not find a header with the total number of words (e.g. '427 words').")


async def _scrape_visible_words(page: Page) -> Dict[str, str]:
    """
    Scrape all currently visible word cards on the page.

    Each card is structurally similar to:
        <div>
          <h3>رَبيع</h3>
          <p>spring</p>
        </div>

    Returns a dict mapping target_language_term -> english_term.
    """
    words: Dict[str, str] = {}

    # XPath pattern based on the structure you provided, but without the index on li.
    item_locator = page.locator(
        'xpath=//*[@id="root"]/div[2]/div/div[3]/div/div[2]/div/section[2]/ul/li/div/div'
    )

    count = await item_locator.count()
    for i in range(count):
        container = item_locator.nth(i)
        h3 = await container.locator("h3").first.text_content()
        p = await container.locator("p").first.text_content()
        if not h3 or not p:
            continue
        target = h3.strip()
        english = p.strip()
        if target and english:
            words[target] = english

    print(f"Scraped {len(words)} visible word entries from the current page view.")
    return words


async def _click_more_if_available(page: Page) -> bool:
    """
    Scroll to the bottom of the page and click a 'More' button if present.

    Returns True if a click was performed, False if no button was found.
    """
    # Scroll to bottom to reveal the button (if it's lazy-loaded).
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(1000)

    # Try a few different selectors to be resilient to minor text changes.
    # Primary: a button with accessible name matching /more/i.
    more_button = page.get_by_role("button", name=re.compile("more", re.IGNORECASE))
    if await more_button.count() > 0 and await more_button.first.is_enabled():
        print("Clicking 'More' button (by role).")
        await more_button.first.click()
        await page.wait_for_timeout(2000)
        return True

    # Fallback: any button whose text contains 'More'.
    buttons = page.locator("button")
    btn_count = await buttons.count()
    for i in range(btn_count):
        txt = await buttons.nth(i).text_content()
        if txt and "more" in txt.lower():
            print("Clicking 'More' button (by text fallback).")
            await buttons.nth(i).click()
            await page.wait_for_timeout(2000)
            return True

    print("No 'More' button found; assuming we've reached the end of the list.")
    return False


async def scrape_duolingo_words() -> Dict[str, str]:
    """
    Main scraping routine.

    Returns a mapping {target_language_term: english_term}.
    """
    script_dir = Path(__file__).resolve().parent
    state_path = script_dir / "duolingo_auth_state.json"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)

        # If we already have saved auth state, create the context with it; otherwise, start fresh.
        if state_path.exists():
            context = await browser.new_context(storage_state=str(state_path))
        else:
            context = await browser.new_context()

        page = await context.new_page()

        # Ensure the user is logged in (interactive on first run).
        await _ensure_logged_in(page, state_path)

        # We should now be on the words page (or at least able to navigate to it as an authenticated user).
        await page.goto(DUOLINGO_WORDS_URL, wait_until="networkidle")

        expected_total = await _get_expected_word_count(page)

        collected: Dict[str, str] = {}
        last_count = -1
        stagnation_rounds = 0

        while True:
            # Scrape currently visible words.
            current = await _scrape_visible_words(page)
            collected.update(current)
            print(f"Collected so far: {len(collected)} / {expected_total}")

            if len(collected) >= expected_total:
                print("Reached or exceeded expected word count; stopping.")
                break

            if len(collected) == last_count:
                stagnation_rounds += 1
            else:
                stagnation_rounds = 0
            last_count = len(collected)

            if stagnation_rounds >= 3:
                print("No new words found after several rounds; stopping to avoid infinite loop.")
                break

            # Try to load more words.
            clicked = await _click_more_if_available(page)
            if not clicked:
                break

        await context.close()
        await browser.close()

    print(f"Final collected count: {len(collected)} (expected {expected_total})")
    return collected


def save_words_to_file(words: Dict[str, str]) -> Path:
    """
    Save scraped words into `data/duolingo_words.csv` under the project root.
    """
    project_root = Path(__file__).resolve().parents[1]
    data_dir = project_root / "data"
    data_dir.mkdir(exist_ok=True)

    out_path = data_dir / "duolingo_words.csv"
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["target_language_term", "english_term"])
        for target, english in sorted(words.items()):
            writer.writerow([target, english])

    print(f"Saved scraped words to {out_path}")
    return out_path


def save_words_to_database(words: Dict[str, str]) -> Tuple[int, int]:
    """
    Insert scraped words into the database as `Term` records for Arabic.

    Returns (added_count, skipped_existing_count).
    """
    load_dotenv()
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set. Please configure it in your environment or .env file.")

    engine = create_engine(db_url)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Ensure tables exist.
    Base.metadata.create_all(bind=engine)

    session = SessionLocal()
    added = 0
    skipped = 0

    try:
        # Ensure Arabic language exists.
        lang = session.query(Language).filter(Language.code == "ar").first()
        if not lang:
            lang = Language(code="ar", name="Arabic")
            session.add(lang)
            session.commit()
            session.refresh(lang)

        for target, english in words.items():
            existing = (
                session.query(Term)
                .filter(
                    Term.language_id == lang.id,
                    Term.english_term == english,
                    Term.target_language_term == target,
                )
                .first()
            )

            if existing:
                skipped += 1
                continue

            term = Term(
                language_id=lang.id,
                english_term=english,
                target_language_term=target,
                transliteration=None,
                example_sentence=None,
                example_sentence_explained=None,
                notes="Imported from Duolingo Practice Hub",
                learned=False,
                correct_counter=0,
            )
            session.add(term)
            added += 1

        session.commit()

    finally:
        session.close()

    return added, skipped


async def main_async() -> None:
    words = await scrape_duolingo_words()
    save_words_to_file(words)
    added, skipped = save_words_to_database(words)
    print(f"Done. Added {added} new terms, skipped {skipped} existing terms.")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()

