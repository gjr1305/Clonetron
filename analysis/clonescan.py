#!/usr/bin/env python3
"""
The .clonescan file: one scan of one plate in a single file.

While scanning, the Pi writes the photos into a normal folder on the USB stick
(so a power cut can't spoil a half-written file). When the scan ends, pack()
puts everything into  <date>_<time>_<name>.clonescan  next to it, checks every
photo in the new file can be read back, and only then removes the folder.

Inside, a .clonescan is an ordinary zip file:
  clonescan.json      what this file is: format version, plate name, wells,
                      photo count, whether the scan finished
  positions.csv       where each photo was taken (stage mm)
  focus.csv           focus checks and re-shoots
  scan_info.json      scan settings, field of view, well centres
  calibration.json    camera pixel-to-mm calibration
  camera.json         exposure and colour gains
  flat.npy            blank light-pad image, used to remove the pink corners
  focus_map.json      the focus map the scan used
  photos/BL_0000.jpg  the raw photos, exactly as the camera took them

The photos are stored as taken (pink corners included). The analysis app
removes the corners with flat.npy when it opens them, so a scan can always be
re-corrected later. Because it's a zip, the photos can still be got out
without the app if ever needed (rename to .zip, or: python clonescan.py extract).

Command line:
  python clonescan.py info  SCAN.clonescan
  python clonescan.py extract SCAN.clonescan [FOLDER]
  python clonescan.py pack  SCAN_FOLDER          (folder -> .clonescan, removes the folder)
"""
import json
import os
import shutil
import sys
import time
import zipfile

FORMAT = "clonescan"
VERSION = 1
EXT = ".clonescan"
MANIFEST = "clonescan.json"
PHOTO_DIR = "photos/"


def _is_photo(name):
    return name.lower().endswith(".jpg") and "_stitched" not in name


def pack(folder, complete=True, remove=True, log=print):
    """Pack a scan folder into <folder>.clonescan. Returns the file's path.
    The folder is removed only after every entry has been read back and checked."""
    folder = folder.rstrip("/\\")
    out = folder + EXT
    part = out + ".part"
    names = sorted(os.listdir(folder))
    photos = [n for n in names if _is_photo(n)]
    others = [n for n in names if n not in photos and os.path.isfile(os.path.join(folder, n))]
    info = {}
    try:
        with open(os.path.join(folder, "scan_info.json")) as f:
            info = json.load(f)
    except (OSError, ValueError):
        pass
    wells = info.get("wells") or sorted({n.split("_")[0] for n in photos})
    base = os.path.basename(folder)
    manifest = {"format": FORMAT, "version": VERSION, "name": base,
                "plate": base[18:] if len(base) > 18 and base[17] == "_" else "",
                "made": info.get("made") or time.strftime("%Y-%m-%d %H:%M:%S"),
                "packed": time.strftime("%Y-%m-%d %H:%M:%S"),
                "complete": bool(complete), "wells": wells, "photos": len(photos),
                "photo_folder": PHOTO_DIR, "flat_field": "flat.npy" in names}
    log(f"Packing {len(photos)} photos into {os.path.basename(out)}...")
    t0 = time.time()
    try:
        _write(folder, part, manifest, others, photos, log)
    except BaseException:
        try:
            os.remove(part)                                # never leave half a file behind
        except OSError:
            pass
        raise
    os.replace(part, out)
    if remove:
        shutil.rmtree(folder, ignore_errors=True)
    mb = os.path.getsize(out) / 1e6
    log(f"Saved {os.path.basename(out)}  ({mb:.0f} MB, {time.time() - t0:.0f} s)")
    return out


def _write(folder, part, manifest, others, photos, log):
    with zipfile.ZipFile(part, "w", allowZip64=True) as z:
        z.writestr(MANIFEST, json.dumps(manifest, indent=1), zipfile.ZIP_DEFLATED)
        for n in others:                                   # small text/calibration files
            z.write(os.path.join(folder, n), n, zipfile.ZIP_DEFLATED)
        for i, n in enumerate(photos, 1):                  # JPEGs don't shrink: store as is
            z.write(os.path.join(folder, n), PHOTO_DIR + n, zipfile.ZIP_STORED)
            if i % 200 == 0:
                log(f"  packed {i}/{len(photos)}")
    with open(part, "rb") as f:                            # make sure it's really on the stick
        os.fsync(f.fileno())
    log("Checking the file...")
    with zipfile.ZipFile(part) as z:
        bad = z.testzip()                                  # reads every entry, checks its CRC
        inside = set(z.namelist())
    missing = [n for n in photos if PHOTO_DIR + n not in inside]
    if bad or missing:
        raise OSError(f"the packed file failed its check ({bad or missing[0]}); "
                      f"the photos are still in {folder}")


class Scan:
    """Read a scan from a .clonescan file or from a scan folder, the same way.
    Photo names are the plain ones from positions.csv (BL_0000.jpg)."""

    def __init__(self, path):
        self.path = os.path.expanduser(path.rstrip("/\\"))
        self.zip = None
        self.manifest = {}
        if os.path.isdir(self.path):
            self.folder = self.path
        elif zipfile.is_zipfile(self.path):
            self.zip = zipfile.ZipFile(self.path)
            self._names = set(self.zip.namelist())
            if MANIFEST not in self._names:
                raise ValueError(f"{os.path.basename(self.path)} is a zip but not a Clonetron scan")
            self.manifest = json.loads(self.zip.read(MANIFEST))
            if self.manifest.get("version", 1) > VERSION:
                raise ValueError("this scan was made by a newer Clonetron; update the app")
        else:
            raise ValueError(f"{self.path} is not a scan folder or a .clonescan file")

    @property
    def name(self):
        b = os.path.basename(self.path)
        return b[:-len(EXT)] if b.endswith(EXT) else b

    def results_folder(self):
        """Where stitched pictures and results go: the folder itself, or
        '<name> results' next to a .clonescan file (the scan file is never changed)."""
        if self.zip is None:
            return self.folder
        d = os.path.join(os.path.dirname(self.path), self.name + " results")
        os.makedirs(d, exist_ok=True)
        return d

    def _member(self, name):
        if self.zip is None:
            return None
        if _is_photo(name) and PHOTO_DIR + name in self._names:
            return PHOTO_DIR + name
        return name if name in self._names else None

    def has(self, name):
        if self.zip is None:
            return os.path.exists(os.path.join(self.folder, name))
        return self._member(name) is not None

    def read_bytes(self, name):
        if self.zip is None:
            with open(os.path.join(self.folder, name), "rb") as f:
                return f.read()
        m = self._member(name)
        if m is None:
            raise FileNotFoundError(name)
        return self.zip.read(m)

    def read_text(self, name):
        return self.read_bytes(name).decode("utf-8")

    def read_json(self, name, default=None):
        return json.loads(self.read_bytes(name)) if self.has(name) else default

    def read_image(self, name):
        import cv2
        import numpy as np
        try:
            data = np.frombuffer(self.read_bytes(name), np.uint8)
        except (OSError, KeyError):
            return None
        return cv2.imdecode(data, cv2.IMREAD_COLOR)

    def load_npy(self, name):
        import io
        import numpy as np
        return np.load(io.BytesIO(self.read_bytes(name))) if self.has(name) else None

    def photos(self):
        if self.zip is None:
            return sorted(n for n in os.listdir(self.folder) if _is_photo(n))
        return sorted(n[len(PHOTO_DIR):] for n in self._names
                      if n.startswith(PHOTO_DIR) and _is_photo(n))

    def extract(self, dest):
        os.makedirs(dest, exist_ok=True)
        if self.zip is None:
            raise ValueError("already a folder")
        for m in self.zip.namelist():
            target = os.path.join(dest, m[len(PHOTO_DIR):] if m.startswith(PHOTO_DIR) else m)
            if os.path.dirname(os.path.abspath(target)) != os.path.abspath(dest):
                continue                                   # never write outside dest
            with self.zip.open(m) as src, open(target, "wb") as f:
                shutil.copyfileobj(src, f)
        return dest


def find_scans(root):
    """Every .clonescan file and unpacked scan folder directly inside root."""
    out = []
    for n in sorted(os.listdir(root)):
        p = os.path.join(root, n)
        if n.endswith(EXT) or os.path.exists(os.path.join(p, "positions.csv")):
            out.append(p)
    return out


def main(argv):
    if len(argv) < 2 or argv[0] not in ("info", "extract", "pack"):
        sys.exit(__doc__.split("Command line:")[1])
    cmd, path = argv[0], argv[1]
    if cmd == "pack":
        pack(path)
        return
    s = Scan(path)
    if cmd == "info":
        m = s.manifest or {"name": s.name, "photos": len(s.photos())}
        for k, v in m.items():
            print(f"{k:13} {v}")
    else:
        dest = argv[2] if len(argv) > 2 else os.path.join(os.path.dirname(s.path), s.name)
        print("Extracted to", s.extract(dest))


if __name__ == "__main__":
    main(sys.argv[1:])
