#!/usr/bin/env python3
"""Downtime checker. Runs on the off-site laptop ("homelab") once a minute.

Checks the live app from outside the office PC and emails when it has been
down for 5 minutes in a row, and again when it is back. It sends one email per
event, not one per minute, and stays quiet 22:00-08:00 IST (the office PC is
switched off at night for now; remove QUIET_HOURS when it runs 24/7).

Quiet-hour rule: an outage that starts and ends inside quiet hours sends
nothing. An outage still going at 08:00 sends the DOWN email then.

The sender's app password is read from ~/.config/mdw-alert/smtp.env
(SMTP_USER=..., SMTP_PASS=...), file mode 600, never stored in the repo.
"""
import json
import os
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from email.message import EmailMessage

URL = "https://mdw.tail2734e7.ts.net:8443/login/"   # tailnet-only address of the live app
OFFICE_PC = "mdw"                                   # its Tailscale name
INTERNET_PROBE = "https://www.gstatic.com/generate_204"
ALERT_TO = "techteammdw@gmail.com"
FAILS_BEFORE_ALERT = 5                              # one check a minute -> 5 minutes
QUIET_HOURS = (22, 8)                               # no emails from 22:00 until 08:00; None = always
HOME = os.path.expanduser("~")
CONFIG = os.path.join(HOME, ".config/mdw-alert/smtp.env")
STATE = os.path.join(HOME, ".local/state/mdw-alert/state.json")
BACKUP_DIR = os.path.join(HOME, "backups/officepc")  # where pull_officepc_backups.sh keeps its files
STALE_DAYS = 4          # alert if no successful pull for this long (dead timer, long silent failure)


def load_config():
    cfg = {}
    with open(CONFIG) as f:
        for line in f:
            if "=" in line and not line.startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                cfg[k] = v
    return cfg


def get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except Exception as e:          # HTTPError carries a status; anything else is "no answer"
        return getattr(e, "code", None)


def tailscale_reaches_office():
    try:
        r = subprocess.run(["tailscale", "ping", "-c", "1", "--timeout", "5s", OFFICE_PC],
                           capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def in_quiet_hours(now):
    if not QUIET_HOURS:
        return False
    start, end = QUIET_HOURS
    return now.hour >= start or now.hour < end


def send(subject, body):
    cfg = load_config()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["SMTP_USER"], ALERT_TO, subject
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(cfg["SMTP_USER"], cfg["SMTP_PASS"].replace(" ", ""))
        s.send_message(msg)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {"fails": 0, "down_since": None, "alerted": False, "reason": ""}


def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(st, f)


def fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%d %b %H:%M")


def mins(seconds):
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def check_backups(st, now):
    """Email when the off-site backup pull has a real problem, once, and when it is fixed.

    pull_officepc_backups.sh writes 'ok', 'unreachable' (office PC off/offline, which is
    normal at night) or 'error: <why>' to last_status. Only 'error' alerts, plus a safety
    net for 'no successful pull for STALE_DAYS' (covers a dead timer)."""
    status = read_file(os.path.join(BACKUP_DIR, "last_status")) or ""
    last_ok = read_file(os.path.join(BACKUP_DIR, "last_success"))
    age_days = (time.time() - int(last_ok)) / 86400 if last_ok and last_ok.isdigit() else None

    problem = None
    if status.startswith("error"):
        problem = status
    elif age_days is None:
        problem = "no successful backup pull has been recorded yet"
    elif age_days > STALE_DAYS:
        problem = f"no successful backup pull for {age_days:.1f} days (last status: {status or 'unknown'})"

    if problem and not st.get("backup_alerted") and not in_quiet_hours(now):
        send("BACKUP PROBLEM: the off-site copy is not updating",
             f"{problem}\n\nLook at ~/backups/officepc/pull.log on the homelab laptop "
             f"(ssh homelab). The office PC's own backups are not affected.")
        st["backup_alerted"] = True
    elif not problem and st.get("backup_alerted"):
        send("RECOVERED: off-site backup is working again",
             f"The last pull succeeded ({age_days:.1f} days ago).")
        st["backup_alerted"] = False


def main():
    if "--test-email" in sys.argv:
        send("[test] MDW downtime checker", "If you can read this, the laptop can send alert emails.")
        print("test email sent")
        return

    now = datetime.now()
    st = load_state()

    # If the laptop itself has no internet we cannot tell whether the office is down
    # (and could not send an email anyway), so this minute does not count.
    if get(INTERNET_PROBE, 8) not in (200, 204):
        print("laptop has no internet; skipping this check")
        return

    check_backups(st, now)
    check_app(st, now)      # always saves the state, including the backup flags above


def check_app(st, now):
    status = get(URL)
    if status == 200:
        if st["alerted"]:
            send("RECOVERED: Matta app is back",
                 f"The app answered again at {fmt(time.time())}.\n"
                 f"It was down from {fmt(st['down_since'])} ({mins(time.time() - st['down_since'])}).")
        st.update(fails=0, down_since=None, alerted=False, reason="")
        save_state(st)
        return

    if tailscale_reaches_office():
        reason = f"The office PC is reachable but the app did not answer (HTTP {status or 'no response'}). Check the InventoryApp service."
    else:
        reason = "The office PC is not reachable over Tailscale: it is powered off, asleep, or its internet is down."

    if st["fails"] == 0:
        st["down_since"] = time.time() - 60
    st["fails"] += 1
    st["reason"] = reason

    if st["fails"] >= FAILS_BEFORE_ALERT and not st["alerted"] and not in_quiet_hours(now):
        send("DOWN: Matta app is not responding",
             f"The app has not answered since about {fmt(st['down_since'])} "
             f"({mins(time.time() - st['down_since'])}).\n\n{reason}\n\nChecked: {URL}")
        st["alerted"] = True
    save_state(st)


if __name__ == "__main__":
    main()
