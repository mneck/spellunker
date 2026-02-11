import asyncio
import json
import re
from pathlib import Path
from typing import Dict, Tuple

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError


URL = "https://www.duolingo.com/practice-hub/words"
CDP_ENDPOINT = "http://127.0.0.1:9222"

OUT_DIR = Path("data")
OUT_PATH = OUT_DIR / "duolingo_words.jsonl"


def parse_expected_count(text: str) -> int:
    # e.g. "427 words"
    m = re.search(r"(\d[\d,]*)", text)
    if not m:
        return -1
    return int(m.group(1).replace(",", ""))


async def get_expected_count(page) -> int:
    # Look for an h2 containing "words"
    h2 = page.locator("h2", has_text="words").first
    try:
        txt = (await h2.inner_text()).strip()
        return parse_expected_count(txt)
    except Exception:
        return -1


async def extract_visible_words(page) -> Dict[Tuple[str, str], None]:
    """
    Duolingo uses randomized classnames, so we anchor on structure:
    list items that contain an <h3> (word) and a <p> (translation).
    """
    items = page.locator("section ul li")
    count = await items.count()

    results: Dict[Tuple[str, str], None] = {}
    for i in range(count):
        li = items.nth(i)
        h3 = li.locator("h3").first
        p = li.locator("p").first
        try:
            word = (await h3.inner_text()).strip()
            trans = (await p.inner_text()).strip()
        except Exception:
            continue

        # Basic sanity: skip empties
        if word and trans:
            results[(word, trans)] = None

    return results


async def click_more_if_possible(page) -> bool:
    """
    Scroll down and click the button that loads more, if present.
    Duolingo label varies; we try a few common text matches.
    """
    # Scroll the main window to the bottom to reveal any lazy "more" control.
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(750)

    # 1) Try an accessible button with a name containing "more" (case‑insensitive).
    try:
        btn = page.get_by_role("button", name=re.compile("more", re.IGNORECASE))
        if await btn.count() > 0 and await btn.first.is_visible():
            print("Clicking 'More' via ARIA role/name.")
            await btn.first.click()
            await page.wait_for_timeout(1200)
            return True
    except Exception:
        pass

    # 2) Try common button text variants (case‑sensitive CSS :has-text).
    selector_variants = [
        "button:has-text('More')",
        "button:has-text('more')",
        "button:has-text('Show more')",
        "button:has-text('Load more')",
        "button:has-text('See more')",
    ]
    for sel in selector_variants:
        loc = page.locator(sel).first
        try:
            if await loc.is_visible():
                print(f"Clicking 'More' via selector: {sel}")
                await loc.click()
                await page.wait_for_timeout(1200)
                return True
        except Exception:
            continue

    # 3) Fallback: any element inside the words section whose text contains "more".
    try:
        words_section = page.locator("section", has=page.locator("ul li h3")).first
        text_more = words_section.get_by_text(re.compile("more", re.IGNORECASE))
        if await text_more.count() > 0 and await text_more.first.is_visible():
            print("Clicking 'More' via generic text match inside words section.")
            await text_more.first.click()
            await page.wait_for_timeout(1200)
            return True
    except Exception:
        pass

    print("No 'More' control found on the page.")
    return False


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_ENDPOINT)

        # Reuse the first existing context if available (your Brave profile)
        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = await browser.new_context()

        # Find an existing tab with the URL, otherwise open a new tab
        page = None
        for existing in context.pages:
            if existing.url.startswith(URL):
                page = existing
                break
        if page is None:
            page = await context.new_page()
            await page.goto(URL, wait_until="domcontentloaded")

        # If you're not logged in, this page will look wrong.
        # We don't automate SSO; we just wait for the list to appear.
        try:
            await page.wait_for_selector("section ul li h3", timeout=20_000)
        except PlaywrightTimeoutError:
            raise SystemExit(
                "Couldn't find the word list. Make sure you're logged into Duolingo in the Brave CDP window "
                "and that the Words page is fully loaded."
            )

        expected = await get_expected_count(page)
        print(f"Expected count (from h2): {expected if expected != -1 else 'unknown'}")

        collected: Dict[Tuple[str, str], None] = {}
        stagnant_rounds = 0

        while True:
            visible = await extract_visible_words(page)
            before = len(collected)
            collected.update(visible)
            after = len(collected)

            print(f"Collected: {after} (added {after - before})")

            # Stop condition if we know the expected count
            if expected != -1 and after >= expected:
                print("Reached expected count.")
                break

            # Try to load more
            loaded_more = await click_more_if_possible(page)

            # If no "more" button (or it stopped working), attempt one more scroll+wait
            if not loaded_more:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)

            # Detect stagnation (no new items appearing)
            if after == before:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0

            if stagnant_rounds >= 3:
                print("No new words detected after multiple attempts. Stopping.")
                break

        # Write output
        with OUT_PATH.open("w", encoding="utf-8") as f:
            for word, trans in sorted(collected.keys()):
                f.write(json.dumps({"word": word, "translation": trans}, ensure_ascii=False) + "\n")

        print(f"Wrote {len(collected)} items to {OUT_PATH}")

        # Optional: enforce exact match if expected is known
        if expected != -1 and len(collected) != expected:
            print(f"WARNING: expected {expected}, got {len(collected)}. Some items may not have loaded.")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())

