from flask import Flask
from playwright.sync_api import sync_playwright
import re
import os
from datetime import datetime
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

def get_latest_event_urls(page, limit=40):
    page.goto(MAIN_EVENTS_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(7000)

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

    # Phrases that indicate invalid or non-specific addresses
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
    # Extract event ID from URL
    match = re.search(r"ItemID=(\d+)", url, re.IGNORECASE)
    if not match:
        match = re.search(r"ItemId=(\d+)", url, re.IGNORECASE)

    return match.group(1) if match else ""


def parse_event_times(when_text):
    # Parse date and time range from "when" text
    text = clean(when_text).replace("מתי?", "").strip()

    dates = re.findall(r"\d{1,2}\.\d{1,2}\.\d{2,4}", text)
    times = re.findall(r"\d{1,2}:\d{2}", text)

    if not dates:
        return 0, 0

    start_date = dates[0]
    end_date = dates[1] if len(dates) > 1 else dates[0]

    start_time = times[0] if len(times) > 0 else "00:00"
    end_time = times[1] if len(times) > 1 else "23:59"

    def to_millis(date_str, time_str):
        dt = date_parser.parse(f"{date_str} {time_str}", dayfirst=True)
        return int(dt.timestamp() * 1000)

    return to_millis(start_date, start_time), to_millis(end_date, end_time)


def clean_address(where_text):
    # Clean address text from unwanted UI strings
    address = clean(where_text)
    address = address.replace("איפה?", "").strip()
    address = address.replace("להצגת מיקום על גבי מפה >>", "").strip()
    return address


def clean(text):
    # Normalize whitespace in text
    if not text:
        return ""
    return " ".join(text.split()).strip()


def shorten_description(description, max_chars=700):
    # Limit description length while preserving readability
    description = clean(description)

    if len(description) <= max_chars:
        return description

    return description[:max_chars].rstrip() + "...\nFor more details, please open the event link."


def get_block_by_heading(page, heading):
    # Extract text block based on section heading
    locator = page.locator(f'h3:has-text("{heading}")').first

    if locator.count() == 0:
        return ""

    parent = locator.locator("xpath=..")
    return clean(parent.inner_text())


def get_text_by_title(page, title_text):
    # Extract text based on label/title in the page
    title = page.locator(f'text="{title_text}"').first

    if title.count() == 0:
        return ""

    try:
        parent = title.locator("xpath=..")
        return clean(parent.inner_text()).replace(title_text, "").strip()
    except:
        return ""


def scrape_event_detail(page, url):
    # Navigate to event page and extract all relevant fields
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)

    title = ""

    # First attempt: og:title (usually contains full event name)
    if page.locator('meta[property="og:title"]').count() > 0:
        title = clean(page.locator('meta[property="og:title"]').first.get_attribute("content") or "")

    # Second attempt: page <title>
    if not title and page.locator("title").count() > 0:
        title = clean(page.locator("title").first.inner_text())

    # Last fallback: <h1>
    if not title and page.locator("h1").count() > 0:
        title = clean(page.locator("h1").first.inner_text())

    # Remove anything after "|" (site branding, extra text, etc.)
    if "|" in title:
        title = title.split("|")[0].strip()

    when_text = get_block_by_heading(page, "מתי")
    where_text = get_block_by_heading(page, "איפה")
    description = get_text_by_title(page, "תיאור")
    description = shorten_description(description)

    image_url = ""

    # First attempt: og:image
    if page.locator('meta[property="og:image"]').count() > 0:
        image_url = page.locator('meta[property="og:image"]').first.get_attribute("content") or ""

    # Second attempt: image inside page content
    if not image_url:
        img = page.locator("div img[src*='digitelimages']").first
        if img.count() > 0:
            image_url = img.get_attribute("src") or ""

    # Convert to absolute URL if needed
    if image_url.startswith("/"):
        image_url = "https://www.tel-aviv.gov.il" + image_url

    external_id = extract_item_id(url)
    date_time_millis, end_time_millis = parse_event_times(when_text)
    address = clean_address(where_text)
    lat, lng = geocode_address(address)
    current_time = int(datetime.now().timestamp() * 1000)

    # Determine if event is still active
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
        event["createdAt"] = int(datetime.now().timestamp() * 1000)
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
    current_time = int(datetime.now().timestamp() * 1000)

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