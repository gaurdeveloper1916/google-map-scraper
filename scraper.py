import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from playwright.sync_api import sync_playwright


# =====================================================
# DATA MODEL
# =====================================================

@dataclass
class Place:
    name: str = ""
    address: str = ""
    website: str = ""
    phone_number: str = ""
    email: str = ""
    reviews_count: Optional[int] = None
    reviews_average: Optional[float] = None
    place_type: str = ""
    introduction: str = ""


# =====================================================
# HELPERS
# =====================================================

def extract_text(page, selector: str) -> str:
    try:
        loc = page.locator(selector)
        if loc.count() > 0:
            return loc.first.inner_text()
    except Exception:
        pass
    return ""


def extract_emails(text: str) -> List[str]:
    return list(set(re.findall(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        text
    )))


# =====================================================
# EXTRACT PLACE
# =====================================================

def extract_place(page, context, logger: logging.Logger = None) -> Place:
    place = Place()

    place.name        = extract_text(page, "h1.DUwDvf")
    place.address     = extract_text(page, 'button[data-item-id="address"]')
    place.phone_number = extract_text(page, 'button[data-item-id*="phone"]')
    place.place_type  = extract_text(page, "button.DkEaL")
    place.introduction = extract_text(page, ".PYvSYb")

    try:
        w = page.locator('a[data-item-id="authority"]')
        if w.count() > 0:
            place.website = w.first.get_attribute("href")
    except Exception as e:
        if logger:
            logger.debug(f"Website extraction failed: {e}")

    try:
        r = extract_text(page, 'div.F7nice span[aria-hidden="true"]')
        if r:
            place.reviews_average = float(r.replace(",", "."))
    except Exception as e:
        if logger:
            logger.debug(f"Rating extraction failed: {e}")

    try:
        rc = extract_text(page, 'span[aria-label*="reviews"]')
        nums = re.findall(r"\d+", rc.replace(",", "")) if rc else []
        if nums:
            place.reviews_count = int(nums[0])
    except Exception as e:
        if logger:
            logger.debug(f"Review count extraction failed: {e}")

    if place.website:
        try:
            web = context.new_page()
            web.goto(place.website, timeout=30000)
            web.wait_for_load_state("domcontentloaded", timeout=10000)
            emails = extract_emails(web.content())
            if emails:
                place.email = ", ".join(emails)
            web.close()
        except Exception as e:
            if logger:
                logger.debug(f"Email extraction failed for {place.website}: {e}")

    return place


# =====================================================
# CORE SCRAPER
# =====================================================

def scrape_places(
    search_for: str,
    total: int,
    logger: logging.Logger = None,
) -> List[Place]:
    """
    Scrape Google Maps for `search_for`, up to `total` listings.
    Pass a logger to receive real-time progress messages.
    Falls back to the root logger if none is provided.
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    places: List[Place] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            timeout=120000,  # 2 min launch timeout
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",   # avoids /dev/shm exhaustion in Docker
                "--disable-gpu",
                "--no-zygote",
                "--single-process",          # important for constrained containers
                "--disable-extensions",
            ],
        )
        context = browser.new_context()
        page = context.new_page()

        try:
            url = (
                "https://www.google.com/maps/search/"
                + search_for.replace(" ", "+")
            )
            logger.info(f"Opening {url}")

            page.goto(url, timeout=120000)
            page.wait_for_selector('a[href*="/maps/place"]', timeout=60000)

            feed = page.locator('//div[@role="feed"]')
            prev = 0
            same = 0

            # Scroll until we have enough listings or hit the end
            while True:
                feed.evaluate("(el) => el.scrollBy(0, 4000)")
                page.wait_for_timeout(800)

                found = page.locator('a[href*="/maps/place"]').count()
                logger.info(f"Found: {found}")

                if found >= total:
                    break
                if found == prev:
                    same += 1
                else:
                    same = 0
                if same >= 5:
                    break
                prev = found

            listings = page.locator('a[href*="/maps/place"]').all()[:total]
            logger.info(f"Total listings: {len(listings)}")

            for i, item in enumerate(listings):
                try:
                    item.click()
                    page.wait_for_selector("h1.DUwDvf", timeout=15000)

                    place = extract_place(page, context, logger)
                    if place.name:
                        places.append(place)
                        logger.info(f"Saved: {place.name}")

                except Exception as e:
                    logger.warning(f"Error on listing {i}: {e}")

        finally:
            browser.close()

    return places
