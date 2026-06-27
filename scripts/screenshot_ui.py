#!/usr/bin/env python
"""Capture a screenshot of the running web UI (form + M87 results) for the README.

Runs the Flask app in a background thread, drives it with headless Chrome via
Selenium, and saves ``screenshots/app_m87.png``. The bundled M87 demo cache
makes the results render instantly.

Requires Selenium and Google Chrome:  pip install selenium
Run from the project root:  PYTHONPATH=. python scripts/screenshot_ui.py
"""

from __future__ import annotations

import os
import threading
import time
import urllib.request

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from app import app

PORT = 5050
OBJECT = "M87"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_ROOT, "screenshots", "app_m87.png")


def _serve() -> None:
    app.run(host="127.0.0.1", port=PORT, use_reloader=False, threaded=True)


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    threading.Thread(target=_serve, daemon=True).start()

    url = f"http://127.0.0.1:{PORT}/"
    for _ in range(50):  # wait for the server to come up
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except OSError:
            time.sleep(0.2)

    opts = Options()
    for a in ("--headless=new", "--no-sandbox", "--disable-gpu",
              "--disable-dev-shm-usage", "--window-size=1366,1000",
              "--force-device-scale-factor=2", "--hide-scrollbars"):
        opts.add_argument(a)
    driver = webdriver.Chrome(options=opts)
    try:
        driver.get(url)
        wait = WebDriverWait(driver, 40)
        pos = wait.until(EC.presence_of_element_located((By.ID, "position")))
        pos.clear()
        pos.send_keys(OBJECT)
        driver.find_element(By.ID, "submit").click()
        # Wait until every result preview image has actually loaded.
        wait.until(lambda d: d.execute_script(
            "const i=[...document.querySelectorAll('.card img')];"
            "return i.length>=7 && i.every(x=>x.complete && x.naturalWidth>0);"))
        time.sleep(1.0)
        height = driver.execute_script("return Math.ceil(document.body.scrollHeight)")
        driver.set_window_size(1366, height + 30)
        time.sleep(0.8)
        driver.save_screenshot(OUT)
        print(f"wrote {os.path.relpath(OUT, _ROOT)} ({height}px tall)")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
