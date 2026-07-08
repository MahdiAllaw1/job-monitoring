import hashlib
import html
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import requests
import yaml
from bs4 import BeautifulSoup

CONFIG_FILE = os.environ.get("JOB_MONITOR_CONFIG", "companies.yml")
STATE_FILE = os.environ.get("JOB_MONITOR_STATE", "jobs_state.json")
MAX_ALERTS_PER_RUN = int(os.environ.get("MAX_ALERTS_PER_RUN", "25"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "35"))
NOTIFY_MODE = os.environ.get("NOTIFY_MODE", "new").strip().lower()  # new | sample | all | reset_baseline
SAMPLE_PER_COMPANY = int(os.environ.get("SAMPLE_PER_COMPANY", "5"))
MAX_BROWSER_CLICKS = int(os.environ.get("MAX_BROWSER_CLICKS", "12"))
BROWSER_TIMEOUT_MS = int(os.environ.get("BROWSER_TIMEOUT_MS", "45000"))

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
        "JobMonitor/2.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Connection": "close",
}

JOB_HREF_PATTERNS = [
    "/job/", "/jobs/", "/careers/", "/career/", "/offres-demploi/", "/offre", "/emploi",
    "jobid=", "job_id=", "jobId=", "reqid=", "requisition", "position", "posting",
    "search/jobdetails", "apply", "vacancies", "openings", "lever.co", "greenhouse.io",
]

BAD_TEXT = {
    "home", "homepage", "privacy", "cookies", "terms", "contact", "linkedin", "facebook",
    "youtube", "instagram", "twitter", "x", "search", "clear", "reset", "read more",
    "voir la description complète du rôle", "envoyer un cv", "save job", "saved jobs",
    "skip to main content", "all jobs", "all locations", "candidature spontanée", "apply",
    "postuler", "join our talent community", "view all jobs", "voir toutes les offres",
}

BAD_HREF_PARTS = [
    "/privacy", "/cookie", "/terms", "/login", "/saved", "/talent-community", "#", "mailto:", "tel:",
    "javascript:", "/blog", "/news", "/events", "/benefits", "/life-at", "/culture", "/faq",
]

SUGGESTION_MARKERS = [
    "jobs for you", "suggested jobs", "previously viewed jobs", "saved jobs", "recommended jobs",
    "similar jobs", "you may also like", "emplois similaires", "offres similaires", "recommand",
]

CLICK_BUTTON_RE = re.compile(
    r"(show\s*more|load\s*more|more\s*jobs|show\s*more\s*requisitions|voir\s*plus|afficher\s*plus|charger\s*plus|plus\s*d.offres|voir\s*les\s*offres\s*suivantes)",
    re.I,
)

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
    # Telegram limit is 4096 characters. Split softly so big debug snapshots do not fail.
    chunks: list[str] = []
    while len(text) > 3800:
        cut = text.rfind("\n\n", 0, 3800)
        if cut < 1200:
            cut = 3800
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    chunks.append(text)
    for chunk in chunks:
        r = requests.post(
            api_url,
            json={"chat_id": TG_CHAT_ID, "text": chunk, "disable_web_page_preview": True},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        time.sleep(0.25)


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


def get_urls(company: dict[str, Any]) -> list[str]:
    urls = company.get("urls")
    if isinstance(urls, list) and urls:
        return [str(u).strip() for u in urls if str(u).strip()]
    return [str(company.get("url", "")).strip()]


def stable_id(company: str, title: str, url: str) -> str:
    parsed = urlparse(url)
    cleaned_query = parse_qs(parsed.query)
    for key in list(cleaned_query):
        k = key.lower()
        if k.startswith(("utm_", "gh_src")) or k in {"src", "source", "ref", "triggergobutton"}:
            cleaned_query.pop(key, None)
    query = urlencode({k: v[0] if len(v) == 1 else v for k, v in sorted(cleaned_query.items())}, doseq=True)
    clean_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", query, ""))
    raw = f"{company}\n{title.lower()}\n{clean_url.lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def fetch(url: str, *, method: str = "GET", json_body: dict[str, Any] | None = None, extra_headers: dict[str, str] | None = None) -> requests.Response:
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    last_exc: Exception | None = None
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
    raise last_exc or RuntimeError("fetch failed")


def clean_title(text: str) -> str:
    text = normalize_space(text)
    text = re.sub(r"\b(Read more|Save job|Save Job|Apply|Postuler|Envoyer un CV|Voir l'offre|View job)\b", "", text, flags=re.I)
    text = re.sub(r"\s+[|·-]\s+(France|FR|Paris|Grenoble|Sophia|Toulouse|Meylan|Courbevoie).*$", "", text, flags=re.I)
    text = normalize_space(text)
    if len(text) > 180:
        text = text[:177].rstrip() + "..."
    return text


def looks_like_job_link(href: str, text: str) -> bool:
    if not href:
        return False
    low_href = href.lower().strip()
    low_text = normalize_space(text).lower()
    if any(low_href.startswith(bad) or bad in low_href for bad in BAD_HREF_PARTS):
        return False
    if not low_text or len(low_text) < 4:
        return False
    if low_text in BAD_TEXT:
        return False
    if len(low_text) > 220:
        # Long blocks are often a whole card. We still allow them if the URL is very job-like.
        return any(x in low_href for x in ["/job/", "/jobs/", "jobid", "requisition", "posting"])
    return any(pat.lower() in low_href for pat in JOB_HREF_PATTERNS)


def strip_after_markers(raw_html: str, markers: Iterable[str]) -> str:
    low = raw_html.lower()
    cut = len(raw_html)
    for marker in markers:
        idx = low.find(marker.lower())
        if idx != -1:
            cut = min(cut, idx)
    return raw_html[:cut]


def extract_location_from_context(context: str) -> str:
    context = normalize_space(context)
    patterns = [
        r"((?:[A-ZÀ-ÿ][\wÀ-ÿ'\-]+(?:\s+[A-ZÀ-ÿ][\wÀ-ÿ'\-]+){0,3}),\s*(?:France|FR))",
        r"((?:Paris|Grenoble|Meylan|Sophia Antipolis|Toulouse|Rousset|Aix-en-Provence|Courbevoie|La Ciotat|Gémenos|Nantes|Rennes|Nice|Lyon|Marseille|Crolles|Caen|Pessac|Valbonne)[^\n,;]{0,40})",
    ]
    for pat in patterns:
        m = re.search(pat, context, re.I)
        if m:
            return normalize_space(m.group(1))[:120]
    return ""


def jobs_from_soup(company_name: str, source_url: str, soup: BeautifulSoup, *, meta: str = "html") -> list[Job]:
    jobs: dict[str, Job] = {}
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = normalize_space(a.get_text(" ", strip=True) or a.get("aria-label", "") or a.get("title", ""))
        url = urljoin(source_url, href)
        if not looks_like_job_link(href, text):
            continue
        title = clean_title(text)
        if len(title) < 4 or title.lower() in BAD_TEXT:
            continue
        context_node = a.find_parent(["li", "article", "section", "div"])
        context = normalize_space(context_node.get_text(" ", strip=True) if context_node else text)
        if any(marker in context.lower()[:250] for marker in SUGGESTION_MARKERS):
            continue
        jid = stable_id(company_name, title, url)
        jobs[jid] = Job(id=jid, company=company_name, title=title, url=url, location=extract_location_from_context(context), meta=meta)
    return list(jobs.values())


def parse_generic_static(company: dict[str, Any], source_url: str) -> list[Job]:
    r = fetch(source_url)
    raw = r.text
    if company.get("ignore_after_markers"):
        raw = strip_after_markers(raw, company["ignore_after_markers"])
    soup = BeautifulSoup(raw, "html.parser")
    return jobs_from_soup(company["name"], source_url, soup, meta="html")


def parse_arm(company: dict[str, Any], source_url: str) -> list[Job]:
    # ARM pages include a real result list, then a changing "Jobs for You" suggestion block.
    # Keep only the first N job links, where N comes from "5 results found".
    r = fetch(source_url)
    raw = strip_after_markers(r.text, ["Jobs for You", "Suggested jobs", "Previously viewed jobs", "Saved jobs"])
    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text(" ", strip=True)
    m = re.search(r"(\d+)\s+results?\s+found", text, re.I)
    expected = int(m.group(1)) if m else None
    jobs = jobs_from_soup(company["name"], source_url, soup, meta="arm-results")
    # ARM sometimes repeats links in hidden/mobile blocks. Preserve order, then cap to real result count.
    dedup_by_url: dict[str, Job] = {}
    for j in jobs:
        key = urlparse(j.url).path.rstrip("/").lower()
        dedup_by_url.setdefault(key, j)
    out = list(dedup_by_url.values())
    if expected:
        out = out[:expected]
    return out


def infer_workday(company: dict[str, Any], source_url: str) -> dict[str, Any] | None:
    parsed = urlparse(source_url)
    cfg = dict(company.get("workday", {}) or {})
    host = cfg.get("host") or f"{parsed.scheme}://{parsed.netloc}"
    tenant = cfg.get("tenant")
    site = cfg.get("site")

    # Pattern: https://wd5.myworkdaysite.com/en-US/recruiting/microchiphr/External?...
    m = re.search(r"/recruiting/([^/]+)/([^/?#]+)", parsed.path)
    if m:
        tenant = tenant or m.group(1)
        site = site or m.group(2)

    # Pattern: https://nxp.wd3.myworkdayjobs.com/careers?...
    if not tenant and ".myworkdayjobs.com" in parsed.netloc:
        tenant = parsed.netloc.split(".")[0]
    if not site:
        parts = [p for p in parsed.path.split("/") if p]
        if parts:
            site = parts[-1]

    if not tenant or not site:
        return None

    qs = parse_qs(parsed.query)
    facets = dict(cfg.get("facets", {}) or {})
    for k, vals in qs.items():
        if k.lower().startswith("utm") or not vals:
            continue
        # Workday facet query names are usually already the API facet names.
        facets.setdefault(k, vals)

    return {"host": host, "tenant": tenant, "site": site, "facets": facets}


def parse_workday(company: dict[str, Any], source_url: str) -> list[Job]:
    name = company["name"]
    cfg = infer_workday(company, source_url)
    if not cfg:
        return []
    host = cfg["host"].rstrip("/")
    tenant = cfg["tenant"]
    site = cfg["site"]
    endpoint = f"{host}/wday/cxs/{tenant}/{site}/jobs"
    facets = cfg.get("facets", {})
    jobs: dict[str, Job] = {}
    offset = 0
    limit = 20
    total = None

    while True:
        body = {"appliedFacets": facets, "limit": limit, "offset": offset, "searchText": ""}
        r = fetch(endpoint, method="POST", json_body=body, extra_headers={"Content-Type": "application/json", "Accept": "application/json"})
        data = r.json()
        total = data.get("total") or data.get("totalCount") or total
        postings = data.get("jobPostings") or data.get("jobs") or []
        if not postings:
            break
        for p in postings:
            if not isinstance(p, dict):
                continue
            title = normalize_space(p.get("title") or p.get("externalPath") or "Untitled Workday job")
            external_path = p.get("externalPath") or p.get("jobPostingId") or ""
            if external_path and not str(external_path).startswith("/"):
                external_path = "/" + str(external_path)
            if "/recruiting/" in urlparse(source_url).path:
                job_url = f"{host}/en-US/recruiting/{tenant}/{site}{external_path}"
            else:
                job_url = f"{host}/{site}{external_path}"
            location = ""
            if isinstance(p.get("locationsText"), str):
                location = p["locationsText"]
            elif isinstance(p.get("bulletFields"), list):
                location = " · ".join(str(x) for x in p["bulletFields"][:3])
            jid = str(p.get("id") or p.get("jobPostingId") or stable_id(name, title, job_url))
            jobs[jid] = Job(id=jid, company=name, title=title, url=job_url, location=location, meta="workday-api")
        offset += limit
        if total is not None and offset >= int(total):
            break
        if offset >= int(company.get("max_api_jobs", 200)):
            break
    return list(jobs.values())


def parse_eightfold(company: dict[str, Any], source_url: str) -> list[Job]:
    name = company["name"]
    parsed = urlparse(source_url)
    qs = parse_qs(parsed.query)
    domain = (qs.get("domain") or [parsed.netloc])[0]
    query = (qs.get("query") or [""])[0]
    location = (qs.get("location") or [""])[0]
    sort_by = (qs.get("sort_by") or ["relevance"])[0]
    base = f"{parsed.scheme}://{parsed.netloc}"

    possible_endpoints = [
        f"{base}/api/apply/v2/jobs",
        f"{base}/api/apply/v2/jobs/search",
        f"{base}/api/careers/v2/jobs",
    ]
    jobs: dict[str, Job] = {}
    for endpoint in possible_endpoints:
        params = {"domain": domain, "query": query, "location": location, "sort_by": sort_by, "num": "50", "start": "0"}
        url = endpoint + "?" + urlencode(params)
        try:
            r = fetch(url, extra_headers={"Accept": "application/json"})
            data = r.json()
        except Exception:
            continue
        candidates: list[Any] = []
        stack = [data]
        seen = 0
        while stack and seen < 200:
            seen += 1
            obj = stack.pop()
            if isinstance(obj, list):
                if obj and all(isinstance(x, dict) for x in obj) and any(("title" in x or "name" in x or "position" in x or "pid" in x) for x in obj):
                    candidates = obj
                    break
                stack.extend(obj[:20])
            elif isinstance(obj, dict):
                stack.extend(obj.values())
        for p in candidates:
            if not isinstance(p, dict):
                continue
            title = normalize_space(p.get("name") or p.get("title") or p.get("position_name") or p.get("position") or "Untitled job")
            if title.lower() == "untitled job":
                continue
            job_id = str(p.get("id") or p.get("pid") or p.get("position_id") or stable_id(name, title, source_url))
            job_url = p.get("canonicalPositionUrl") or p.get("url") or p.get("apply_url") or p.get("position_url")
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
            jobs[job_id] = Job(id=job_id, company=name, title=title, url=job_url, location=location_text, meta="eightfold-api")
        if jobs:
            break
    return list(jobs.values())


def browser_extract_one(company: dict[str, Any], source_url: str) -> list[Job]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    except Exception as exc:
        raise RuntimeError("Playwright is not installed. In GitHub Actions, keep the 'playwright install chromium' step.") from exc

    company_name = company["name"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(locale="fr-FR", user_agent=HEADERS["User-Agent"])
        page = context.new_page()
        try:
            page.goto(source_url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass

            # Cookie banners often hide load-more buttons.
            for label in ["Accept All", "Accept all", "Tout accepter", "Accepter", "I agree", "Agree"]:
                try:
                    page.get_by_text(label, exact=False).first.click(timeout=1200)
                    break
                except Exception:
                    pass

            previous_height = 0
            for _ in range(3):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(900)
                height = page.evaluate("document.body.scrollHeight")
                if height == previous_height:
                    break
                previous_height = height

            for _ in range(MAX_BROWSER_CLICKS):
                clicked = False
                buttons = page.locator("button, a[role='button'], [role='button']")
                count = min(buttons.count(), 80)
                for i in range(count):
                    try:
                        b = buttons.nth(i)
                        txt = normalize_space(b.inner_text(timeout=500))
                        if txt and CLICK_BUTTON_RE.search(txt) and b.is_visible():
                            b.click(timeout=2500)
                            page.wait_for_timeout(1500)
                            try:
                                page.wait_for_load_state("networkidle", timeout=8000)
                            except PlaywrightTimeoutError:
                                pass
                            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                            page.wait_for_timeout(600)
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    break

            raw_items = page.evaluate(
                r"""
                () => {
                  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
                  const visible = el => {
                    const r = el.getBoundingClientRect();
                    const st = window.getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
                  };
                  const markers = ['jobs for you', 'suggested jobs', 'previously viewed jobs', 'saved jobs', 'recommended jobs', 'similar jobs', 'you may also like', 'emplois similaires', 'offres similaires'];
                  const out = [];
                  for (const a of Array.from(document.querySelectorAll('a[href]'))) {
                    if (!visible(a)) continue;
                    const href = a.href;
                    const text = clean(a.innerText || a.getAttribute('aria-label') || a.getAttribute('title') || a.textContent);
                    let n = a;
                    let bad = false;
                    let context = text;
                    for (let depth = 0; n && depth < 6; depth++, n = n.parentElement) {
                      const t = clean(n.innerText || '');
                      if (t.length > context.length && t.length < 1200) context = t;
                      const prefix = t.slice(0, 220).toLowerCase();
                      if (markers.some(m => prefix.includes(m))) { bad = true; break; }
                    }
                    if (bad) continue;
                    out.push({href, text, context});
                  }
                  return out;
                }
                """
            )
        finally:
            browser.close()

    jobs: dict[str, Job] = {}
    for item in raw_items:
        href = item.get("href", "")
        text = item.get("text", "")
        if not looks_like_job_link(href, text):
            continue
        title = clean_title(text)
        if len(title) < 4 or title.lower() in BAD_TEXT:
            continue
        loc = extract_location_from_context(item.get("context", ""))
        jid = stable_id(company_name, title, href)
        jobs[jid] = Job(id=jid, company=company_name, title=title, url=href, location=loc, meta="browser")
    return list(jobs.values())


def parse_browser(company: dict[str, Any], source_url: str) -> list[Job]:
    return browser_extract_one(company, source_url)


def parse_one_url(company: dict[str, Any], source_url: str) -> list[Job]:
    adapter = (company.get("adapter") or "auto").lower()
    host = urlparse(source_url).netloc.lower()
    path = urlparse(source_url).path.lower()

    # Hard adapters first.
    if adapter == "arm" or "careers.arm.com" in host:
        return parse_arm(company, source_url)
    if adapter == "workday" or "workday" in host:
        try:
            jobs = parse_workday(company, source_url)
            if jobs:
                return jobs
        except Exception as exc:
            print(f"Workday API failed for {company['name']}: {exc}. Falling back to browser/static.")
    if adapter == "eightfold" or "eightfold.ai" in host:
        try:
            jobs = parse_eightfold(company, source_url)
            if jobs:
                return jobs
        except Exception as exc:
            print(f"Eightfold API failed for {company['name']}: {exc}. Falling back to browser/static.")
    if adapter == "browser":
        return parse_browser(company, source_url)

    # Auto/static: try HTML first because it is cheap and stable. If empty, render with browser.
    try:
        jobs = parse_generic_static(company, source_url)
        min_jobs = int(company.get("min_jobs_before_browser", 1))
        if len(jobs) >= min_jobs:
            return jobs
    except Exception as exc:
        print(f"Static parser failed for {company['name']} {source_url}: {exc}. Falling back to browser.")

    return parse_browser(company, source_url)


def parse_company(company: dict[str, Any]) -> list[Job]:
    combined: dict[str, Job] = {}
    errors: list[str] = []
    for source_url in get_urls(company):
        if not source_url:
            continue
        try:
            for job in parse_one_url(company, source_url):
                combined[job.id] = job
        except Exception as exc:
            errors.append(f"{source_url}: {type(exc).__name__}: {exc}")
    if errors and not combined:
        raise RuntimeError(" | ".join(errors))
    if errors:
        print(f"Partial parser warnings for {company['name']}: " + " | ".join(errors))
    return sorted(combined.values(), key=lambda j: (j.company.lower(), j.title.lower(), j.url.lower()))


def format_job(job: Job) -> str:
    bits = [f"• {job.company}: {job.title}"]
    if job.location:
        bits.append(f"  {job.location}")
    if job.meta:
        bits.append(f"  parser: {job.meta}")
    bits.append(f"  {job.url}")
    return "\n".join(bits)


def send_snapshot(parsed: dict[str, list[Job]], mode: str) -> None:
    lines: list[str] = []
    title = "🧪 Job monitor sample" if mode == "sample" else "📋 Job monitor current snapshot"
    lines.append(title)
    lines.append("This does not reset the baseline.")
    for company, jobs in parsed.items():
        lines.append(f"\n{company}: {len(jobs)} job(s) detected")
        shown = jobs if mode == "all" else jobs[:SAMPLE_PER_COMPANY]
        for j in shown:
            lines.append(format_job(j))
        if mode == "sample" and len(jobs) > SAMPLE_PER_COMPANY:
            lines.append(f"  …and {len(jobs) - SAMPLE_PER_COMPANY} more detected.")
    tg_send("\n".join(lines))


def main() -> int:
    if NOTIFY_MODE not in {"new", "sample", "all", "reset_baseline"}:
        raise ValueError("NOTIFY_MODE must be one of: new, sample, all, reset_baseline")

    config = load_config()
    state = load_state()
    initialized = bool(state.get("initialized", False))
    state.setdefault("companies", {})

    parsed_by_company: dict[str, list[Job]] = {}
    all_new: list[Job] = []
    summaries: list[str] = []
    errors: list[str] = []
    parser_alerts: list[str] = []

    for company in config["companies"]:
        name = company["name"]
        cstate = state["companies"].setdefault(name, {"seen_ids": [], "last_count": None, "last_error": None})
        try:
            jobs = parse_company(company)
            parsed_by_company[name] = jobs
            seen_now = sorted({j.id for j in jobs})
            old_ids = set(cstate.get("seen_ids", []))
            new_jobs = [j for j in jobs if j.id not in old_ids]
            if initialized and new_jobs and NOTIFY_MODE == "new":
                all_new.extend(new_jobs)
            summaries.append(f"{name}: {len(jobs)} job(s)")
        except Exception as exc:
            msg = f"{name}: {type(exc).__name__}: {exc}"
            errors.append(msg)
            previous_error = cstate.get("last_error")
            cstate["last_error"] = msg
            cstate["last_checked_epoch"] = int(time.time())
            if initialized and NOTIFY_MODE == "new" and previous_error != msg:
                parser_alerts.append(msg)

    if NOTIFY_MODE in {"sample", "all"}:
        send_snapshot(parsed_by_company, NOTIFY_MODE)
        if errors:
            tg_send("⚠️ Parser errors during sample run:\n\n" + "\n".join(errors))
        print("Snapshot sent. State was not modified.")
        return 0

    if NOTIFY_MODE == "reset_baseline":
        state = {"initialized": False, "companies": {}, "last_checked_epoch": None}
        for company_name, jobs in parsed_by_company.items():
            state["companies"][company_name] = {
                "seen_ids": sorted({j.id for j in jobs}),
                "last_count": len(jobs),
                "last_error": None,
                "last_checked_epoch": int(time.time()),
            }
        state["initialized"] = True
        state["last_checked_epoch"] = int(time.time())
        save_state(state)
        tg_send("✅ Job monitor baseline reset.\n\n" + "\n".join(summaries))
        if errors:
            tg_send("⚠️ Some sources need attention:\n\n" + "\n".join(errors))
        return 0

    # Normal mode: update state and alert only new jobs after initialization.
    for company in config["companies"]:
        name = company["name"]
        if name not in parsed_by_company:
            continue
        jobs = parsed_by_company[name]
        cstate = state["companies"].setdefault(name, {})
        cstate["seen_ids"] = sorted({j.id for j in jobs})
        cstate["last_count"] = len(jobs)
        cstate["last_error"] = None
        cstate["last_checked_epoch"] = int(time.time())

    state["initialized"] = True
    state["last_checked_epoch"] = int(time.time())
    save_state(state)

    if not initialized:
        tg_send("✅ Job monitor initialized. Baseline stored, no old jobs will be spammed.\n\n" + "\n".join(summaries))
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

    if parser_alerts:
        tg_send("⚠️ Job monitor parser problem(s)\n\n" + "\n".join(parser_alerts))

    if errors:
        print("Errors:")
        for e in errors:
            print(" -", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
