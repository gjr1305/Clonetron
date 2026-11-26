"""
USB stick handling for the scanner app: find the stick, check space, eject.

Scans are only ever saved to a USB stick, never to the Pi's SD card. On the
Pi, USB sticks show up as /dev/sda1, /dev/sdb1 ... (the SD card is
/dev/mmcblk0), so anything mounted from /dev/sd* counts as a stick.
"""
import json
import os
import shutil
import subprocess
import tempfile
import zipfile

SCAN_SUBFOLDER = "Clonetron"            # scans go in <stick>/Clonetron/<date>_<name>.clonescan
SIM_ROOT = os.path.join(tempfile.gettempdir(), "clonetron-sim-usb")


def _run(cmd, timeout=20):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _writable(path):
    try:
        with tempfile.NamedTemporaryFile(dir=path):
            return True
    except Exception:
        return False


def sticks(sim=False):
    """Mounted USB sticks: [{dev, mount, label, free, total, writable}]"""
    out = []
    if sim:
        if os.path.isdir(SIM_ROOT):
            u = shutil.disk_usage(SIM_ROOT)
            out.append({"dev": "sim", "mount": SIM_ROOT, "label": "SIM-USB", "free": u.free,
                        "total": u.total, "writable": True})
        return out
    try:
        with open("/proc/mounts") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        parts = line.split()
        if len(parts) < 3 or not parts[0].startswith("/dev/sd"):
            continue
        dev, mount = parts[0], parts[1].replace("\\040", " ")
        try:
            u = shutil.disk_usage(mount)
        except OSError:
            continue
        out.append({"dev": dev, "mount": mount, "label": os.path.basename(mount) or dev,
                    "free": u.free, "total": u.total, "writable": _writable(mount)})
    return out


def unmounted(sim=False):
    """USB partitions that are plugged in but not opened (mounted) yet."""
    if sim:
        return []
    code, txt = _run(["lsblk", "-nrpo", "NAME,TYPE,MOUNTPOINT"])
    if code:
        return []
    found = []
    for line in txt.splitlines():
        p = line.split()
        if len(p) >= 2 and p[0].startswith("/dev/sd") and p[1] == "part" and len(p) == 2:
            found.append(p[0])
    return found


def _fstype(dev):
    code, txt = _run(["lsblk", "-no", "FSTYPE", dev])
    return txt.strip() if code == 0 else ""


def _explain(dev, err):
    """Turn a udisksctl error into something a person can act on."""
    fs = _fstype(dev)
    if "not authorized" in err.lower():
        return ("The app isn't allowed to open USB sticks. On the Pi run: "
                "cd ~/clonetron/scanner && bash install_service.sh")
    if fs == "ntfs":
        return ("This stick is formatted as NTFS, which the Pi can't open without an extra "
                "driver. Either run  sudo apt install ntfs-3g  on the Pi, or reformat the "
                "stick as exFAT on a laptop (this erases it).")
    if fs == "exfat":
        return "This exFAT stick couldn't be opened. On the Pi run:  sudo apt install exfatprogs"
    if not fs:
        return ("The stick has no format the Pi recognises. Format it as exFAT or FAT32 "
                "on a laptop (this erases it).")
    if "wrong fs type" in err or "superblock" in err:
        return (f"The stick ({fs}) looks damaged or wasn't removed safely. Check it on a laptop, "
                "or reformat it as exFAT.")
    return "Couldn't open the stick: " + err.strip().splitlines()[-1][:200]


_last_try = {}          # dev -> (time, reason): don't retry a failing stick every poll


def try_mount(dev):
    """Open a stick that wasn't opened automatically (needs udisks2, standard on Pi OS).
    Returns None if it opened, else a plain-English reason."""
    import time
    t, reason = _last_try.get(dev, (0, None))
    if reason and time.time() - t < 15:
        return reason
    code, txt = _run(["udisksctl", "mount", "-b", dev, "--no-user-interaction"])
    reason = None if code == 0 else _explain(dev, txt or "")
    _last_try[dev] = (time.time(), reason)
    return reason


def get(sim=False, reasons=None):
    """The stick to save to, opening one if it's plugged in but not mounted.
    If `reasons` is a list, why any stick couldn't be opened is added to it."""
    s = sticks(sim)
    if not s:
        for dev in unmounted(sim):
            r = try_mount(dev)
            if r and reasons is not None:
                reasons.append(r)
        s = sticks(sim)
    return s[0] if s else None


def eject(stick, sim=False):
    """Finish writing, unmount and power the stick off. Returns an error or None."""
    _run(["sync"], timeout=120)
    if sim:
        return None
    code, txt = _run(["udisksctl", "unmount", "-b", stick["dev"], "--no-user-interaction"], 60)
    if code:
        return "couldn't unmount: " + txt
    disk = stick["dev"].rstrip("0123456789")
    _run(["udisksctl", "power-off", "-b", disk, "--no-user-interaction"])
    return None


def _manifest(path):
    try:
        with zipfile.ZipFile(path) as z:
            return json.loads(z.read("clonescan.json"))
    except Exception:
        return None


def recent_scans(stick, n=10):
    """Newest scans on the stick: .clonescan files, plus any scan still in a folder
    (one being scanned now, or one whose packing failed)."""
    root = os.path.join(stick["mount"], SCAN_SUBFOLDER)
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted((x for x in os.listdir(root) if not x.endswith(".part")), reverse=True):
        d = os.path.join(root, name)
        if name.endswith(".clonescan"):
            m = _manifest(d)
            out.append({"name": name, "photos": m["photos"] if m else 0, "packed": True,
                        "complete": bool(m and m.get("complete")), "bad": m is None,
                        "mb": round(os.path.getsize(d) / 1e6)})
        elif os.path.isdir(d):
            try:
                photos = sum(1 for f in os.scandir(d)
                             if f.name.endswith(".jpg") and "_stitched" not in f.name)
            except OSError:
                photos = 0
            out.append({"name": name, "photos": photos, "packed": False})
        if len(out) >= n:
            break
    return out
