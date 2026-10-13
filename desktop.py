#!/usr/bin/env python3
"""Desktop app entry point (what the macOS and Windows downloads run).

Starts EdgeDesk on this computer, opens it in the browser, and shows a small window with
"Open EdgeDesk" and "Quit". If EdgeDesk is already running, it just opens the browser.
"""
import os
import sys
import threading
import time
import urllib.request
import webbrowser

import paths

PORT = int(os.environ.get("PORT", 8787))
URL = "http://127.0.0.1:%d" % PORT


def show():
    if not os.environ.get("EDGEDESK_NO_BROWSER"):       # set only by automated build checks
        webbrowser.open(URL)


def up():
    try:
        urllib.request.urlopen(URL + "/api/history", timeout=2).read()
        return True
    except Exception:
        return False


def _log_to_file():
    """A windowed app has no console: send output to a log file next to the data."""
    try:
        d = os.path.join(paths.HOME, "logs")
        os.makedirs(d, exist_ok=True)
        fh = open(os.path.join(d, "edgedesk.log"), "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stderr = fh
    except Exception:
        pass


def _serve():
    import server
    sys.argv = [sys.argv[0], "--port", str(PORT)]
    server.main()


def _window():
    """Small control window. Returns False if no window toolkit is available."""
    try:
        import tkinter as tk
    except Exception:
        return False
    try:
        root = tk.Tk()
    except Exception:
        return False
    root.title("EdgeDesk")
    root.resizable(False, False)
    root.configure(bg="#14172E", padx=26, pady=22)
    tk.Label(root, text="EdgeDesk", font=("Helvetica", 22, "bold"), fg="#C6F432", bg="#14172E").pack(anchor="w")
    status = tk.Label(root, text="Starting…", font=("Helvetica", 12), fg="#ECEEFF", bg="#14172E", justify="left", wraplength=300)
    status.pack(anchor="w", pady=(6, 16))
    row = tk.Frame(root, bg="#14172E")
    row.pack(anchor="w")
    tk.Button(row, text="Open EdgeDesk", command=show, padx=10, pady=4).pack(side="left")
    tk.Button(row, text="Quit", command=root.destroy, padx=10, pady=4).pack(side="left", padx=(10, 0))
    tk.Label(root, text="Keep this window open while you use EdgeDesk.\nData: " + paths.HOME, font=("Helvetica", 10),
             fg="#9BA1CC", bg="#14172E", justify="left", wraplength=300).pack(anchor="w", pady=(16, 0))
    state = {"opened": False}

    def tick():
        if up():
            status.config(text="Running at %s\nThe first load takes a few minutes while it gathers data." % URL)
            if not state["opened"]:
                state["opened"] = True
                show()
        root.after(1500, tick)
    root.after(500, tick)
    try:                                       # show the window in front once, then behave normally
        root.lift()
        root.attributes("-topmost", True)
        root.after(800, lambda: root.attributes("-topmost", False))
    except Exception:
        pass
    root.mainloop()
    return True


def main():
    if up():                                  # another copy is already serving: just show it
        show()
        return 0
    if paths.FROZEN or sys.stdout is None:
        _log_to_file()
    threading.Thread(target=_serve, daemon=True).start()
    if os.environ.get("EDGEDESK_HEADLESS") or not _window():
        for _ in range(120):                  # no window available: open the browser and keep serving
            if up():
                show()
                break
            time.sleep(0.5)
        while True:
            time.sleep(3600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
