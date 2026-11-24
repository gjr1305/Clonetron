"""
Clonetron machine settings.

Defaults are the values for our MakerBot Replicator 2X build. Another build
overrides any of them in ~/clonetron.json (or the file named in the
CLONETRON_CONFIG environment variable); see config/clonetron.example.json.
"""
import json
import os

SETTINGS_FILE = os.environ.get("CLONETRON_CONFIG", os.path.expanduser("~/clonetron.json"))
SCANS_DIR = os.path.expanduser("~/scans")          # calibration files live here

DEFAULTS = {
    "serial_port": "/dev/ttyACM0",
    "wells_mm": {                                  # mm from home: X to the left, Y towards you
        "BL": [66.0, 42.3], "BR": [66.0, 81.4],
        "ML": [105.1, 42.3], "MR": [105.1, 81.4],
        "FL": [144.2, 42.3], "FR": [144.2, 81.4],
    },
    "scan_order": ["BL", "BR", "MR", "ML", "FL", "FR"],
    "well_radius_mm": 17.4,
    "z_default_mm": 18.0,
    "z_limit_mm": 28.0,
    "z_backlash_mm": 0.5,
    "output_rotate_deg": 180,
    "web_port": 8000,
}


def load():
    cfg = json.loads(json.dumps(DEFAULTS))          # deep copy
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                user = json.load(f)
            cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
        except Exception as e:
            print(f"(couldn't read {SETTINGS_FILE}: {e}; using defaults)")
    return cfg
