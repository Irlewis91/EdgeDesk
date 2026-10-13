#!/usr/bin/env python3
"""Print today's EdgeDesk email (subject, HTML, plain text) for the morning scheduled task.

Makes sure the local server is up and the data is fresh first. Talks only to 127.0.0.1.

  python3 /Users/ian/EdgeDesk/daily_email.py
"""
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:%s" % os.environ.get("PORT", "8787")
MAX_AGE_MIN = 45


def get(path, method="GET"):
    req = urllib.request.Request(BASE + path, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def up():
    try:
        get("/api/history")
        return True
    except Exception:
        return False


def state():
    d = json.loads(get("/api/dashboard"))
    gen = (d.get("dashboard") or {}).get("generated")
    age = None
    if gen:
        age = (datetime.datetime.now(datetime.timezone.utc) - datetime.datetime.fromisoformat(gen)).total_seconds() / 60
    return d.get("running"), age, d.get("error")


def main():
    if not up():
        uid = str(os.getuid())
        if subprocess.call(["launchctl", "kickstart", "gui/%s/com.edgedesk.server" % uid]) != 0:
            subprocess.call(["launchctl", "bootstrap", "gui/" + uid,
                             os.path.expanduser("~/Library/LaunchAgents/com.edgedesk.server.plist")])
        for _ in range(30):
            time.sleep(10)
            if up():
                break
        else:
            print("ERROR: EdgeDesk server did not start")
            return 1

    running, age, err = state()
    if age is None or age > MAX_AGE_MIN:
        if not running:
            get("/api/refresh", "POST")
        for _ in range(40):                                  # up to 10 minutes
            time.sleep(15)
            running, age, err = state()
            if not running and age is not None and age <= MAX_AGE_MIN:
                break
    if age is None:
        print("ERROR: no data available%s" % ((" — " + err) if err else ""))
        return 1

    m = json.loads(get("/api/email"))
    print("DATA_AGE_MINUTES: %d" % age)
    print("=====SUBJECT")
    print(m["subject"])
    print("=====HTML")
    print(m["html"])
    print("=====TEXT")
    print(m["text"])
    print("=====END")
    return 0


if __name__ == "__main__":
    sys.exit(main())
