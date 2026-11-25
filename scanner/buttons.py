"""
The five front-panel buttons (UP, DOWN, LEFT, RIGHT, CENTRE).

Which Pi pin each button is on comes from ~/scans/buttons.json, written by the
button-learning script (BCM numbers, e.g. {"UP": 12, "DOWN": 5, ...}). Each
button joins its pin to ground when pressed; the Pi's internal pull-ups keep
the pins high otherwise. Without the file or the GPIO library (laptop,
simulator) nothing is read, and presses can still come from the web page.
"""
import json
import os
import threading
import time

import config

MAP_FILE = os.path.join(config.SCANS_DIR, "buttons.json")
NAMES = ("UP", "DOWN", "LEFT", "RIGHT", "CENTRE")


class Buttons:
    def __init__(self, on_press, enabled=True):
        self.on_press = on_press
        self.pins = {}
        self.ok = False
        if not enabled:
            return
        try:
            with open(MAP_FILE) as f:
                m = json.load(f)
            self.pins = {n: int(m[n]) for n in NAMES if n in m}
        except Exception as e:
            print(f"(buttons: no {MAP_FILE}: {e})")
            return
        try:
            import RPi.GPIO as GPIO
            self.GPIO = GPIO
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            for p in self.pins.values():
                GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.ok = True
            threading.Thread(target=self._loop, daemon=True).start()
            print("Buttons:", ", ".join(f"{n}=GPIO{p}" for n, p in self.pins.items()))
        except Exception as e:
            print(f"(buttons not available: {e})")

    def _loop(self):
        G = self.GPIO
        last = {n: 1 for n in self.pins}
        t_last = {n: 0.0 for n in self.pins}
        while True:
            now = time.time()
            for n, p in self.pins.items():
                try:
                    v = G.input(p)
                except Exception:
                    continue
                if v == 0 and last[n] == 1 and now - t_last[n] > 0.25:   # pressed (debounced)
                    t_last[n] = now
                    try:
                        self.on_press(n)
                    except Exception as e:
                        print("button handler error:", e)
                last[n] = v
            time.sleep(0.015)
