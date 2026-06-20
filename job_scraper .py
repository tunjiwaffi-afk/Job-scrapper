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
import gspread
from google.oauth2.service_account import Credentials

# ============================================================
# CONFIG — edit these or set as environment variables
# ============================================================

SHEET_ID = os.environ.get("JOB_SHEET_ID", "PASTE_YOUR_SHEET_ID_HERE")
SHEET_TAB = os.environ.get("JOB_SHEET_TAB", "Jobs")
CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDS_FILE", "service_account.json")

# Turn sources on/off here.
SOURCES_ENABLED = {
    "hiringcafe": True,    # experimental, unofficial API -- see fetch_hiringcafe()
    "builtin": False,      # template, no documented public API -- see fetch_builtin()
    "jobright": False,     # not possible -- requires a logged-in account, see fetch_jobright()
    "linkedin": False,     # not possible -- ToS prohibits scraping, see fetch_linkedin()
    "ziprecruiter": False, # not possible -- ToS prohibits scraping, see fetch_ziprecruiter()
    "indeed": False,       # not possible -- ToS prohibits scraping, see fetch_indeed()
    "simplyhired": False,  # not possible -- ToS prohibits scraping, see fetch_simplyhired()
}

# Only keep jobs that look like they're remote AND restricted to/open to the USA.
# This is real structured filtering for hiring.cafe (sent directly in the API
# request), not text-guessing.
USA_REMOTE_ONLY = True

# Which sources need the best-effort text filter applied after fetching
# (vs. sources that already filter for USA-remote at the API request level).
NEEDS_TEXT_FILTER = {
    "hiringcafe": False,
    "builtin": False,
    "jobright": False,
    "linkedin": False,
    "ziprecruiter": False,
    "indeed": False,
    "simplyhired": False,
}

# Only keep jobs that look like genuine software developer/engineer roles.
# Best-effort title matching -- see looks_software_developer() below.
SOFTWARE_DEV_ONLY = True

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("job_scraper")


# ============================================================
# USA-REMOTE FILTERING (best-effort text matching)
# ============================================================
# Free job APIs mostly don't have a clean "candidate must be located in
# country X" field. This does simple keyword matching against whatever
# location-ish text a source provides. It will sometimes miss valid USA
# jobs (if the listing doesn't mention "USA" explicitly) and can very
# occasionally include a false positive. hiring.cafe is the one source
# with genuine structured filtering, since the country restriction is
# sent directly in the API request rather than guessed from text.

US_HINTS = [
    "usa", "u.s.a", "u.s.", "us only", "us-only", "united states",
    "remote (us)", "remote - us", "remote, us", "anywhere in the us",
    "anywhere in the usa", "usa only", "us based", "us-based",
]

NON_US_HINTS = [
    "worldwide", "anywhere in the world", "europe", "emea", "apac",
    "uk", "united kingdom", "canada only", "latam", "international",
    "global", "australia", "asia",
]


def looks_usa_remote(location_text):
    """Best-effort: True if text signals USA, and isn't dominated by a
    clearly non-US-only signal (e.g. "Worldwide", "Europe")."""
    if not location_text:
        return False
    text = location_text.lower()
    has_us = any(h in text for h in US_HINTS)
    has_non_us_only = any(h in text for h in NON_US_HINTS) and not has_us
    return has_us and not has_non_us_only


# ============================================================
# SOFTWARE DEVELOPER FILTERING (best-effort title matching)
# ============================================================
# Keeps roles whose title reads as an actual developer/engineer position,
# and excludes adjacent-but-different roles (QA, data, sales engineer,
# management, internships) even if "engineer" appears in the title.

DEV_EXCLUDE_HINTS = [
    "qa", "quality assurance", "test engineer", "sdet",
    "sales engineer", "support engineer", "data engineer",
    "data scientist", "machine learning", "devops", "site reliability",
    "security engineer", "network engineer", "hardware engineer",
    "manager", "director", "intern", "internship", "scrum master",
    "product manager", "project manager", "recruiter", "designer",
]


def looks_software_developer(title):
    """Best-effort: True if the title reads as a genuine software
    developer/engineer role, not an adjacent or unrelated one."""
    if not title:
        return False
    text = title.lower()
    if any(h in text for h in DEV_EXCLUDE_HINTS):
        return False
    return "developer" in text or ("software" in text and "engineer" in text)


# ============================================================
# SOURCE FETCHERS — each returns a list of {title, company, link}
# ============================================================

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


def _hc_extract(job):
    """
    hiring.cafe's job object shape isn't officially documented, so this
    tries several plausible field-name patterns defensively. If results
    come through with blank titles/companies, that means these guesses
    are wrong -- send Claude one raw job entry and the mapping can be
    corrected.
    """
    def first(*paths):
        for path in paths:
            val = job
            try:
                for key in path:
                    val = val[key]
                if val:
                    return val
            except (KeyError, TypeError, IndexError):
                continue
        return None

    title = first(("title",), ("job_title",), ("position",),
                   ("job_information", "title"),
                   ("v5_processed_job_data", "title"))
    company = first(("company_name",), ("company",),
                     ("company_information", "name"),
                     ("employer_name",))
    if isinstance(company, dict):
        company = company.get("name")
    link = first(("apply_url",), ("job_url",), ("url",), ("link",),
                 ("job_information", "apply_url"))

    return {
        "title": title.strip() if isinstance(title, str) else "",
        "company": company.strip() if isinstance(company, str) else "",
        "link": link.strip() if isinstance(link, str) else "",
    }


def fetch_hiringcafe():
    """
    EXPERIMENTAL. Uses hiring.cafe's internal (undocumented) search API,
    reverse-engineered from public reference scrapers -- not an official
    integration. Two real risks:
      1. hiring.cafe can change this API at any time with no warning.
      2. hiring.cafe is known to run Cloudflare bot protection. GitHub
         Actions runs from shared datacenter IPs that get blocked more
         often than a normal home/phone connection. If this consistently
         returns 0 jobs, that's most likely what's happening, and there's
         no free fix for it (paid anti-bot proxy services exist but cost
         money).
    Filtering to Remote + USA happens in the request itself (real
    structured filtering, not text-guessing), which is why this is the
    most reliable source for that specific requirement when it works.

    Known limitation: the seniorityLevel filter below is inherited as-is
    from the reference implementation this was built from, and currently
    skews toward entry/mid-level roles -- the exact accepted values for
    "Senior", "Director" etc. aren't confirmed. To fix this properly:
    open hiring.cafe in Safari, apply your real filters there (Remote +
    United States + whatever seniority you want), copy the resulting
    page URL, and send it over -- the real filter values can likely be
    decoded straight from that URL instead of guessed.
    """
    jobs = []
    jobs_endpoint = "https://hiring.cafe/api/search-jobs"

    hc_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": "https://hiring.cafe/",
        "Origin": "https://hiring.cafe",
    }

    search_state = {
        "locations": [{
            "formatted_address": "United States",
            "types": ["country"],
            "geometry": {"location": {"lat": "39.8283", "lon": "-98.5795"}},
            "id": "user_country",
            "address_components": [{
                "long_name": "United States",
                "short_name": "US",
                "types": ["country"],
            }],
            "options": {"flexible_regions": []},  # empty = don't broaden past USA
        }],
        "workplaceTypes": ["Remote"],  # remote only, not hybrid/onsite
        "defaultToUserLocation": False,
        "userLocation": None,
        "physicalEnvironments": ["Office", "Outdoor", "Vehicle", "Industrial", "Customer-Facing"],
        "physicalLaborIntensity": ["Low", "Medium", "High"],
        "physicalPositions": ["Sitting", "Standing"],
        "oralCommunicationLevels": ["Low", "Medium", "High"],
        "computerUsageLevels": ["Low", "Medium", "High"],
        "cognitiveDemandLevels": ["Low", "Medium", "High"],
        "currency": {"label": "Any", "value": None},
        "frequency": {"label": "Any", "value": None},
        "minCompensationLowEnd": None,
        "minCompensationHighEnd": None,
        "maxCompensationLowEnd": None,
        "maxCompensationHighEnd": None,
        "restrictJobsToTransparentSalaries": False,
        "calcFrequency": "Yearly",
        "commitmentTypes": ["Full Time", "Part Time", "Contract", "Internship", "Temporary", "Seasonal", "Volunteer"],
        "jobTitleQuery": "software developer",
        "jobDescriptionQuery": "",
        "associatesDegreeFieldsOfStudy": [],
        "excludedAssociatesDegreeFieldsOfStudy": [],
        "bachelorsDegreeFieldsOfStudy": [],
        "excludedBachelorsDegreeFieldsOfStudy": [],
        "mastersDegreeFieldsOfStudy": [],
        "excludedMastersDegreeFieldsOfStudy": [],
        "doctorateDegreeFieldsOfStudy": [],
        "excludedDoctorateDegreeFieldsOfStudy": [],
        "associatesDegreeRequirements": [],
        "bachelorsDegreeRequirements": [],
        "mastersDegreeRequirements": [],
        "doctorateDegreeRequirements": [],
        "licensesAndCertifications": [],
        "excludedLicensesAndCertifications": [],
        "excludeAllLicensesAndCertifications": False,
        "seniorityLevel": ["No Prior Experience Required", "Entry Level", "Mid Level"],
        "roleTypes": ["Individual Contributor", "People Manager"],
        "roleYoeRange": [0, 20],
        "excludeIfRoleYoeIsNotSpecified": False,
        "managementYoeRange": [0, 20],
        "excludeIfManagementYoeIsNotSpecified": False,
        "securityClearances": ["None", "Confidential", "Secret", "Top Secret", "Top Secret/SCI", "Public Trust", "Interim Clearances", "Other"],
        "languageRequirements": [],
        "excludedLanguageRequirements": [],
        "languageRequirementsOperator": "OR",
        "excludeJobsWithAdditionalLanguageRequirements": False,
        "airTravelRequirement": ["None", "Minimal", "Moderate", "Extensive"],
        "landTravelRequirement": ["None", "Minimal", "Moderate", "Extensive"],
        "morningShiftWork": [],
        "eveningShiftWork": [],
        "overnightShiftWork": [],
        "weekendAvailabilityRequired": "Doesn't Matter",
        "holidayAvailabilityRequired": "Doesn't Matter",
        "overtimeRequired": "Doesn't Matter",
        "onCallRequirements": ["None", "Occasional (once a month or less)", "Regular (once a week or more)"],
        "benefitsAndPerks": [],
        "applicationFormEase": [],
        "companyNames": [],
        "excludedCompanyNames": [],
        "usaGovPref": None,
        "industries": [],
        "excludedIndustries": [],
        "companyKeywords": [],
        "companyKeywordsBooleanOperator": "OR",
        "excludedCompanyKeywords": [],
        "hideJobTypes": [],
        "encouragedToApply": [],
        "searchQuery": "",
        "dateFetchedPastNDays": 30,
        "hiddenCompanies": [],
        "user": None,
        "searchModeSelectedCompany": None,
        "departments": [],
        "restrictedSearchAttributes": [],
        "sortBy": "default",
        "technologyKeywordsQuery": "",
        "requirementsKeywordsQuery": "",
        "companyPublicOrPrivate": "all",
        "latestInvestmentYearRange": [None, None],
        "latestInvestmentSeries": [],
        "latestInvestmentAmount": None,
        "latestInvestmentCurrency": [],
        "investors": [],
        "excludedInvestors": [],
        "isNonProfit": "all",
        "companySizeRanges": [],
        "minYearFounded": None,
        "maxYearFounded": None,
        "excludedLatestInvestmentSeries": [],
    }

    payload = {"size": 100, "page": 0, "searchState": search_state}

    try:
        r = requests.post(jobs_endpoint, json=payload, headers=hc_headers, timeout=20)
        if r.status_code != 200:
            log.warning(f"hiring.cafe returned status {r.status_code} (likely blocked or API changed)")
            return jobs

        data = r.json()
        raw_jobs = []
        if isinstance(data, dict):
            for key in ("results", "jobs", "data", "items", "content"):
                if isinstance(data.get(key), list):
                    raw_jobs = data[key]
                    break
            if not raw_jobs and isinstance(data.get("hits"), dict):
                hits = data["hits"].get("hits", [])
                raw_jobs = [h.get("_source", h) for h in hits]
        elif isinstance(data, list):
            raw_jobs = data

        for item in raw_jobs:
            mapped = _hc_extract(item)
            if mapped["link"]:
                jobs.append(mapped)

        if raw_jobs and not jobs:
            log.warning("hiring.cafe returned data but field mapping found nothing usable -- field names likely need adjusting.")
    except Exception as e:
        log.warning(f"hiring.cafe fetch failed: {e}")
    return jobs


def fetch_jobright():
    """
    NOT IMPLEMENTED -- and not really possible as an open scraper.
    Jobright.ai requires creating an account and logging in before any
    job listings are visible; there's no public, unauthenticated way to
    pull data from it. Building this would mean automating a login flow
    and managing a session/cookies, which is a different (and much more
    fragile and ToS-sensitive) kind of project than the other sources
    here. Left in as a placeholder in case that changes.
    """
    log.info("jobright: skipped — requires a logged-in account, no public API.")
    return []


def fetch_linkedin():
    """
    NOT IMPLEMENTED, by design. LinkedIn's Terms of Service explicitly
    prohibit scraping, and they actively enforce this both technically
    (bot detection) and legally (they have sued scrapers in the past).
    No workaround is built here -- this isn't a missing feature, it's a
    deliberate boundary. Left in as a disabled placeholder for clarity.
    """
    log.info("linkedin: skipped — scraping violates their Terms of Service.")
    return []


def fetch_ziprecruiter():
    """
    NOT IMPLEMENTED, by design. Same reasoning as LinkedIn: ZipRecruiter's
    Terms of Service prohibit scraping and they run active anti-bot
    protection. They offer an official partner API for legitimate use
    cases, which would be the correct path if this is ever revisited.
    """
    log.info("ziprecruiter: skipped — scraping violates their Terms of Service.")
    return []


def fetch_indeed():
    """
    NOT IMPLEMENTED, by design. Indeed's Terms of Service explicitly
    prohibit automated scraping, they run strong bot detection, and they
    have a history of pursuing legal action against scrapers. Indeed does
    offer official employer/partner API access through proper channels.
    """
    log.info("indeed: skipped — scraping violates their Terms of Service.")
    return []


def fetch_simplyhired():
    """
    NOT IMPLEMENTED, by design. SimplyHired is part of the same corporate
    family as Indeed (Recruit Holdings) and is treated the same way here
    -- its terms call for respecting its scraping restrictions, and
    third-party scraper tools for it explicitly note the same.
    """
    log.info("simplyhired: skipped — scraping violates their Terms of Service.")
    return []


SOURCE_FUNCS = {
    "hiringcafe": fetch_hiringcafe,
    "builtin": fetch_builtin,
    "jobright": fetch_jobright,
    "linkedin": fetch_linkedin,
    "ziprecruiter": fetch_ziprecruiter,
    "indeed": fetch_indeed,
    "simplyhired": fetch_simplyhired,
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
        log.info(f"  -> {len(jobs)} job(s) found before filtering")

        if USA_REMOTE_ONLY and NEEDS_TEXT_FILTER.get(source, False):
            jobs = [j for j in jobs if looks_usa_remote(j.get("_location", ""))]
            log.info(f"  -> {len(jobs)} job(s) after USA-remote filter")

        all_jobs.extend(jobs)
        time.sleep(1)  # small pause between sources, good etiquette

    log.info(f"Total jobs collected: {len(all_jobs)}")

    if SOFTWARE_DEV_ONLY:
        all_jobs = [j for j in all_jobs if looks_software_developer(j.get("title", ""))]
        log.info(f"Total jobs after software-developer filter: {len(all_jobs)}")

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
