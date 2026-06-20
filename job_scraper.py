#!/usr/bin/env python3
"""
Remote Job Scraper -> Google Sheets

Pulls remote job listings from multiple sources and appends new postings
(Job Title, Company, Link, Date Added) to a Google Sheet, skipping anything
already there so you never get duplicates.

See README.md for setup instructions (Google Sheets credentials, scheduling).
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone

import requests
import feedparser
import gspread
from google.oauth2.service_account import Credentials

# ============================================================
# CONFIG — edit these or set as environment variables
# ============================================================

SHEET_ID = os.environ.get("JOB_SHEET_ID", "PASTE_YOUR_SHEET_ID_HERE")
SHEET_TAB = os.environ.get("JOB_SHEET_TAB", "Jobs")
CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDS_FILE", "service_account.json")

# Turn sources on/off here. builtin/hiringcafe/jobright are templates —
# read the README before flipping them to True.
SOURCES_ENABLED = {
    "remoteok": True,
    "remotive": True,
    "weworkremotely": True,
    "builtin": False,
    "hiringcafe": False,
    "jobright": False,
}

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; PersonalJobAggregator/1.0)"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("job_scraper")


# ============================================================
# SOURCE FETCHERS — each returns a list of {title, company, link}
# ============================================================

def fetch_remoteok():
    """RemoteOK public JSON API: https://remoteok.com/api"""
    jobs = []
    try:
        r = requests.get("https://remoteok.com/api", headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        for item in data:
            if not isinstance(item, dict) or "id" not in item:
                continue  # first element is metadata/legal notice, skip it
            url = item.get("url", "") or ""
            link = "https://remoteok.com" + url if url.startswith("/") else url
            jobs.append({
                "title": (item.get("position") or "").strip(),
                "company": (item.get("company") or "").strip(),
                "link": link.strip(),
            })
    except Exception as e:
        log.warning(f"RemoteOK fetch failed: {e}")
    return jobs


def fetch_remotive():
    """Remotive public API: https://remotive.com/api/remote-jobs"""
    jobs = []
    try:
        r = requests.get("https://remotive.com/api/remote-jobs", headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        for item in data.get("jobs", []):
            jobs.append({
                "title": (item.get("title") or "").strip(),
                "company": (item.get("company_name") or "").strip(),
                "link": (item.get("url") or "").strip(),
            })
    except Exception as e:
        log.warning(f"Remotive fetch failed: {e}")
    return jobs


def fetch_weworkremotely():
    """We Work Remotely RSS feeds (multiple categories)."""
    feeds = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-design-jobs.rss",
        "https://weworkremotely.com/categories/remote-customer-support-jobs.rss",
        "https://weworkremotely.com/categories/remote-sales-and-marketing-jobs.rss",
        "https://weworkremotely.com/categories/remote-product-jobs.rss",
        "https://weworkremotely.com/categories/all-other-remote-jobs.rss",
    ]
    jobs = []
    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                # WWR titles usually come as "Company: Job Title"
                title_raw = entry.get("title", "")
                if ":" in title_raw:
                    company, title = title_raw.split(":", 1)
                else:
                    company, title = "", title_raw
                jobs.append({
                    "title": title.strip(),
                    "company": company.strip(),
                    "link": entry.get("link", "").strip(),
                })
        except Exception as e:
            log.warning(f"We Work Remotely fetch failed for {feed_url}: {e}")
    return jobs


def fetch_builtin():
    """
    TEMPLATE — not finished. BuiltIn has no documented public API.
    To make this work:
      1. Open builtin.com/jobs/remote in a browser, open DevTools -> Network -> XHR.
      2. Look for a request returning JSON job results as you scroll/search.
      3. Replace the code below with a requests.get()/post() to that endpoint,
         and map its fields into {title, company, link}.
    Until then this returns nothing, by design (no guessing at endpoints).
    """
    log.info("builtin: skipped — template not yet wired up to a real endpoint.")
    return []


def fetch_hiringcafe():
    """
    TEMPLATE — not finished. Same approach as fetch_builtin():
    inspect hiring.cafe's network requests in DevTools to find its
    search/results endpoint, then implement the request + field mapping here.
    """
    log.info("hiringcafe: skipped — template not yet wired up to a real endpoint.")
    return []


def fetch_jobright():
    """
    TEMPLATE — not finished. Same approach as above for jobright.ai.
    """
    log.info("jobright: skipped — template not yet wired up to a real endpoint.")
    return []


SOURCE_FUNCS = {
    "remoteok": fetch_remoteok,
    "remotive": fetch_remotive,
    "weworkremotely": fetch_weworkremotely,
    "builtin": fetch_builtin,
    "hiringcafe": fetch_hiringcafe,
    "jobright": fetch_jobright,
}


# ============================================================
# GOOGLE SHEETS
# ============================================================

def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_TAB, rows=1000, cols=4)
        ws.append_row(["Job Title", "Company", "Link", "Date Added"])
    return ws


def get_existing_links(ws):
    """Links already in the sheet, so we never post the same job twice."""
    try:
        values = ws.col_values(3)  # column C = Link
        return set(v.strip() for v in values if v.strip())
    except Exception:
        return set()


def append_jobs(ws, jobs):
    existing = get_existing_links(ws)
    seen_in_batch = set()
    new_rows = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for job in jobs:
        link = (job.get("link") or "").strip()
        if not link or link in existing or link in seen_in_batch:
            continue
        seen_in_batch.add(link)
        new_rows.append([job.get("title", ""), job.get("company", ""), link, today])

    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")
    return len(new_rows)


# ============================================================
# MAIN
# ============================================================

def main():
    all_jobs = []
    for source, enabled in SOURCES_ENABLED.items():
        if not enabled:
            continue
        log.info(f"Fetching from {source}...")
        jobs = SOURCE_FUNCS[source]()
        log.info(f"  -> {len(jobs)} job(s) found")
        all_jobs.extend(jobs)
        time.sleep(1)  # small pause between sources, good etiquette

    log.info(f"Total jobs collected: {len(all_jobs)}")

    if SHEET_ID == "PASTE_YOUR_SHEET_ID_HERE":
        log.error("Set JOB_SHEET_ID (env var) or edit SHEET_ID in this file. See README.")
        sys.exit(1)

    if not os.path.exists(CREDENTIALS_FILE):
        log.error(f"Missing credentials file: {CREDENTIALS_FILE}. See README for setup.")
        sys.exit(1)

    ws = get_sheet()
    added = append_jobs(ws, all_jobs)
    log.info(f"Done. Added {added} new job(s) to the sheet.")


if __name__ == "__main__":
    main()
