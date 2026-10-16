#!/usr/bin/env python3
"""
Internship watch: checks the careers pages of every organisation in orgs.yaml
for internships, summer schemes and placements, emails you when something new
appears, and writes the data behind the dashboard in docs/.

Usage
  python watch.py                 normal run (emails new finds if SMTP is set up)
  python watch.py --dry-run       run without sending email; prints what it would send
  python watch.py --only IPPR     check one organisation (name contains this text)
  python watch.py --seed          record everything currently listed without alerting
  python watch.py --no-render     never use the headless browser

Email settings come from environment variables (see README):
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_TO, CONTACT_EMAIL, DASHBOARD_URL
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html as htmlmod
import json
import os
import re
import smtplib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse, urldefrag

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
MAX_WORKERS = 6
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "1.0"))          # seconds between requests from the same worker
TIMEOUT = 20
MAX_PAGES_PER_ORG = 5
REDISCOVER_DAYS = 30

# What counts as an internship-type listing (matched against link text and URL path)
INTERN_RE = re.compile(
    r"\b(intern|interns|internship|internships|summer\s+(student|students|scheme|schemes|placement|placements|"
    r"programme|program|internship|internships|analyst|associate)|placement|placements|work\s+experience|"
    r"trainee|trainees|traineeship|traineeships|student\s+(placement|placements|scheme|programme)|"
    r"insight\s+(day|days|week|weeks|programme)|early\s+careers?)\b",
    re.I,
)
# Links that match the keywords but are almost never postings
NOISE_RE = re.compile(r"(privacy|cookie|terms|accessibility|newsletter|subscribe|donate|login|log in|sign in)", re.I)
GENERIC_RE = re.compile(r"^(apply( now| here| online)?|read more|find out more|more|learn more|view( job| role| vacancy| details)?|"
                        r"details|click here|here|more info(rmation)?|see more)$", re.I)
CAREERS_RE = re.compile(r"(career|jobs?\b|vacanc|work[\s_-]*(with|for)[\s_-]*us|join[\s_-]*(us|our|the)|opportunit|internship|recruit)", re.I)
STATUS_RE = re.compile(r"\b(applications?|apply|deadline|closing date|closes|opens?|opened|closed|now open|recruitment)\b", re.I)
SOCIAL_HOSTS = ("twitter.com", "x.com", "facebook.com", "linkedin.com", "instagram.com", "youtube.com",
                "tiktok.com", "bsky.app", "threads.net", "whatsapp.com")
JS_HOSTS = ("beapplied.com", "myworkdayjobs.com", "careers.un.org", "taleo.net", "successfactors", "icims.com")


# ---------------------------------------------------------------- utilities
def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def clean(text: str) -> str:
    return " ".join((text or "").split())


def pid_for(org: str, url: str) -> str:
    return hashlib.sha1(f"{org}|{url}".encode()).hexdigest()[:14]


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def same_site(a: str, b: str) -> bool:
    ha, hb = host_of(a), host_of(b)
    root = lambda h: ".".join(h.split(".")[-3:]) if h.endswith((".co.uk", ".org.uk", ".gov.uk", ".ac.uk")) else ".".join(h.split(".")[-2:])
    return root(ha) == root(hb)


class Blocked(Exception):
    pass


# ---------------------------------------------------------------- fetching
class Fetcher:
    def __init__(self, contact: str):
        self.ua = f"InternshipWatch/1.0 (personal job alerts; {contact})" if contact else "InternshipWatch/1.0 (personal job alerts)"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": self.ua, "Accept-Language": "en-GB,en;q=0.8"})
        self._robots: dict[str, robotparser.RobotFileParser] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        with self._lock:
            rp = self._robots.get(base)
        if rp is None:
            rp = robotparser.RobotFileParser()
            try:
                r = self.session.get(base + "/robots.txt", timeout=10)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException:
                rp.parse([])
            with self._lock:
                self._robots[base] = rp
        return rp.can_fetch(self.ua, url)

    def get_html(self, url: str) -> str:
        if not self.allowed(url):
            raise Blocked("blocked by robots.txt")
        r = self.session.get(url, timeout=TIMEOUT)
        time.sleep(REQUEST_DELAY)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "xml" not in ctype:
            raise ValueError(f"not a web page ({ctype or 'unknown type'})")
        return r.text

    def get_json(self, url: str, method: str = "GET", body: dict | None = None):
        r = self.session.request(method, url, json=body, timeout=TIMEOUT)
        time.sleep(REQUEST_DELAY)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------- applicant tracking systems
def ats_from_url(url: str):
    p = urlparse(url)
    h = p.netloc.lower()
    parts = [x for x in p.path.split("/") if x]
    if h in ("boards.greenhouse.io", "job-boards.greenhouse.io") and parts:
        return ("greenhouse", parts[0])
    if h == "jobs.lever.co" and parts:
        return ("lever", parts[0])
    if h == "apply.workable.com" and parts and parts[0] not in ("api", "j"):
        return ("workable", parts[0])
    if h in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and parts:
        return ("smartrecruiters", parts[0])
    if h.endswith(".pinpointhq.com"):
        return ("pinpoint", h.split(".")[0])
    return None


def fetch_ats(f: Fetcher, kind: str, ident: str) -> list[tuple[str, str]]:
    """Return every (title, url) the employer's applicant tracking system lists."""
    jobs: list[tuple[str, str]] = []
    if kind == "greenhouse":
        data = f.get_json(f"https://boards-api.greenhouse.io/v1/boards/{ident}/jobs")
        jobs = [(j["title"], j["absolute_url"]) for j in data.get("jobs", [])]
    elif kind == "lever":
        data = f.get_json(f"https://api.lever.co/v0/postings/{ident}?mode=json")
        jobs = [(j["text"], j["hostedUrl"]) for j in data]
    elif kind == "workable":
        data = f.get_json(f"https://apply.workable.com/api/v3/accounts/{ident}/jobs", "POST",
                          {"query": "", "location": [], "department": [], "worktype": [], "remote": []})
        jobs = [(j["title"], f"https://apply.workable.com/{ident}/j/{j['shortcode']}/") for j in data.get("results", [])]
    elif kind == "smartrecruiters":
        offset = 0
        while offset < 1000:
            data = f.get_json(f"https://api.smartrecruiters.com/v1/companies/{ident}/postings?limit=100&offset={offset}")
            batch = data.get("content", [])
            jobs += [(j["name"], f"https://jobs.smartrecruiters.com/{ident}/{j['id']}") for j in batch]
            if len(batch) < 100:
                break
            offset += 100
    elif kind == "pinpoint":
        data = f.get_json(f"https://{ident}.pinpointhq.com/postings.json")
        jobs = [(j["title"], j["url"]) for j in data.get("data", [])]
    return jobs


# ---------------------------------------------------------------- page parsing
def link_text(a) -> str:
    t = clean(a.get_text(" ", strip=True)) or clean(a.get("title") or a.get("aria-label") or "")
    if not t or GENERIC_RE.match(t):
        parent = a
        for _ in range(4):  # look for the heading of the card/row the link sits in
            parent = parent.parent
            if parent is None or parent.name in ("body", "html", "main"):
                break
            if len(parent.get_text(" ", strip=True)) > 800:  # bigger than a card: stop
                break
            h = parent.find(["h1", "h2", "h3", "h4", "h5", "strong"])
            if h:
                ht = clean(h.get_text(" ", strip=True))
                if ht and not GENERIC_RE.match(ht):
                    return ht[:200]
    return t[:200]


def soup_of(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "footer", "svg", "form"]):
        tag.decompose()
    return soup


def parse_page(page_url: str, html: str):
    """Return (matches, ats_found, careers_candidates, anchor_count, text_len) for one page."""
    soup = soup_of(html)
    matches, ats_found, careers = {}, set(), []
    anchors = soup.find_all("a", href=True)
    for a in anchors:
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urldefrag(urljoin(page_url, href))[0]
        h = host_of(url)
        if any(h == s or h.endswith("." + s) for s in SOCIAL_HOSTS):
            continue
        ats = ats_from_url(url)
        if ats:
            ats_found.add(ats)
        text = link_text(a)
        path_words = urlparse(url).path.replace("-", " ").replace("_", " ").replace("/", " ")
        hay = f"{text} {path_words}"
        if CAREERS_RE.search(hay) and (same_site(url, page_url) or ats or any(j in h for j in JS_HOSTS)):
            careers.append((text, url))
        if NOISE_RE.search(text) or url.rstrip("/") == page_url.rstrip("/"):
            continue
        if INTERN_RE.search(hay):
            title = text or path_words.strip().title()
            matches.setdefault(url, title)
    text_len = len(soup.get_text(" ", strip=True))
    return matches, ats_found, careers, len(anchors), text_len


def status_sentences(html: str) -> list[str]:
    soup = soup_of(html)
    for tag in soup(["nav", "header"]):
        tag.decompose()
    text = clean(soup.get_text(" ", strip=True))
    out = []
    for s in re.split(r"(?<=[.!?])\s+", text):
        if 20 < len(s) < 300 and STATUS_RE.search(s) and not NOISE_RE.search(s) and s not in out:
            out.append(s)
    return out[:25]


def score_careers_link(text: str, url: str) -> int:
    hay = f"{text} {url}".lower()
    score = 0
    if "intern" in hay: score += 5
    if re.search(r"vacanc|jobs?\b|career", hay): score += 4
    if re.search(r"work.?(with|for).?us|join", hay): score += 3
    if "opportunit" in hay: score += 1
    if ats_from_url(url) or any(j in host_of(url) for j in JS_HOSTS): score += 4
    return score


# ---------------------------------------------------------------- per-organisation check
@dataclass
class OrgResult:
    name: str
    postings: dict = field(default_factory=dict)      # url -> title
    snapshots: dict = field(default_factory=dict)     # url -> [sentences]
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    careers_urls: list = field(default_factory=list)
    render_queue: list = field(default_factory=list)
    visited: set = field(default_factory=set)
    skipped: str | None = None
    discovered: list | None = None


def needs_render(url: str, anchors: int, text_len: int) -> bool:
    return any(j in host_of(url) for j in JS_HOSTS) or anchors < 8 or text_len < 400


def handle_html(org: dict, url: str, html: str, res: OrgResult, f: Fetcher, visited: set, allow_follow=True):
    matches, ats_found, _careers, _a, _t = parse_page(url, html)
    for murl, title in matches.items():
        res.postings.setdefault(murl, title)
    for kind, ident in ats_found:
        if (kind, ident) in visited:
            continue
        visited.add((kind, ident))
        try:
            for title, jurl in fetch_ats(f, kind, ident):
                if INTERN_RE.search(title):
                    res.postings.setdefault(jurl, title)
            res.notes.append(f"read {kind} job board '{ident}'")
        except Exception as e:  # noqa: BLE001
            res.errors.append(f"{kind} '{ident}': {e}")
    if org.get("watch_text"):
        res.snapshots[url] = status_sentences(html)
    # Follow links to an internships page on the same site, one level deep
    if allow_follow:
        for murl, title in list(matches.items()):
            if len(visited) >= MAX_PAGES_PER_ORG + 2:
                break
            if murl in visited or not same_site(murl, url) or murl.lower().endswith(".pdf"):
                continue
            if "intern" in f"{title} {murl}".lower():
                visited.add(murl)
                try:
                    handle_html(org, murl, f.get_html(murl), res, f, visited, allow_follow=False)
                except Exception:  # noqa: BLE001
                    pass


def discover(org: dict, f: Fetcher) -> list[str]:
    html = f.get_html(org["url"])
    _m, _ats, careers, _a, _t = parse_page(org["url"], html)
    ranked = sorted({u: score_careers_link(t, u) for t, u in careers}.items(), key=lambda kv: -kv[1])
    urls = [u for u, s in ranked if s > 0][:3]
    return urls or [org["url"]]


def check_org(org: dict, f: Fetcher, cache: dict, render_ok: bool) -> OrgResult:
    res = OrgResult(org["name"])
    if org.get("skip"):
        res.skipped = org["skip"]
        return res
    visited = res.visited
    if org.get("ats"):
        a = org["ats"]
        visited.add((a["type"], a["id"]))
        try:
            for title, jurl in fetch_ats(f, a["type"], a["id"]):
                if INTERN_RE.search(title):
                    res.postings.setdefault(jurl, title)
            res.careers_urls.append(f"{a['type']}:{a['id']}")
        except Exception as e:  # noqa: BLE001
            res.errors.append(f"{a['type']} '{a['id']}': {e}")
        if not org.get("careers"):
            return res

    urls = org.get("careers")
    if not urls:
        c = cache.get(org["name"])
        fresh = c and (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(c["discovered"])).days < REDISCOVER_DAYS
        if fresh:
            urls = c["urls"]
        else:
            try:
                urls = discover(org, f)
                res.discovered = urls
                if urls == [org["url"]]:
                    res.notes.append("no careers page found on the homepage; scanning the homepage only")
            except Blocked as e:
                res.errors.append(f"{org['url']}: {e}")
                return res
            except Exception as e:  # noqa: BLE001
                res.errors.append(f"{org['url']}: {e}")
                return res

    for url in urls[:MAX_PAGES_PER_ORG]:
        res.careers_urls.append(url)
        visited.add(url)
        try:
            html = f.get_html(url)
        except Blocked as e:
            res.errors.append(f"{url}: {e}")
            continue
        except Exception as e:  # noqa: BLE001
            res.errors.append(f"{url}: {e}")
            continue
        _m, _ats, _c, anchors, text_len = parse_page(url, html)
        handle_html(org, url, html, res, f, visited)
        if render_ok and (org.get("render") or needs_render(url, anchors, text_len)):
            res.render_queue.append(url)   # read it again later in a real browser
    return res


def render_pass(jobs: list[tuple[dict, str, OrgResult]], f: Fetcher):
    """Load JavaScript-heavy pages in a headless browser (one at a time)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        for org, url, res in jobs:
            res.notes.append(f"{url} needs a browser to read; install playwright to check it")
        return
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=f.ua)
        for org, url, res in jobs:
            try:
                page.goto(url, timeout=45000, wait_until="networkidle")
                handle_html(org, url, page.content(), res, f, res.visited)
                res.notes.append(f"read {url} with a headless browser")
            except Exception as e:  # noqa: BLE001
                res.errors.append(f"{url} (browser): {e}")
        browser.close()


# ---------------------------------------------------------------- state, email, dashboard
def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"postings": {}, "pages": {}, "changes": [], "careers_cache": {}, "coverage": {}, "runs": []}


def merge(state: dict, orgs: list[dict], results: list[OrgResult], seed: bool):
    ts = now_iso()
    first_run = not state["runs"]
    meta = {o["name"]: o for o in orgs}
    new_posts, changes = [], []
    for res in results:
        o = meta[res.name]
        if res.discovered:
            state["careers_cache"][res.name] = {"urls": res.discovered, "discovered": ts}
        if res.skipped:
            status, detail = "skipped", res.skipped
        elif res.errors and not (res.postings or res.snapshots):
            status, detail = "error", "; ".join(res.errors)[:500]
        elif res.errors:
            status, detail = "partial", "; ".join(res.errors)[:500]
        else:
            status, detail = "ok", "; ".join(res.notes)[:500]
        state["coverage"][res.name] = {
            "status": status, "detail": detail, "careers_urls": res.careers_urls,
            "found": len(res.postings), "checked": ts,
        }
        seen_ids = set()
        for url, title in res.postings.items():
            pid = pid_for(res.name, url)
            seen_ids.add(pid)
            p = state["postings"].get(pid)
            if p:
                p.update(last_seen=ts, active=True, title=title)
                p.pop("closed", None)
            else:
                p = {"org": res.name, "title": title, "url": url, "first_seen": ts, "last_seen": ts,
                     "active": True, "tier": o.get("tier"), "category": o.get("category"),
                     "initial": bool(first_run or seed)}
                state["postings"][pid] = p
                if not (first_run or seed):
                    new_posts.append(p)
        if status == "ok":  # only mark things closed when the whole check worked
            for pid, p in state["postings"].items():
                if p["org"] == res.name and p.get("active") and pid not in seen_ids:
                    p.update(active=False, closed=ts)
        for url, sents in res.snapshots.items():
            h = hashlib.sha1("\n".join(sents).encode()).hexdigest()
            prev = state["pages"].get(url)
            if prev and prev["hash"] != h:
                added = [s for s in sents if s not in prev["sentences"]]
                if added:
                    ch = {"org": res.name, "url": url, "at": ts, "added": added,
                          "tier": o.get("tier"), "category": o.get("category")}
                    state["changes"].insert(0, ch)
                    if not seed:
                        changes.append(ch)
            state["pages"][url] = {"org": res.name, "hash": h, "sentences": sents, "checked": ts}
    state["changes"] = state["changes"][:200]
    state["runs"].insert(0, {"at": ts, "new": len(new_posts), "changes": len(changes),
                             "errors": sum(1 for r in results if r.errors)})
    state["runs"] = state["runs"][:60]
    return new_posts, changes, first_run


def write_dashboard_data(state: dict, orgs: list[dict], out: Path):
    postings = [dict(id=k, **v) for k, v in state["postings"].items()]
    postings.sort(key=lambda p: p["first_seen"], reverse=True)
    coverage = [dict(name=o["name"], tier=o.get("tier"), category=o.get("category"), homepage=o.get("url"),
                     **state["coverage"].get(o["name"], {"status": "not checked yet"})) for o in orgs]
    data = {"generated": now_iso(), "postings": postings, "changes": state["changes"][:100],
            "coverage": coverage, "runs": state["runs"][:10]}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("window.WATCH_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n")


def build_email(new_posts, changes, first_run, total_live, dashboard):
    esc = htmlmod.escape
    if first_run:
        subject = f"Internship watch is running: {total_live} listings found"
        text = (f"The first check found {total_live} internship-type links across your organisations.\n"
                f"From now on you'll only get emails about new ones.\n{dashboard}")
        body = f"<p>{esc(text).replace(chr(10), '<br>')}</p>"
        return subject, text, body
    orgs = sorted({p["org"] for p in new_posts} | {c["org"] for c in changes})
    parts = []
    if new_posts:
        parts.append(f"{len(new_posts)} new listing{'s' if len(new_posts) != 1 else ''}")
    if changes:
        parts.append(f"{len(changes)} page update{'s' if len(changes) != 1 else ''}")
    subject = f"{' and '.join(parts)}: {', '.join(orgs[:3])}{' and more' if len(orgs) > 3 else ''}"
    lines, html = [], []
    if new_posts:
        html.append("<h2 style='font-size:16px'>New listings</h2><ul>")
        for p in sorted(new_posts, key=lambda p: (str(p.get("tier")), p["org"])):
            lines.append(f"- {p['org']}: {p['title']}\n  {p['url']}")
            html.append(f"<li><b>{esc(p['org'])}</b> (tier {esc(str(p.get('tier')))}): "
                        f"<a href='{esc(p['url'])}'>{esc(p['title'])}</a></li>")
        html.append("</ul>")
    if changes:
        html.append("<h2 style='font-size:16px'>Pages that changed what they say about applications</h2>")
        for c in changes:
            lines.append(f"- {c['org']} ({c['url']}):\n" + "\n".join(f"    \"{s}\"" for s in c["added"]))
            html.append(f"<p><b>{esc(c['org'])}</b> <a href='{esc(c['url'])}'>{esc(c['url'])}</a></p><ul>"
                        + "".join(f"<li>{esc(s)}</li>" for s in c["added"]) + "</ul>")
    if dashboard:
        lines.append(f"\nDashboard: {dashboard}")
        html.append(f"<p><a href='{esc(dashboard)}'>Open the dashboard</a></p>")
    return subject, "\n".join(lines), "".join(html)


def send_email(subject, text, html):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("EMAIL_TO")
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    port = int(os.environ.get("SMTP_PORT") or 465)
    if not (host and to and user and pw):
        print("Email not configured (SMTP_HOST, SMTP_USER, SMTP_PASS, EMAIL_TO); skipping email.")
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    msg.set_content(text)
    msg.add_alternative(f"<div style='font-family:Arial,sans-serif;font-size:14px'>{html}</div>", subtype="html")
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
    print(f"Emailed {to}: {subject}")


# ---------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--orgs", default=str(ROOT / "orgs.yaml"))
    ap.add_argument("--state", default=str(ROOT / "state" / "state.json"))
    ap.add_argument("--out", default=str(ROOT / "docs" / "data.js"))
    ap.add_argument("--only", help="only check organisations whose name contains this text")
    ap.add_argument("--dry-run", action="store_true", help="don't send email")
    ap.add_argument("--seed", action="store_true", help="record current listings without alerting")
    ap.add_argument("--no-render", action="store_true", help="never use the headless browser")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(Path(args.orgs).read_text())
    orgs = (cfg.get("organisations") or []) + (cfg.get("job_boards") or [])
    if args.only:
        orgs = [o for o in orgs if args.only.lower() in o["name"].lower()]
        if not orgs:
            sys.exit(f"No organisation matches '{args.only}'")

    state_path = Path(args.state)
    state = load_state(state_path)
    f = Fetcher(os.environ.get("CONTACT_EMAIL", ""))
    render_ok = not args.no_render
    if render_ok:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            render_ok = False
            print("Playwright not installed: JavaScript-heavy pages will only get a basic check.")

    print(f"Checking {len(orgs)} organisations...")
    results: list[OrgResult] = []
    with ThreadPoolExecutor(MAX_WORKERS) as ex:
        futs = {ex.submit(check_org, o, f, state["careers_cache"], render_ok): o for o in orgs}
        for fut in as_completed(futs):
            o = futs[fut]
            try:
                r = fut.result()
            except Exception as e:  # noqa: BLE001
                r = OrgResult(o["name"], errors=[f"unexpected error: {e}"])
            results.append(r)
            flag = "skip" if r.skipped else ("ERR " if r.errors else "ok  ")
            print(f"  {flag} {r.name}: {len(r.postings)} found" + (f" | {r.errors[0][:90]}" if r.errors else ""))

    queue = [(next(o for o in orgs if o["name"] == r.name), u, r) for r in results for u in r.render_queue]
    if queue:
        print(f"Loading {len(queue)} JavaScript-heavy pages in a headless browser...")
        render_pass(queue, f)

    new_posts, changes, first_run = merge(state, orgs, results, args.seed)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    write_dashboard_data(state, orgs, Path(args.out))

    live = sum(1 for p in state["postings"].values() if p.get("active"))
    print(f"Done. {len(new_posts)} new listings, {len(changes)} page updates, {live} live in total.")
    if args.seed:
        return
    if new_posts or changes or first_run:
        subject, text, html = build_email(new_posts, changes, first_run, live, os.environ.get("DASHBOARD_URL", ""))
        if args.dry_run:
            print("\n--- email (dry run) ---\nSubject:", subject, "\n" + text)
        else:
            send_email(subject, text, html)


if __name__ == "__main__":
    main()
