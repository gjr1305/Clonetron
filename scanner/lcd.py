"""
The 20x4 front-panel LCD (HD44780, 4-bit mode on the Pi's GPIO).

Wiring (BCM numbers): RS 25, E 24, D4 23, D5 17, D6 27, D7 22, see
docs/lcd-wiring.png. If the display or its library isn't there (on a laptop,
or in the simulator) everything here quietly does nothing.
"""

COLS, ROWS = 20, 4


class Lcd:
    def __init__(self, enabled=True):
        self.dev = None
        self.shown = None
        if not enabled:
            return
        try:
            import RPi.GPIO as GPIO
            from RPLCD.gpio import CharLCD
            GPIO.setwarnings(False)
            self.dev = CharLCD(numbering_mode=GPIO.BCM, cols=COLS, rows=ROWS,
                               pin_rs=25, pin_e=24, pins_data=[23, 17, 27, 22],
                               compat_mode=True)
            self.dev.clear()
        except Exception as e:
            print(f"(LCD not available: {e})")
            self.dev = None

    def show(self, lines):
        """Show up to 4 lines; only rewrites the display when something changed."""
        lines = [(str(t) if t is not None else "")[:COLS].ljust(COLS) for t in list(lines)[:ROWS]]
        lines += [" " * COLS] * (ROWS - len(lines))
        if lines == self.shown or self.dev is None:
            self.shown = lines
            return
        try:
            for r, text in enumerate(lines):
                if self.shown is None or self.shown[r] != text:
                    self.dev.cursor_pos = (r, 0)
                    self.dev.write_string(text)
            self.shown = lines
        except Exception as e:
            print(f"(LCD write failed: {e})")
            self.shown = None

    def close(self, goodbye=("Clonetron", "stopped")):
        if self.dev is None:
            return
        try:
            self.show(goodbye)
            self.dev.close(clear=False)
        except Exception:
            pass
