#!/usr/bin/env python3
"""
MATH 285 seat watcher - UIUC Course Explorer, Spring 2027.

Checks every 30 minutes whether a 9:00 AM section of MATH 285 shows as open,
then alerts you with a desktop notification (and optionally a phone push).

No installs needed: Python 3.8+ standard library only.

Usage:
    python3 math285_watcher.py                 # run forever, check every 30 min
    python3 math285_watcher.py --once          # check one time and print the sections
    python3 math285_watcher.py --interval 15   # check every 15 min instead
    python3 math285_watcher.py --test-notify   # make sure alerts actually reach you

Phone alerts (optional, free): install the "ntfy" app (iOS/Android), subscribe to
a topic name only you know (e.g. karthik-math285-x7q2), then run:
    python3 math285_watcher.py --ntfy karthik-math285-x7q2
"""

import argparse
import json
import re
import platform
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

# ---- settings you might change -------------------------------------------
YEAR, TERM = "2027", "spring"
SUBJECT, NUMBER = "MATH", "285"
TARGET_START = "09:00 AM"          # section start time to watch
DEFAULT_INTERVAL_MIN = 30
# ---------------------------------------------------------------------------

BASE = "https://courses.illinois.edu/cisapp/explorer/schedule"
COURSE_URL = f"{BASE}/{YEAR}/{TERM}/{SUBJECT}/{NUMBER}.xml?mode=cascade"
PAGE_URL = f"https://courses.illinois.edu/schedule/{YEAR}/{TERM}/{SUBJECT}/{NUMBER}"
HEADERS = {"User-Agent": "MATH285-seat-watcher (personal student script)"}


def fetch_xml(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return ET.fromstring(resp.read())


def text(el, path):
    found = el.find(path)
    return found.text.strip() if found is not None and found.text else ""


def norm_time(t):
    """'9:00 AM' / '09:00 AM' / '0900' -> '09:00 AM' for comparison."""
    t = t.strip().upper()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H%M", "%H:%M"):
        try:
            return datetime.strptime(t, fmt).strftime("%I:%M %p")
        except ValueError:
            pass
    return t


def get_sections():
    """Return a list of dicts: crn, section, type, start, end, days, instructor, status."""
    root = fetch_xml(COURSE_URL)
    sections = []
    for s in root.iter("detailedSection"):
        sections.append(parse_section(s))
    # Fallback if cascade mode didn't inline the section details
    if not sections:
        for ref in root.iter("section"):
            href = ref.get("href")
            if href:
                sections.append(parse_section(fetch_xml(href)))
    return sections


def parse_section(s):
    meeting = s.find(".//meeting")
    start = end = days = mtype = instr = ""
    if meeting is not None:
        start = text(meeting, "start")
        end = text(meeting, "end")
        days = text(meeting, "daysOfTheWeek")
        mtype = text(meeting, "type")
        instr = "; ".join(
            i.text.strip() for i in meeting.iter("instructor") if i.text
        )
    return {
        "crn": s.get("id", ""),
        "section": text(s, "sectionNumber"),
        "type": mtype,
        "start": start,
        "end": end,
        "days": days,
        "instructor": instr,
        "status": find_status(s) or "Unknown",
    }


def is_open(status):
    st = status.lower()
    return st.startswith("open") or st == "crosslistopen"


def send_imessage(to, text):
    """Send an iMessage from this Mac's Messages app (macOS only)."""
    text = text.replace("\\", "\\\\").replace('"', "'")
    to = to.replace('"', "")
    script = f'''
tell application "Messages"
    set svc to 1st account whose service type = iMessage
    send "{text}" to participant "{to}" of svc
end tell'''
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"(iMessage failed: {r.stderr.strip()})")
    else:
        print(f"(iMessage sent to {to})")


def notify(title, message, ntfy_topic=None, imessage_to=None):
    print(f"\a*** {title}: {message}")
    if imessage_to:
        try:
            send_imessage(imessage_to, f"{title} {message} {PAGE_URL}")
        except Exception as e:
            print(f"(iMessage failed: {e})")
    system = platform.system()
    try:
        if system == "Darwin":
            safe = message.replace('"', "'")
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{safe}" with title "{title}" sound name "Glass"'],
                check=False)
        elif system == "Windows":
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$n=New-Object System.Windows.Forms.NotifyIcon;"
                "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
                f"$n.ShowBalloonTip(15000,'{title}','{message.replace(chr(39), '')}',"
                "[System.Windows.Forms.ToolTipIcon]::Info);Start-Sleep 16"
            )
            subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])
        else:
            subprocess.run(["notify-send", title, message], check=False)
    except Exception as e:  # notification failure shouldn't kill the watcher
        print(f"(desktop notification failed: {e})")

    if ntfy_topic:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{ntfy_topic}",
                data=message.encode(),
                headers={"Title": title, "Priority": "urgent", "Click": PAGE_URL},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"(phone push failed: {e})")


# Only enrollmentStatus means seats (Open / Closed / Open (Restricted)).
# sectionStatusCode "A" just means the section is Active, so it's ignored.
STATUS_TAGS = ("enrollmentStatus",)


def find_status(el):
    for tag in STATUS_TAGS:
        for found in el.iter(tag):
            if found.text and found.text.strip():
                return found.text.strip()
    return ""


_page_cache = {}


def strip_tags(s):
    return re.sub(r"<[^>]+>", " ", str(s)).replace("&nbsp;", " ").strip()


def page_sections(debug=False):
    """Read the availability column from the Course Explorer web page.
    The XML feed has no seat status, but the page embeds a
    `sectionDataObj = [...]` JSON array with an 'availability' field."""
    if "data" in _page_cache:
        return _page_cache["data"]
    req = urllib.request.Request(PAGE_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    m = re.search(r"sectionDataObj\s*=\s*(\[.*?\])\s*;", html, re.S)
    data = {}
    if m:
        for row in json.loads(m.group(1)):
            crn = re.sub(r"\D", "", strip_tags(row.get("crn", "")))
            data[crn] = row
    elif debug:
        print("\n--- sectionDataObj not found; page snippet ---")
        i = html.find("48595")
        print(html[max(0, i - 1500): i + 1500] if i >= 0 else html[:3000])
    _page_cache["data"] = data
    return data


STATUS_RE = re.compile(
    r"\b(Open\s*\(Restricted\)|CrossListOpen|Open|Closed|Full|Cancelled)\b")


def row_status(crn, debug=False):
    """Find the HTML table row for this CRN and read its availability text."""
    req = urllib.request.Request(PAGE_URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    i = html.find(f">{crn}<")
    if i < 0:
        return None
    start = html.rfind("<tr", 0, i)
    end = html.find("</tr>", i)
    row = html[start:end + 5] if start >= 0 and end >= 0 else html[i - 3000:i + 3000]
    if debug:
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "math285_debug_row.html")
        with open(path, "w") as f:
            f.write(row)
        print(f"\n(saved row HTML to {path})")
    text = " ".join(strip_tags(row).split())
    m = STATUS_RE.search(text)
    if m:
        return m.group(1)
    m = STATUS_RE.search(row)  # fall back to attributes, e.g. title="Closed"
    return m.group(1) if m else None


def section_status(crn, debug=False):
    try:
        st = row_status(crn, debug)
        if st:
            return st
    except Exception as e:
        if debug:
            print(f"(row parse failed: {e})")
    row = page_sections(debug).get(crn)
    if debug:
        print(f"\n--- page data for CRN {crn} ---\n{json.dumps(row, indent=1)[:3000]}")
    if not row:
        return "Unknown"
    for key in ("availability", "enrollmentStatus", "status"):
        if row.get(key):
            return " ".join(strip_tags(row[key]).split())
    return "Unknown"


def check(verbose=False, debug=False):
    _page_cache.clear()  # fresh page every check
    sections = get_sections()
    target = norm_time(TARGET_START)
    nine_am = [s for s in sections if norm_time(s["start"]) == target]
    for s in nine_am:
        if s["status"] == "Unknown" or debug:
            s["status"] = section_status(s["crn"], debug)

    if verbose or not nine_am:
        print(f"\nAll {SUBJECT} {NUMBER} sections ({TERM.title()} {YEAR}):")
        for s in sections:
            print(f"  {s['section']:<4} CRN {s['crn']:<6} {s['start']:>8}-{s['end']:<8} "
                  f"{s['days']:<5} {s['status']:<20} {s['instructor']}")
    if not sections:
        print("  (no sections published yet for this term)")
    elif not nine_am:
        print(f"  No section starting at {TARGET_START} found - check TARGET_START.")
    return [s for s in nine_am if is_open(s["status"])], nine_am


def main():
    ap = argparse.ArgumentParser(description="Watch for an open 9 AM MATH 285 seat.")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_MIN,
                    help="minutes between checks (default 30)")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--ntfy", metavar="TOPIC", help="ntfy.sh topic for phone alerts")
    ap.add_argument("--test-notify", action="store_true",
                    help="send a test alert and exit")
    ap.add_argument("--imessage", metavar="PHONE_OR_EMAIL",
                    help="also send an iMessage to this number/Apple ID (Mac only)")
    ap.add_argument("--state", metavar="FILE",
                    help="remember alerted sections between runs (for cloud/cron use)")
    ap.add_argument("--debug", action="store_true",
                    help="print the raw section data (for troubleshooting)")
    args = ap.parse_args()

    if args.test_notify:
        notify("MATH 285 watcher", "Test alert - notifications work!", args.ntfy, args.imessage)
        return

    print(f"Watching {SUBJECT} {NUMBER} {TARGET_START} sections, "
          f"{TERM.title()} {YEAR}. Ctrl+C to stop.")
    last_alerted = set()
    if args.state:
        try:
            with open(args.state) as f:
                last_alerted = set(json.load(f))
        except (OSError, ValueError):
            pass
    first = True
    while True:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            open_secs, nine_am = check(verbose=first or args.once,
                                    debug=args.debug)
            for s in nine_am:
                print(f"[{stamp}] {s['section']} (CRN {s['crn']}) {s['start']} "
                      f"{s['days']}: {s['status']}")
            new = [s for s in open_secs if s["crn"] not in last_alerted]
            if new:
                msg = ", ".join(f"{s['section']} CRN {s['crn']} ({s['status']})"
                                for s in new)
                notify("MATH 285 9AM seat OPEN!", f"{msg} - register now", args.ntfy, args.imessage)
            # re-arm alerts for sections that close again
            last_alerted = {s["crn"] for s in open_secs}
            if args.state:
                with open(args.state, "w") as f:
                    json.dump(sorted(last_alerted), f)
        except Exception as e:
            print(f"[{stamp}] check failed: {e} (will retry)")
        first = False
        if args.once:
            break
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
