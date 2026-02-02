#!/usr/bin/env python3
import os
import re
import json
import hashlib
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

APP_NAME = "ath-events"
URL = "https://events.bostonathenaeum.org/en/"

# Files live next to this script
STATE_FILE = Path(__file__).with_name("state.json")
PRETTY_FILE = Path(__file__).with_name("events_pretty.json")
MD_FILE = Path(__file__).with_name("events.md")

# ntfy (allow Actions/local override via env vars)
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "ath-events-notifications")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_URL = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"

MONTHS = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4,
    "MAY": 5, "JUNE": 6, "JULY": 7, "AUGUST": 8,
    "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}
MONTH_BY_NUM = {v: k for k, v in MONTHS.items()}

DATE_HDR_RE = re.compile(
    r"^\s*(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{1,2}),\s*(\d{4})\s*$",
    re.I,
)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def notify(title: str, body: str) -> None:
    # Silence ntfy response body to keep logs clean.
    subprocess.run(
        ["curl", "-sS", "-o", "/dev/null", "-X", "POST", "-H", f"Title: {title}", "-d", body, NTFY_URL],
        check=False,
    )

def looks_like_event_url(u: str) -> bool:
    try:
        p = urlparse(u)
    except Exception:
        return False
    if p.netloc and p.netloc != "events.bostonathenaeum.org":
        return False
    path = p.path or ""
    if not path.startswith("/en/"):
        return False
    if path.rstrip("/") in ("/en", "/en/"):
        return False
    return True

def parse_date_header(s: str) -> Optional[Tuple[int, int, int]]:
    m = DATE_HDR_RE.match(norm(s))
    if not m:
        return None
    mon = m.group(1).upper()
    day = int(m.group(2))
    year = int(m.group(3))
    month = MONTHS.get(mon)
    if not month:
        return None
    return (year, month, day)

def weekday_abbrev(year: int, month: int, day: int) -> str:
    try:
        w = datetime(year, month, day).weekday()  # Mon=0..Sun=6
    except Exception:
        return "?"
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][w]

def normalize_status(s: str) -> str:
    s = norm(s).upper()
    # unify common variants
    s = s.replace("WAIT LISTED", "WAITLISTED")
    return s

@dataclass(frozen=True)
class Event:
    url: str
    year: int
    month: int
    day: int
    time_et: str
    status: str
    title: str
    venue: str
    keywords: Tuple[str, ...]

    def key(self) -> str:
        return self.url

    def date_str(self) -> str:
        mon = MONTH_BY_NUM.get(self.month, "")
        return f"{mon} {self.day}, {self.year}".strip()

    def when_str(self) -> str:
        mon = MONTH_BY_NUM.get(self.month, "")
        dow = weekday_abbrev(self.year, self.month, self.day)
        left = f"{mon} {self.day} ({dow})".strip()
        if self.time_et:
            return f"{left} {self.time_et} ET"
        return left

def event_to_dict(e: Event) -> dict:
    return {
        "url": e.url,
        "date": e.date_str(),
        "time_et": e.time_et,
        "status": e.status,
        "event_title": e.title,
        "venue": e.venue,
        "keywords": list(e.keywords),
    }

def dict_to_event(d: dict) -> Optional[Event]:
    try:
        url = norm(d.get("url", ""))
        date = norm(d.get("date", ""))
        time_et = norm(d.get("time_et", "")).upper()
        status = normalize_status(d.get("status", ""))
        title = norm(d.get("event_title", ""))
        venue = norm(d.get("venue", ""))
        keywords = tuple(norm(x) for x in (d.get("keywords") or []))

        m = DATE_HDR_RE.match(date)
        if not m:
            return None
        mon = m.group(1).upper()
        day = int(m.group(2))
        year = int(m.group(3))
        month = MONTHS.get(mon)
        if not month:
            return None

        return Event(
            url=url,
            year=year,
            month=month,
            day=day,
            time_et=time_et,
            status=status,
            title=title,
            venue=venue,
            keywords=keywords,
        )
    except Exception:
        return None

def is_children_family(e: Event) -> bool:
    return any(k.strip().lower() == "children's/family" for k in e.keywords)

def is_library_orientation(e: Event) -> bool:
    return "LIBRARY ORIENTATION TOUR" in e.title.upper()

def is_art_arch_tour(e: Event) -> bool:
    return norm(e.title).lower() == "art & architecture tour"

def is_saturday(e: Event) -> bool:
    try:
        return datetime(e.year, e.month, e.day).weekday() == 5
    except Exception:
        return False

def fmt_line(e: Event) -> str:
    status = f"[{e.status}] " if e.status else ""
    return f"- {e.when_str()} -- {status}{e.title}".strip()

# Baseline / tracked set: exclude orientation + children/family, and only keep Sat tours (not weekday tours).
def should_track(e: Event) -> bool:
    if is_library_orientation(e):
        return False
    if is_children_family(e):
        return False
    if is_art_arch_tour(e) and not is_saturday(e):
        return False
    return True

# "New events" notifications: exclude orientation + children/family + ALL Art&Arch tours (even Saturdays).
def should_notify_as_new_event(e: Event) -> bool:
    if is_library_orientation(e):
        return False
    if is_children_family(e):
        return False
    if is_art_arch_tour(e):
        return False
    return True

def load_state() -> Tuple[str, Dict[str, Event]]:
    if not STATE_FILE.exists():
        return ("", {})
    try:
        old = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        old_hash = old.get("hash", "") or ""
        out: Dict[str, Event] = {}
        for d in old.get("items", []):
            if not isinstance(d, dict):
                continue
            e = dict_to_event(d)
            if e and e.url:
                out[e.key()] = e
        return (old_hash, out)
    except Exception:
        return ("", {})

def save_state(now: str, events: List[Event], h: str) -> None:
    payload = {
        "app": APP_NAME,
        "checked_at": now,
        "url": URL,
        "hash": h,
        "items": [event_to_dict(e) for e in events],
    }
    STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def write_outputs(events: List[Event]) -> None:
    PRETTY_FILE.write_text(
        json.dumps([event_to_dict(e) for e in events], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_lines = ["# Boston Athenaeum events\n"]
    for e in events:
        kw = f" ({', '.join(e.keywords)})" if e.keywords else ""
        status = f"[{e.status}] " if e.status else ""
        md_lines.append(f"- {e.when_str()} -- {status}{e.title}{kw}".strip())
    MD_FILE.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

def fetch_events() -> List[Event]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)

        # Click "Load more" repeatedly.
        load_more = page.locator("button:has-text('Load more'), a:has-text('Load more')")
        def count_cards() -> int:
            return page.locator("a.product-item[href]").count()

        for _ in range(80):
            try:
                if load_more.count() == 0:
                    break
                btn = load_more.first
                if not btn.is_visible():
                    break
                before = count_cards()
                btn.click()
                page.wait_for_load_state("networkidle", timeout=30000)
                page.wait_for_timeout(500)  # render time
                after = count_cards()
                if after <= before:
                    break
            except PWTimeoutError:
                break
            except Exception:
                break

        # Fallback scroll (some lazy-load on scroll too)
        for _ in range(8):
            before = count_cards()
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(800)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            after = count_cards()
            if after <= before:
                break

        cards = page.locator("a.product-item[href]")
        n = cards.count()

        seen = set()
        out: List[Event] = []

        for i in range(n):
            a = cards.nth(i)

            href = a.get_attribute("href") or ""
            abs_url = urljoin("https://events.bostonathenaeum.org", href)
            if not looks_like_event_url(abs_url):
                continue

            # Title
            try:
                title = norm(a.locator(".product-item__name").first.inner_text(timeout=1000))
            except Exception:
                title = ""
            if not title:
                continue

            # Time (e.g., "6:00 PM")
            try:
                time_et = norm(a.locator("time").first.inner_text(timeout=1000)).upper()
            except Exception:
                time_et = ""

            # Venue
            try:
                venue = norm(a.locator(".product-item__venue").first.inner_text(timeout=1000))
            except Exception:
                venue = ""

            # Status (often in ".product-item__price")
            status = ""
            try:
                prices = a.locator(".product-item__price")
                for j in range(min(prices.count(), 3)):
                    s = norm(prices.nth(j).inner_text(timeout=500))
                    if s:
                        status = normalize_status(s)
                        break
            except Exception:
                status = ""

            # Keywords
            keywords: List[str] = []
            try:
                kw_spans = a.locator(".keyword-container .event-keyword span")
                for s in kw_spans.all_inner_texts():
                    s = norm(s)
                    if s:
                        keywords.append(s)
            except Exception:
                keywords = []

            # Date: use partition header (best), else badge.
            ymd = None
            try:
                hdr = a.locator(
                    "xpath=ancestor::div[contains(@class,'partition')][1]//h2[contains(@class,'separator-title')]//span"
                ).first
                ymd = parse_date_header(hdr.inner_text(timeout=1000))
            except Exception:
                ymd = None

            if not ymd:
                try:
                    mon = norm(a.locator(".bt-date-badge__month").first.inner_text(timeout=500)).upper()
                    day = int(norm(a.locator(".bt-date-badge__day").first.inner_text(timeout=500)))
                    year = datetime.now().year
                    month = MONTHS.get(mon)
                    if month:
                        ymd = (year, month, day)
                except Exception:
                    ymd = None

            if not ymd:
                continue

            year, month, day = ymd

            e = Event(
                url=abs_url,
                year=year,
                month=month,
                day=day,
                time_et=time_et,
                status=status,
                title=title,
                venue=venue,
                keywords=tuple(keywords),
            )

            if e.key() in seen:
                continue
            seen.add(e.key())
            out.append(e)

        browser.close()

        out.sort(key=lambda e: (e.year, e.month, e.day, e.time_et, e.title.lower(), e.url))
        return out

def main() -> None:
    notify_first_run = "--notify-first-run" in sys.argv

    now_dt = datetime.now()
    now = now_dt.isoformat(timespec="seconds")

    old_hash, old_events = load_state()
    old_by_key = old_events
    old_keys = set(old_by_key.keys())

    events = fetch_events()
    current_by_key = {e.key(): e for e in events}

    # Hash is based ONLY on event payload, not timestamps, so it’s stable.
    payload_items = [event_to_dict(e) for e in events]
    blob = json.dumps(payload_items, ensure_ascii=False, indent=2)
    h = sha256(blob)

    first_run = (old_hash == "")

    # Build tracked sets
    current_tracked = {k: e for k, e in current_by_key.items() if should_track(e)}
    old_tracked = {k: e for k, e in old_by_key.items() if should_track(e)}

    # New events (filtered)
    new_events = [e for k, e in current_tracked.items() if k not in old_tracked and should_notify_as_new_event(e)]
    new_events.sort(key=lambda e: (e.year, e.month, e.day, e.time_et, e.title.lower()))

    # Reopened Saturday tours: SOLD OUT -> not SOLD OUT
    reopened_sat_tours: List[Tuple[Event, str, str]] = []
    for k, cur in current_by_key.items():
        if not is_art_arch_tour(cur) or not is_saturday(cur):
            continue
        prev = old_by_key.get(k)
        if not prev:
            continue
        old_status = (prev.status or "").upper()
        new_status = (cur.status or "").upper()
        if old_status == "SOLD OUT" and new_status != "SOLD OUT":
            reopened_sat_tours.append((cur, old_status, new_status))
    reopened_sat_tours.sort(key=lambda t: (t[0].year, t[0].month, t[0].day, t[0].time_et))

    # Notifications
    if first_run and notify_first_run:
        baseline_list = sorted(current_tracked.values(), key=lambda e: (e.year, e.month, e.day, e.time_et, e.title.lower()))
        lines = [f"Baseline (current matching events): {len(baseline_list)}"]
        lines.extend(fmt_line(e) for e in baseline_list)
        notify("Athenaeum events: baseline", "\n".join(lines))

    if (not first_run) and (new_events or reopened_sat_tours):
        lines: List[str] = []
        if new_events:
            lines.append(f"New events: {len(new_events)}")
            lines.extend(fmt_line(e) for e in new_events)
        if reopened_sat_tours:
            if lines:
                lines.append("")
            lines.append(f"Saturday Art & Architecture Tour no longer sold out: {len(reopened_sat_tours)}")
            for e, old_s, new_s in reopened_sat_tours:
                when = e.when_str()
                lines.append(f"- {when} -- Art & Architecture Tour [{old_s} -> {new_s or 'AVAILABLE'}]")

        title_bits = []
        if new_events:
            title_bits.append(f"{len(new_events)} new")
        if reopened_sat_tours:
            title_bits.append(f"{len(reopened_sat_tours)} tour reopen")
        notify("Athenaeum events: " + ", ".join(title_bits), "\n".join(lines))

    # Write state + outputs only when payload changed (prevents spam commits).
    if (not first_run) and old_hash and h == old_hash:
        print(f"State: {STATE_FILE}")
        print(f"Items found: {len(events)}")
        print("Status: no changes (not rewriting state.json)")
        return

    save_state(now, events, h)
    write_outputs(events)

    print(f"State: {STATE_FILE}")
    print(f"Items found: {len(events)}")
    if first_run:
        print("Status: first run (baseline created" + (", notified)" if notify_first_run else ", no notification)"))
    else:
        if new_events or reopened_sat_tours:
            print("Status: notified and state updated")
        else:
            print("Status: no relevant changes (state updated because payload changed)")

if __name__ == "__main__":
    main()