#!/usr/bin/env python3
"""
Remote Job Scraper -> Google Sheets
"""

import os
import sys
import time
import logging
from datetime import datetime, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials

SHEET_ID = os.environ.get("JOB_SHEET_ID", "PASTE_YOUR_SHEET_ID_HERE")
SHEET_TAB = os.environ.get("JOB_SHEET_TAB", "Jobs")
CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDS_FILE", "service_account.json")

JOOBLE_API_KEY = os.environ.get("JOOBLE_API_KEY", "")
CAREERJET_API_KEY = os.environ.get("CAREERJET_API_KEY", "")

SEARCH_TERMS = ["software developer remote", "data engineer remote"]

SOURCES_ENABLED = {
    "hiringcafe": True,
    "jooble": True,
    "careerjet": False,
    "welcometothejungle": False,
    "builtin": False,
    "jobright": False,
    "linkedin": False,
    "ziprecruiter": False,
    "indeed": False,
    "simplyhired": False,
}

USA_REMOTE_ONLY = True

NEEDS_TEXT_FILTER = {
    "hiringcafe": False,
    "jooble": True,
    "careerjet": True,
    "welcometothejungle": True,
    "builtin": False,
    "jobright": False,
    "linkedin": False,
    "ziprecruiter": False,
    "indeed": False,
    "simplyhired": False,
}

SOFTWARE_DEV_ONLY = True

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("job_scraper")

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
    if not location_text:
        return False
    text = location_text.lower()
    has_us = any(h in text for h in US_HINTS)
    has_non_us_only = any(h in text for h in NON_US_HINTS) and not has_us
    return has_us and not has_non_us_only


DEV_EXCLUDE_HINTS = [
    "qa", "quality assurance", "test engineer", "sdet",
    "sales engineer", "support engineer",
    "data scientist", "machine learning", "devops", "site reliability",
    "security engineer", "network engineer", "hardware engineer",
    "manager", "director", "intern", "internship", "scrum master",
    "product manager", "project manager", "recruiter", "designer",
]


def looks_software_developer(title):
    if not title:
        return False
    text = title.lower()
    if any(h in text for h in DEV_EXCLUDE_HINTS):
        return False
    return "developer" in text or ("software" in text and "engineer" in text)


def looks_data_engineer(title):
    if not title:
        return False
    text = title.lower()
    if any(h in text for h in DEV_EXCLUDE_HINTS):
        return False
    return "data engineer" in text or "data engineering" in text


def looks_target_role(title):
    return looks_software_developer(title) or looks_data_engineer(title)


def fetch_jooble():
    jobs = []
    if not JOOBLE_API_KEY:
        log.info("jooble: skipped - JOOBLE_API_KEY not set.")
        return jobs
    url = f"https://jooble.org/api/{JOOBLE_API_KEY}"
    for term in SEARCH_TERMS:
        try:
            r = requests.post(
                url,
                json={"keywords": term, "location": "USA", "page": "1"},
                timeout=15,
            )
            if r.status_code != 200:
                log.warning(f"jooble: status {r.status_code} for term {term!r}")
                continue
            data = r.json()
            for item in data.get("jobs", []):
                jobs.append({
                    "title": (item.get("title") or "").strip(),
                    "company": (item.get("company") or "").strip(),
                    "link": (item.get("link") or "").strip(),
                    "_location": (item.get("location") or "").strip(),
                })
        except Exception as e:
            log.warning(f"jooble fetch failed for term {term!r}: {e}")
    return jobs


def fetch_careerjet():
    jobs = []
    if not CAREERJET_API_KEY:
        log.info("careerjet: skipped - CAREERJET_API_KEY not set.")
        return jobs
    url = "https://search.api.careerjet.net/v4/query"
    cj_headers = {"User-Agent": "Mozilla/5.0 (compatible; PersonalJobAggregator/1.0)"}
    for term in SEARCH_TERMS:
        try:
            params = {
                "keywords": term,
                "location": "USA",
                "locale_code": "en_US",
                "user_ip": "0.0.0.0",
                "user_agent": cj_headers["User-Agent"],
                "page_size": "50",
            }
            r = requests.get(
                url, params=params, headers=cj_headers,
                auth=(CAREERJET_API_KEY, ""), timeout=15,
            )
            if r.status_code != 200:
                log.warning(f"careerjet: status {r.status_code} for term {term!r}")
                continue
            data = r.json()
            for item in data.get("jobs", []):
                jobs.append({
                    "title": (item.get("title") or "").strip(),
                    "company": (item.get("company") or "").strip(),
                    "link": (item.get("url") or "").strip(),
                    "_location": (item.get("locations") or "").strip(),
                })
        except Exception as e:
            log.warning(f"careerjet fetch failed for term {term!r}: {e}")
    return jobs


def fetch_welcometothejungle():
    log.info("welcometothejungle: skipped - no verified working API access yet.")
    return []


def fetch_builtin():
    log.info("builtin: skipped - no documented public API.")
    return []


def _hc_extract(job):
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
    jobs = []
    jobs_endpoint = "https://hiring.cafe/api/search-jobs"

    hc_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": "https://hiring.cafe/",
        "Origin": "https://hiring.cafe",
    }

    base_search_state = {
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
            "options": {"flexible_regions": []},
        }],
        "workplaceTypes": ["Remote"],
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
        "jobTitleQuery": "",
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

    for term in SEARCH_TERMS:
        search_state = dict(base_search_state)
        search_state["jobTitleQuery"] = term
        payload = {"size": 100, "page": 0, "searchState": search_state}
        try:
            r = requests.post(jobs_endpoint, json=payload, headers=hc_headers, timeout=20)
            if r.status_code != 200:
                log.warning(f"hiring.cafe returned status {r.status_code} for term {term!r}")
                continue
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

            term_jobs = []
            for item in raw_jobs:
                mapped = _hc_extract(item)
                if mapped["link"]:
                    term_jobs.append(mapped)

            if raw_jobs and not term_jobs:
                log.warning("hiring.cafe: data returned but field mapping found nothing usable.")
            jobs.extend(term_jobs)
        except Exception as e:
            log.warning(f"hiring.cafe fetch failed for term {term!r}: {e}")

    return jobs


def fetch_jobright():
    log.info("jobright: skipped - requires a logged-in account.")
    return []


def fetch_linkedin():
    log.info("linkedin: skipped - scraping violates their Terms of Service.")
    return []


def fetch_ziprecruiter():
    log.info("ziprecruiter: skipped - scraping violates their Terms of Service.")
    return []


def fetch_indeed():
    log.info("indeed: skipped - scraping violates their Terms of Service.")
    return []


def fetch_simplyhired():
    log.info("simplyhired: skipped - scraping violates their Terms of Service.")
    return []


SOURCE_FUNCS = {
    "hiringcafe": fetch_hiringcafe,
    "jooble": fetch_jooble,
    "careerjet": fetch_careerjet,
    "welcometothejungle": fetch_welcometothejungle,
    "builtin": fetch_builtin,
    "jobright": fetch_jobright,
    "linkedin": fetch_linkedin,
    "ziprecruiter": fetch_ziprecruiter,
    "indeed": fetch_indeed,
    "simplyhired": fetch_simplyhired,
}


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
    try:
        values = ws.col_values(3)
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
        time.sleep(1)

    log.info(f"Total jobs collected: {len(all_jobs)}")

    if SOFTWARE_DEV_ONLY:
        all_jobs = [j for j in all_jobs if looks_target_role(j.get("title", ""))]
        log.info(f"Total jobs after role filter: {len(all_jobs)}")

    if SHEET_ID == "PASTE_YOUR_SHEET_ID_HERE":
        log.error("Set JOB_SHEET_ID env var or edit SHEET_ID in this file.")
        sys.exit(1)

    if not os.path.exists(CREDENTIALS_FILE):
        log.error(f"Missing credentials file: {CREDENTIALS_FILE}.")
        sys.exit(1)

    ws = get_sheet()
    added = append_jobs(ws, all_jobs)
    log.info(f"Done. Added {added} new job(s) to the sheet.")


if __name__ == "__main__":
    main()
