from flask import Flask
from playwright.sync_api import sync_playwright
import re
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from dateutil import parser as date_parser

import requests
import firebase_admin
from firebase_admin import credentials, firestore


cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred)

db = firestore.client()
app = Flask(__name__)

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
MAIN_EVENTS_URL = "https://www.tel-aviv.gov.il/Visitors/Events/Pages/Events.aspx"
ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")


def get_current_time_millis():
    return int(datetime.now(ISRAEL_TZ).timestamp() * 1000)


def get_latest_event_urls(page, limit=40):
    page_loaded = False

    for attempt in range(3):
        try:
            print(f"Opening main events page, attempt {attempt + 1}/3")

            page.goto(MAIN_EVENTS_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(7000)

            page_loaded = True
            break

        except Exception as e:
            print(f"Failed to open main events page on attempt {attempt + 1}:", e)

            if attempt < 2:
                print("Waiting 10 seconds before retry...")
                page.wait_for_timeout(10000)

    if not page_loaded:
        print("Could not open main events page after 3 attempts")
        return []

    links = page.locator("a[href*='MainItemPage.aspx'][href*='ItemID=']")

    urls = []
    seen_ids = set()

    for i in range(links.count()):
        href = links.nth(i).get_attribute("href")

        if not href:
            continue

        if href.startswith("/"):
            href = "https://www.tel-aviv.gov.il" + href

        external_id = extract_item_id(href)

        if not external_id or external_id in seen_ids:
            continue

        seen_ids.add(external_id)
        urls.append(href)

        if len(urls) == limit:
            break

    return urls


def geocode_address(address):
    if not address:
        return 0.0, 0.0

    invalid_phrases = [
        "כמפורט בכתבה",
        "ברחבי העיר"
    ]

    for phrase in invalid_phrases:
        if phrase in address:
            return 0.0, 0.0

    query = f"{address}, ישראל"
    url = "https://maps.googleapis.com/maps/api/geocode/json"

    params = {
        "address": query,
        "key": GOOGLE_MAPS_API_KEY,
        "language": "he",
        "region": "il"
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        data = response.json()

        if data.get("status") != "OK":
            print("Google geocoding failed:", data.get("status"), address)
            return 0.0, 0.0

        location = data["results"][0]["geometry"]["location"]
        return location["lat"], location["lng"]

    except Exception as e:
        print("Google geocoding exception:", address, e)
        return 0.0, 0.0


def extract_item_id(url):
    match = re.search(r"ItemID=(\d+)", url, re.IGNORECASE)

    if not match:
        match = re.search(r"ItemId=(\d+)", url, re.IGNORECASE)

    return match.group(1) if match else ""


def parse_event_times(when_text):
    text = clean(when_text).replace("מתי?", "").strip()

    if not text:
        return 0, 0

    # If the page has "המועדים הקרובים", we only use the part before it
    # for the main date range. This prevents nearby dates from breaking the range.
    main_time_part = text

    if "המועדים הקרובים" in text:
        main_time_part = text.split("המועדים הקרובים", 1)[0].strip()

    dates = re.findall(r"\d{1,2}\.\d{1,2}\.\d{2,4}", main_time_part)
    times = re.findall(r"\d{1,2}:\d{2}", main_time_part)

    # Fallback: if no dates were found before "המועדים הקרובים",
    # search in the full text.
    if not dates:
        dates = re.findall(r"\d{1,2}\.\d{1,2}\.\d{2,4}", text)

    if not times:
        times = re.findall(r"\d{1,2}:\d{2}", text)

    if not dates:
        return 0, 0

    start_date = dates[0]
    end_date = dates[1] if len(dates) > 1 else dates[0]

    start_time = times[0] if len(times) > 0 else "00:00"
    end_time = times[1] if len(times) > 1 else "23:59"

    def to_millis(date_str, time_str):
        dt = date_parser.parse(f"{date_str} {time_str}", dayfirst=True)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ISRAEL_TZ)

        return int(dt.timestamp() * 1000)

    return to_millis(start_date, start_time), to_millis(end_date, end_time)


def clean_address(where_text):
    address = clean(where_text)
    address = address.replace("איפה?", "").strip()
    address = address.replace("להצגת מיקום על גבי מפה >>", "").strip()
    return address


def clean(text):
    if not text:
        return ""

    return " ".join(text.split()).strip()


def shorten_description(description, max_chars=700):
    description = clean(description)

    if len(description) <= max_chars:
        return description

    return description[:max_chars].rstrip() + "...\nFor more details, please open the event link."


def get_block_by_heading(page, heading):
    locator = page.locator(f'h3:has-text("{heading}")').first

    if locator.count() == 0:
        return ""

    parent = locator.locator("xpath=..")
    return clean(parent.inner_text())


def get_text_by_title(page, title_text):
    title = page.locator(f'text="{title_text}"').first

    if title.count() == 0:
        return ""

    try:
        parent = title.locator("xpath=..")
        return clean(parent.inner_text()).replace(title_text, "").strip()
    except Exception:
        return ""


def normalize_url(url):
    if not url:
        return ""

    url = url.strip()

    if url.startswith("//"):
        return "https:" + url

    if url.startswith("/"):
        return "https://www.tel-aviv.gov.il" + url

    return url


def first_from_srcset(srcset):
    if not srcset:
        return ""

    return srcset.split(",")[0].strip().split(" ")[0]


def get_image_src(img):
    return (
        img.get_attribute("src")
        or img.get_attribute("data-src")
        or img.get_attribute("data-original")
        or img.get_attribute("data-lazy-src")
        or first_from_srcset(img.get_attribute("srcset"))
        or ""
    )


def is_bad_image_url(url):
    if not url:
        return True

    lower_url = url.lower()

    bad_words = [
        "logo",
        "icon",
        "sprite",
        "facebook",
        "youtube",
        "instagram",
        "whatsapp",
        "accessibility"
    ]

    return any(word in lower_url for word in bad_words)


def get_event_image_url(page):
    images = page.locator("img")

    best_image_url = ""
    best_score = 0

    for i in range(images.count()):
        img = images.nth(i)

        try:
            box = img.bounding_box()
        except Exception:
            box = None

        if not box:
            continue

        width = box.get("width", 0)
        height = box.get("height", 0)
        y = box.get("y", 99999)

        # Prefer large images near the top of the event page
        if width < 250 or height < 120:
            continue

        if y > 900:
            continue

        image_url = normalize_url(get_image_src(img))

        if not image_url:
            continue

        if is_bad_image_url(image_url):
            continue

        score = (width * height) - (y * 10)

        if score > best_score:
            best_score = score
            best_image_url = image_url

    if best_image_url:
        return best_image_url

    # Fallback: official share image
    if page.locator('meta[property="og:image"]').count() > 0:
        image_url = normalize_url(
            page.locator('meta[property="og:image"]').first.get_attribute("content") or ""
        )

        if image_url and not is_bad_image_url(image_url):
            return image_url

    # Fallback: Tel Aviv image path
    img = page.locator("div img[src*='digitelimages']").first

    if img.count() > 0:
        image_url = normalize_url(get_image_src(img))

        if image_url and not is_bad_image_url(image_url):
            return image_url

    return ""


def open_event_page(page, url):
    for attempt in range(3):
        try:
            print(f"Opening event page, attempt {attempt + 1}/3:", url)

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            return True

        except Exception as e:
            print(f"Failed to open event page on attempt {attempt + 1}:", url, e)

            if attempt < 2:
                print("Waiting 10 seconds before retry...")
                page.wait_for_timeout(10000)

    return False


def scrape_event_detail(page, url):
    page_loaded = open_event_page(page, url)

    if not page_loaded:
        raise Exception(f"Could not open event page after retries: {url}")

    title = ""

    if page.locator('meta[property="og:title"]').count() > 0:
        title = clean(page.locator('meta[property="og:title"]').first.get_attribute("content") or "")

    if not title and page.locator("title").count() > 0:
        title = clean(page.locator("title").first.inner_text())

    if not title and page.locator("h1").count() > 0:
        title = clean(page.locator("h1").first.inner_text())

    if "|" in title:
        title = title.split("|")[0].strip()

    when_text = get_block_by_heading(page, "מתי")
    where_text = get_block_by_heading(page, "איפה")

    description = get_text_by_title(page, "תיאור")
    description = shorten_description(description)

    image_url = get_event_image_url(page)

    external_id = extract_item_id(url)
    date_time_millis, end_time_millis = parse_event_times(when_text)

    address = clean_address(where_text)
    lat, lng = geocode_address(address)

    current_time = get_current_time_millis()

    is_active = not (end_time_millis > 0 and end_time_millis < current_time)

    event = {
        "id": f"digitel_{external_id}",
        "producerId": "tel_aviv_municipality",
        "imageUri": image_url,
        "name": title,
        "producer": "Tel Aviv Municipality",
        "dateTimeMillis": date_time_millis,
        "address": address,
        "description": description,
        "categories": ["Municipality"],
        "lat": lat,
        "lng": lng,
        "source": "TEL_AVIV_MUNICIPALITY",
        "maxParticipants": -1,
        "participants": [],
        "externalId": external_id,
        "sourceUrl": url,
        "endTimeMillis": end_time_millis,
        "isActive": is_active,
        "updatedAt": current_time
    }

    return event


def save_event_if_needed(event):
    doc_ref = db.collection("events").document(event["id"])
    doc = doc_ref.get()

    if not doc.exists:
        event["createdAt"] = get_current_time_millis()
        doc_ref.set(event, merge=True)
        print("Added:", event["name"])
        return "added"

    existing_event = doc.to_dict() or {}

    has_changes = any(
        existing_event.get(key) != value
        for key, value in event.items()
        if key != "updatedAt"
    )

    if has_changes:
        doc_ref.set(event, merge=True)
        print("Updated:", event["name"])
        return "updated"

    print("No changes:", event["name"])
    return "no_changes"


def add_new_events_until_existing(page, urls):
    for url in urls:
        event = scrape_valid_event(page, url)

        if event is None:
            continue

        doc_ref = db.collection("events").document(event["id"])
        doc = doc_ref.get()

        if doc.exists:
            print("Reached existing event, stopping new events scan:", event["name"])
            break

        save_event_if_needed(event)


def deactivate_expired_firestore_events():
    current_time = get_current_time_millis()

    docs = db.collection("events") \
        .where("isActive", "==", True) \
        .stream()

    for doc in docs:
        event = doc.to_dict() or {}
        end_time = event.get("endTimeMillis", 0)

        if end_time > 0 and end_time < current_time:
            doc.reference.update({
                "isActive": False,
                "updatedAt": current_time
            })
            print("Deactivated expired event:", event.get("name", doc.id))


def update_existing_tlv_events(page):
    docs = db.collection("events") \
        .where("source", "==", "TEL_AVIV_MUNICIPALITY") \
        .where("isActive", "==", True) \
        .stream()

    for doc in docs:
        existing_event = doc.to_dict() or {}
        source_url = existing_event.get("sourceUrl", "")

        if not source_url:
            print("Skipping TLV event without sourceUrl:", doc.id)
            continue

        event = scrape_valid_event(page, source_url)

        if event is None:
            continue

        save_event_if_needed(event)


def scrape_valid_event(page, url):
    try:
        event = scrape_event_detail(page, url)
    except Exception as e:
        print("Failed to scrape:", url, e)
        return None

    if event["dateTimeMillis"] == 0:
        print("Skipped non-event page:", event["name"])
        return None

    return event


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        latest_urls = get_latest_event_urls(page, limit=20)

        print(f"Found {len(latest_urls)} candidate urls")

        deactivate_expired_firestore_events()

        if not latest_urls:
            print("No candidate urls found. Skipping website-dependent updates.")
            browser.close()
            print("Scraper finished without website data")
            return

        update_existing_tlv_events(page)
        add_new_events_until_existing(page, latest_urls)

        browser.close()

        print("Scraper finished successfully")


@app.route("/", methods=["GET"])
def run_scraper():
    main()
    return "EventSpot scraper finished successfully", 200


if __name__ == "__main__":
    main()