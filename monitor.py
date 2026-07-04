import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
import yaml
from bs4 import BeautifulSoup

CONFIG_FILE = os.environ.get("JOB_MONITOR_CONFIG", "companies.yml")
STATE_FILE = os.environ.get("JOB_MONITOR_STATE", "jobs_state.json")
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "20"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
        "JobMonitor/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Connection": "close",
}

JOB_HREF_PATTERNS = [
    "/job/", "/jobs/", "/careers/", "/career/", "/offres-demploi/", "/offre", "/emploi",
    "jobid=", "job_id=", "jobId=", "reqid=", "requisition", "position", "posting",
    "search/jobdetails", "apply", "vacancies", "openings",
]

BAD_TEXT = {
    "home", "homepage", "privacy", "cookies", "terms", "contact", "linkedin", "facebook",
    "youtube", "instagram", "twitter", "x", "search", "clear", "reset", "read more",
    "voir la description complète du rôle", "envoyer un cv", "save job", "saved jobs",
    "skip to main content", "all jobs", "all locations", "candidature spontanée",
}

@dataclass(frozen=True)
class Job:
    id: str
    company: str
    title: str
    url: str
    location: str = ""
    meta: str = ""


def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("Telegram disabled: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing.")
        print(text)
        return
    api_url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(api_url, json=payload, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()


def load_config() -> dict[str, Any]:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "companies" not in data or not isinstance(data["companies"], list):
        raise ValueError("companies.yml must contain a top-level 'companies' list.")
    return data


def load_state() -> dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"initialized": False, "companies": {}, "last_checked_epoch": None}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(s or "")).strip()


def stable_id(company: str, title: str, url: str) -> str:
    parsed = urlparse(url)
    cleaned_query = parse_qs(parsed.query)
    # Remove tracking params so the same job does not become new every run.
    for key in list(cleaned_query):
        if key.lower().startswith(("utm_", "gh_src")) or key.lower() in {"src", "source", "ref"}:
            cleaned_query.pop(key, None)
    query = urlencode({k: v[0] if len(v) == 1 else v for k, v in sorted(cleaned_query.items())}, doseq=True)
    clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", query, ""))
    raw = f"{company}\n{title.lower()}\n{clean_url.lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def fetch(url: str, *, method: str = "GET", json_body: dict[str, Any] | None = None, extra_headers: dict[str, str] | None = None) -> requests.Response:
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    last_exc = None
    for attempt in range(3):
        try:
            if method.upper() == "POST":
                r = requests.post(url, headers=headers, json=json_body, timeout=REQUEST_TIMEOUT)
            else:
                r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r
        except Exception as exc:
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def looks_like_job_link(href: str, text: str, base_netloc: str) -> bool:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return False
    low_href = href.lower()
    low_text = normalize_space(text).lower()
    if not low_text or len(low_text) < 4:
        return False
    if low_text in BAD_TEXT:
        return False
    if any(bad in low_href for bad in ["/privacy", "/cookie", "/terms", "/login", "/saved", "/talent-community"]):
        return False
    return any(pat.lower() in low_href for pat in JOB_HREF_PATTERNS)


def parse_generic(company: dict[str, Any]) -> list[Job]:
    name = company["name"]
    source_url = company["url"]
    r = fetch(source_url)
    soup = BeautifulSoup(r.text, "html.parser")
    base = source_url
    parsed_base = urlparse(source_url)
    jobs: dict[str, Job] = {}

    # Primary extraction: candidate anchors.
    for a in soup.select("a[href]"):
        text = normalize_space(a.get_text(" ", strip=True))
        href = a.get("href", "")
        url = urljoin(base, href)
        if not looks_like_job_link(href, text, parsed_base.netloc):
            continue

        # Clean common duplicated phrases in accessible labels.
        title = re.sub(r"\b(Read more|Save job|Save Job|Apply|Postuler|Envoyer un CV)\b", "", text, flags=re.I)
        title = normalize_space(title)
        if len(title) > 180:
            title = title[:177].rstrip() + "..."
        if len(title) < 4:
            continue

        jid = stable_id(name, title, url)
        jobs[jid] = Job(id=jid, company=name, title=title, url=url)

    # Fallback for pages where job data is present as JSON/LD or script text.
    if not jobs:
        text = soup.get_text(" ", strip=True)
        # This fallback intentionally avoids keyword filtering. It only tries to prevent silent failure.
        # If a page is fully JavaScript-rendered and has no server-side jobs, an adapter is needed.
        for m in re.finditer(r"(?P<title>[A-ZÀ-ÿ0-9][A-Za-zÀ-ÿ0-9 /&+,.()\-]{8,120})", text):
            title = normalize_space(m.group("title"))
            if any(bad in title.lower() for bad in BAD_TEXT):
                continue
            if len(title.split()) < 2:
                continue
            jid = stable_id(name, title, source_url + "#text")
            jobs[jid] = Job(id=jid, company=name, title=title, url=source_url, meta="text-fallback")
            if len(jobs) >= 20:
                break

    return list(jobs.values())


def parse_workday(company: dict[str, Any]) -> list[Job]:
    name = company["name"]
    cfg = company.get("workday", {})
    host = cfg.get("host") or "https://wd5.myworkdaysite.com"
    tenant = cfg.get("tenant")
    site = cfg.get("site")
    if not tenant or not site:
        return parse_generic(company)

    endpoint = f"{host.rstrip('/')}/wday/cxs/{tenant}/{site}/jobs"
    facets = cfg.get("facets", {})
    jobs: dict[str, Job] = {}
    offset = 0
    limit = 20
    total = None

    while True:
        body = {
            "appliedFacets": facets,
            "limit": limit,
            "offset": offset,
            "searchText": "",
        }
        r = fetch(endpoint, method="POST", json_body=body, extra_headers={"Content-Type": "application/json"})
        data = r.json()
        total = data.get("total") or data.get("totalCount") or total
        postings = data.get("jobPostings") or data.get("jobs") or []
        if not postings:
            break

        for p in postings:
            title = normalize_space(p.get("title") or p.get("externalPath") or "Untitled Workday job")
            external_path = p.get("externalPath") or p.get("jobPostingId") or p.get("bulletFields", [""])[0]
            if external_path and not str(external_path).startswith("/"):
                external_path = "/" + str(external_path)
            url = urljoin(company["url"], external_path or "")
            if "/job/" not in url and external_path:
                url = f"{host.rstrip('/')}/en-US/recruiting/{tenant}/{site}{external_path}"
            location = ""
            if isinstance(p.get("locationsText"), str):
                location = p["locationsText"]
            elif isinstance(p.get("bulletFields"), list):
                location = " · ".join(str(x) for x in p["bulletFields"][:2])
            jid = str(p.get("id") or p.get("jobPostingId") or stable_id(name, title, url))
            jobs[jid] = Job(id=jid, company=name, title=title, url=url, location=location)

        offset += limit
        if total is not None and offset >= int(total):
            break
        if offset >= 100:
            break

    return list(jobs.values())


def parse_eightfold(company: dict[str, Any]) -> list[Job]:
    name = company["name"]
    source_url = company["url"]
    parsed = urlparse(source_url)
    qs = parse_qs(parsed.query)
    domain = (qs.get("domain") or [parsed.netloc])[0]
    query = (qs.get("query") or [""])[0]
    location = (qs.get("location") or [""])[0]
    sort_by = (qs.get("sort_by") or ["relevance"])[0]
    pid = (qs.get("pid") or [""])[0]

    base = f"{parsed.scheme}://{parsed.netloc}"
    endpoint = f"{base}/api/apply/v2/jobs"
    params = {
        "domain": domain,
        "query": query,
        "location": location,
        "sort_by": sort_by,
        "num": "20",
        "start": "0",
    }
    if pid:
        params["pid"] = pid
    url = endpoint + "?" + urlencode(params)

    try:
        r = fetch(url, extra_headers={"Accept": "application/json"})
        data = r.json()
    except Exception:
        return parse_generic(company)

    candidates = []
    for key in ("positions", "jobs", "data", "results"):
        val = data.get(key) if isinstance(data, dict) else None
        if isinstance(val, list):
            candidates = val
            break
        if isinstance(val, dict):
            for nested_key in ("positions", "jobs", "results"):
                if isinstance(val.get(nested_key), list):
                    candidates = val[nested_key]
                    break

    jobs: dict[str, Job] = {}
    for p in candidates:
        if not isinstance(p, dict):
            continue
        title = normalize_space(p.get("name") or p.get("title") or p.get("position_name") or "Untitled job")
        job_id = str(p.get("id") or p.get("pid") or p.get("position_id") or stable_id(name, title, source_url))
        job_url = p.get("canonicalPositionUrl") or p.get("url") or p.get("apply_url")
        if not job_url:
            job_url = f"{base}/careers/job/{job_id}"
        job_url = urljoin(base, str(job_url))
        loc = p.get("location") or p.get("locations") or ""
        if isinstance(loc, list):
            location_text = " · ".join(str(x) for x in loc[:3])
        elif isinstance(loc, dict):
            location_text = normalize_space(" ".join(str(v) for v in loc.values() if v))
        else:
            location_text = normalize_space(str(loc))
        jobs[job_id] = Job(id=job_id, company=name, title=title, url=job_url, location=location_text)

    if not jobs:
        return parse_generic(company)
    return list(jobs.values())


def parse_company(company: dict[str, Any]) -> list[Job]:
    adapter = (company.get("adapter") or "generic").lower()
    if adapter == "workday":
        return parse_workday(company)
    if adapter == "eightfold":
        return parse_eightfold(company)
    return parse_generic(company)


def format_job(job: Job) -> str:
    bits = [f"• {job.company}: {job.title}"]
    if job.location:
        bits.append(f"  {job.location}")
    if job.meta:
        bits.append(f"  ({job.meta})")
    bits.append(f"  {job.url}")
    return "\n".join(bits)


def main() -> int:
    config = load_config()
    state = load_state()
    initialized = bool(state.get("initialized", False))
    state.setdefault("companies", {})

    all_new: list[Job] = []
    summaries: list[str] = []
    errors: list[str] = []

    for company in config["companies"]:
        name = company["name"]
        cstate = state["companies"].setdefault(name, {"seen_ids": [], "last_count": None, "last_error": None})
        try:
            jobs = parse_company(company)
            seen_now = sorted({j.id for j in jobs})
            old_ids = set(cstate.get("seen_ids", []))
            new_jobs = [j for j in jobs if j.id not in old_ids]

            if initialized and new_jobs:
                all_new.extend(new_jobs)

            cstate["seen_ids"] = seen_now
            cstate["last_count"] = len(jobs)
            cstate["last_error"] = None
            cstate["last_checked_epoch"] = int(time.time())
            summaries.append(f"{name}: {len(jobs)} job(s)")
        except Exception as exc:
            msg = f"{name}: {type(exc).__name__}: {exc}"
            errors.append(msg)
            previous = cstate.get("last_error")
            cstate["last_error"] = msg
            cstate["last_checked_epoch"] = int(time.time())
            if initialized and previous != msg:
                tg_send(f"⚠️ Job monitor parser problem\n\n{msg}\n\nSource: {company.get('url', '')}")

    state["initialized"] = True
    state["last_checked_epoch"] = int(time.time())
    save_state(state)

    if not initialized:
        tg_send("Job monitor initialized. Baseline stored, no old jobs will be spammed.\n\n" + "\n".join(summaries))
        if errors:
            tg_send("⚠️ Some sources need attention:\n\n" + "\n".join(errors))
        return 0

    if all_new:
        capped = all_new[:MAX_ALERTS_PER_RUN]
        text = "🚨 New job offer(s) detected\n\n" + "\n\n".join(format_job(j) for j in capped)
        if len(all_new) > MAX_ALERTS_PER_RUN:
            text += f"\n\n…and {len(all_new) - MAX_ALERTS_PER_RUN} more. Open the career pages directly."
        tg_send(text)
    else:
        print("No new jobs detected.")

    if errors:
        print("Errors:")
        for e in errors:
            print(" -", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
