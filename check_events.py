#!/usr/bin/env python3
import json
import re
import hashlib
import os
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

# Write state.json right next to this script
STATE_FILE = Path(__file__).with_name("state.json")

# Notifications via ntfy.sh
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "ath-events-notifications")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh")
NTFY_URL = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"

MONTHS = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4,
    "MAY": 5, "JUNE": 6, "JULY": 7, "AUGUST": 8,
    "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11, "DECEMBER": 12,
}

DATE_HDR_RE = re.compile(
    r"^\s*(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\s+(\d{1,2}),\s*(\d{4})\s*$",
    re.I,
)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def notify(title: str, body: str) -> None:
    # ntfy accepts headers like "Title:" for a nicer notification title
    subprocess.run(
        ["curl", "-sS", "-X", "POST", "-H", f"Title: {title}", "-d", body, NTFY_URL],
        check=False,
    )

def looks_like_event_url(u: str) -> bool:
    try:
        p = urlparse(u)
    except Exception:
        return False
    if p.netloc and p.netloc != "events.bostonathenaeum.org":
        return False
    path = (p.path or "")
    if not path.startswith("/en/"):
        return False
    if path.rstrip("/") in ("/en", "/en/"):
        return False
    return True

def normalize_status(s: str) -> str:
    s = norm(s).upper()
    s = s.replace("WAITLISTED", "WAITLISTED")
    s = s.replace("SOLD OUT", "SOLD OUT")
    s = s.replace("FREE", "FREE")
    return s

def parse_date_header(s: str) -> Optional[Tuple[int, int, int]]:
    """
    Returns (year, month, day) from strings like "FEBRUARY 25, 2026".
    """
    m = DATE_HDR_RE.match(norm(s))
    if not m:
        return None
    mon, day, year = m.group(1).upper(), int(m.group(2)), int(m.group(3))
    month = MONTHS.get(mon)
    if not month:
        return None
    return (year, month, day)

def weekday_abbrev(year: int, month: int, day: int) -> str:
    # 0=Mon ... 5=Sat 6=Sun
    try:
        w = datetime(year, month, day).weekday()
    except Exception:
        return "?"
    return ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][w]

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

    def date_str(self) -> str:
        # "FEBRUARY 25, 2026"
        month_name = [k for k,v in MONTHS.items() if v == self.month][0]
        return f"{month_name} {self.day}, {self.year}"

    def when_str(self) -> str:
        dow = weekday_abbrev(self.year, self.month, self.day)
        # "FEBRUARY 25 (Tue) 6:00 PM ET"
        month_name = [k for k,v in MONTHS.items() if v == self.month][0]
        parts = [f"{month_name} {self.day} ({dow})"]
        if self.time_et:
            parts.append(f"{self.time_et} ET")
        return " ".join(parts)

    def key(self) -> str:
        # Stable identity across runs
        return self.url

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
        url = d.get("url", "")
        date = norm(d.get("date", ""))
        time_et = norm(d.get("time_et", ""))
        status = norm(d.get("status", ""))
        title = norm(d.get("event_title", ""))
        venue = norm(d.get("venue", ""))
        keywords = tuple(norm(x) for x in (d.get("keywords") or []))

        m = DATE_HDR_RE.match(date)
        if not m:
            return None
        mon, day, year = m.group(1).upper(), int(m.group(2)), int(m.group(3))
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

def should_track_for_notifications(e: Event) -> bool:
    title_u = e.title.upper()

    # Exclude Library Orientation Tour
    if "LIBRARY ORIENTATION TOUR" in title_u:
        return False

    # Exclude Children's/Family
    if any(k.strip().lower() == "children's/family" for k in e.keywords):
        return False

    # For Art & Architecture Tour: only keep Saturdays
    if is_art_arch_tour(e) and not is_saturday(e):
        return False

    return True

def is_art_arch_tour(e: Event) -> bool:
    return norm(e.title).lower() == "art & architecture tour"

def is_saturday(e: Event) -> bool:
    try:
        return datetime(e.year, e.month, e.day).weekday() == 5
    except Exception:
        return False

def format_line(e: Event) -> str:
    # No URLs per your request
    status = f"[{e.status}] " if e.status else ""
    return f"- {e.when_str()} -- {status}{e.title}"

def load_state() -> Tuple[str, Dict[str, Event]]:
    if not STATE_FILE.exists():
        return ("", {})
    try:
        old = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        old_hash = old.get("hash", "") or ""
        events = {}
        for d in old.get("items", []):
            e = dict_to_event(d)
            if e and e.url:
                events[e.key()] = e
        return (old_hash, events)
    except Exception:
        return ("", {})

def save_state(now: str, items: List[Event], h: str) -> None:
    payload = {
        "app": APP_NAME,
        "checked_at": now,
        "url": URL,
        "hash": h,
        "items": [event_to_dict(e) for e in items],
    }
    STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

def fetch_events() -> List[Event]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=60000)

        # Repeatedly click "Load more" until it stops growing.
        load_more = page.locator("button:has-text('Load more'), a:has-text('Load more')")
        max_clicks = 80

        def count_cards() -> int:
            return page.locator("a.product-item[href]").count()

        for _ in range(max_clicks):
            try:
                if load_more.count() == 0:
                    break
                btn = load_more.first
                if not btn.is_visible():
                    break

                before = count_cards()
                btn.click()
                page.wait_for_load_state("networkidle", timeout=30000)

                # Wait a bit for Angular rendering
                page.wait_for_timeout(600)

                after = count_cards()
                if after <= before:
                    break
            except PWTimeoutError:
                break
            except Exception:
                break

        # Some pages also lazy-load on scroll (belt + suspenders).
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

            # Title: h2.product-item__name
            try:
                title = norm(a.locator(".product-item__name").first.inner_text(timeout=1000))
            except Exception:
                title = ""

            if not title:
                continue

            # Time: <time>...</time>
            try:
                time_et = norm(a.locator("time").first.inner_text(timeout=1000)).upper()
            except Exception:
                time_et = ""

            # Venue: .product-item__venue
            try:
                venue = norm(a.locator(".product-item__venue").first.inner_text(timeout=1000))
            except Exception:
                venue = ""

            # Status: .product-item__price (may appear twice; pick first non-empty)
            status = ""
            try:
                prices = a.locator(".product-item__price")
                pc = prices.count()
                for j in range(min(pc, 3)):
                    s = norm(prices.nth(j).inner_text(timeout=500))
                    if s:
                        status = normalize_status(s)
                        break
            except Exception:
                status = ""

            # Keywords: buttons under .keyword-container
            keywords: List[str] = []
            try:
                kws = a.locator(".keyword-container .event-keyword span")
                for s in kws.all_inner_texts():
                    s = norm(s)
                    if s:
                        keywords.append(s)
            except Exception:
                keywords = []

            # Date: find nearest partition header, e.g. "FEBRUARY 25, 2026"
            date_txt = ""
            ymd = None
            try:
                hdr = a.locator(
                    "xpath=ancestor::div[contains(@class,'partition')][1]//h2[contains(@class,'separator-title')]//span"
                ).first
                date_txt = norm(hdr.inner_text(timeout=1000))
                ymd = parse_date_header(date_txt)
            except Exception:
                ymd = None

            if not ymd:
                # Fallback: month/day badge + current year
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

        # Stable sort by date/time/title
        def k(e: Event):
            return (e.year, e.month, e.day, e.time_et, e.title.lower(), e.url)
        out.sort(key=k)
        return out

def main() -> None:
    notify_first_run = "--notify-first-run" in sys.argv

    now = datetime.now().isoformat(timespec="seconds")
    old_hash, old_events = load_state()

    events = fetch_events()
    payload_items = [event_to_dict(e) for e in events]
    blob = json.dumps(payload_items, ensure_ascii=False, indent=2)
    h = sha256(blob)

    first_run = (old_hash == "")

    # Build filtered views for notifications
    current_by_key = {e.key(): e for e in events}
    old_by_key = old_events

    current_tracked = {k: e for k, e in current_by_key.items() if should_track_for_notifications(e)}
    old_tracked = {k: e for k, e in old_by_key.items() if should_track_for_notifications(e)}

    # (1) New tracked events
    new_events = [e for k, e in current_tracked.items() if k not in old_tracked]

    # (2) Art & Architecture Tour on Saturdays: SOLD OUT -> not SOLD OUT
    reopened_sat_tours: List[Event] = []
    for k, cur in current_by_key.items():
        if not is_art_arch_tour(cur) or not is_saturday(cur):
            continue
        prev = old_by_key.get(k)
        if not prev:
            continue
        if prev.status.upper() == "SOLD OUT" and cur.status.upper() != "SOLD OUT":
            reopened_sat_tours.append(cur)

    should_notify = (len(new_events) > 0) or (len(reopened_sat_tours) > 0) or (first_run and notify_first_run)

    if should_notify:
        if first_run:
            baseline = sorted(current_tracked.values(), key=lambda e: (e.year, e.month, e.day, e.time_et, e.title.lower()))
            lines = [format_line(e) for e in baseline]
            body = f"Baseline (current matching events): {len(baseline)}\n" + "\n".join(lines)
            notify("Athenaeum events: baseline", body)
        else:
            parts: List[str] = []
            if new_events:
                parts.append(f"New events: {len(new_events)}")
                parts.extend(format_line(e) for e in new_events)
            if reopened_sat_tours:
                if parts:
                    parts.append("")
                parts.append(f"Art & Architecture Tour reopened (Sat): {len(reopened_sat_tours)}")
                parts.extend(format_line(e) for e in reopened_sat_tours)

            notify("Athenaeum events updated", "\n".join(parts))

    save_state(now, events, h)

    # Optional convenience outputs next to the script
    Path(__file__).with_name("events_pretty.json").write_text(
        json.dumps(payload_items, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md = Path(__file__).with_name("events.md")
    md_lines = ["# Boston Athenaeum events\n"]
    for e in events:
        status = f"[{e.status}] " if e.status else ""
        kw = f" ({', '.join(e.keywords)})" if e.keywords else ""
        md_lines.append(f"- {e.when_str()} -- {status}{e.title}{kw}")
    md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    print(f"State: {STATE_FILE}")
    print(f"Items found: {len(events)}")
    if first_run:
        print("Status: first run (baseline created" + (", notified)" if notify_first_run else ", no notification)"))
    else:
        print("Status: notified" if should_notify else "Status: no relevant changes")

if __name__ == "__main__":
    main()