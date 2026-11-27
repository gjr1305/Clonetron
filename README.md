# Clonetron

Open-source automated scanner and analysis app for clonogenic assays. A
Raspberry Pi and a GRBL stage image crystal-violet stained 6-well plates; a
portable laptop app counts colonies and cells, classifies holo-, mero- and
paraclones, and fits LQ survival curves. Every data point links back to its
image, so any measurement can be checked and corrected by hand.

> **Status: early development.** The scanner scripts work on our machine. The
> browser control app works; the analysis app has its first version (see Roadmap).

## The machine

A MakerBot Replicator 2X converted into an inverted brightfield scanner:

| Part | What we use |
|---|---|
| Computer | Raspberry Pi 3B+ |
| Camera | IMX219 sensor (Pi Camera v2 type), stock lens removed |
| Objective | 4× infinity-corrected (RMS thread) |
| Motion | Arduino Uno + CNC shield, GRBL 1.1h, 3 × DRV8825 |
| Light | Edge-lit A4 LED light pad under the plate |
| Display | 20×4 HD44780 LCD (shows the Pi's IP address at boot) |

One photo covers about 3.2 × 2.4 mm at roughly 1 µm per pixel; a 35 mm well
takes about 213 photos.

## What's in the repo

```
scanner/                 runs on the Pi
  clonetron_server.py    the scanner app: control everything from a browser
  web/                   the app's pages
  hw.py, config.py       stage/camera access (and the simulator), settings
  usb.py, lcd.py         USB stick handling, front-panel LCD
  buttons.py             the five front-panel buttons
  install_service.sh     makes the app start when the Pi boots
  plate_scan.py          focus map, scan, blur check and re-shoot
  live_focus.py          live view, jogging and manual focus checks
  flatfield_calibrate.py camera colour and flat-field calibration (plate out)
  focus_repeat_test.py   checks that autofocus gives the same answer each time
  tile_focus_chart.py    per-photo sharpness chart for a finished scan
analysis/                runs on a laptop
  Start Clonetron Analysis.bat   double-click to start the analysis app (Windows)
  clonetron_analysis.py  the analysis app: open scans, stitch, count and check colonies
  colonies.py            finds colonies in a stitched well
  clonescan.py           the .clonescan file format (shared with the Pi)
  stitch_scan.py         stitches a .clonescan file into well and plate images
  web/                   the app's pages
config/
  clonetron.example.json machine settings (well positions, focus height, ...)
portable-test/           checks that a locked-down laptop can run the app
docs/                    wiring diagrams
```

## Getting started on the Pi

```bash
git clone https://github.com/gjr1305/Clonetron.git ~/clonetron
cp ~/clonetron/config/clonetron.example.json ~/clonetron.json   # then edit it
cd ~/clonetron/scanner
python3 flatfield_calibrate.py          # once, with the plate OUT
python3 plate_scan.py --make-map        # once per plate holder setup
python3 plate_scan.py                   # every scan
```

To update later: `cd ~/clonetron && git pull`.

Needs `python3-picamera2`, `python3-opencv`, `python3-numpy` and `python3-serial`
(all from `apt`). Calibration files and scans go to `~/scans`.

## The scanner app

On the Pi, choose a password once, then start the app:

```bash
cd ~/clonetron/scanner
python3 clonetron_server.py --set-password
python3 clonetron_server.py
```

Open `http://<IP on the LCD>:8000` in a browser on the same network and log in.
So far it has live view, X/Y/Z jogging with step sizes, Home, Stop, a live
sharpness meter, Go to well and Autofocus. The Setup page runs the flat-field
calibration and makes the focus map, with live progress and a Cancel button.
The Scan page runs a scan: name the plate, pick wells and the focus mode, and
press Start. It shows a progress bar, time left and each photo as it is saved,
and has a Stop button.

**Front-panel buttons.** Press CENTRE on the scanner to open the LCD menu
(UP/DOWN to move, CENTRE to select, LEFT to go back): Home stage, Go to well,
Scan whole plate, Eject USB, Show address, Shut down Pi. During a scan, CENTRE
asks whether to stop it. The Live page has the same five buttons. Which pin
each button is on is learned once and saved in `~/scans/buttons.json`.

**Scans are saved only to a USB stick** plugged into the Pi, never to the SD
card, as one file per plate: `Clonetron/<date>_<time>_<plate name>.clonescan`.
The file holds the raw photos, where each was taken, the focus data and the
calibration (including the flat-field image that removes the camera's pink
corners), so the stick alone is enough for analysis on a laptop. During the
scan the photos go into a folder; when the scan ends (or is stopped) they are
packed into the file, the file is checked, and only then is the folder removed.
For that moment the stick needs room for both, so the app asks for twice the
scan's size free. It won't start without a stick and stops cleanly if the stick
is pulled out or fills up. Press **Eject** before unplugging it.

A `.clonescan` file is a plain zip inside, so the photos can always be got out
without the app: `python analysis/clonescan.py extract FILE.clonescan`
(or `info` to see what's in it).

**Start it at boot.** Once it works, run this once on the Pi:

```bash
cd ~/clonetron/scanner && bash install_service.sh
```

It asks before changing anything. From then on the app starts by itself, the
LCD shows the address to open, and during a scan it shows the well, photo count
and time left. After a `git pull`, run `sudo systemctl restart clonetron`.
`bash install_service.sh --remove` undoes it.

**Try it without the scanner.** On any laptop with Python:

```bash
py -m pip install --user numpy opencv-python
py scanner\clonetron_server.py --sim
```

then open <http://127.0.0.1:8000>. The simulator draws a fake stained plate
that is only sharp near Z = 19.6 mm, so the focus meter can be tried too.

## Analysis on a laptop

**Windows:** copy the `analysis` folder anywhere and double-click
**Start Clonetron Analysis.bat**. The first time, it installs numpy and OpenCV
for your Windows user (no admin rights; needs the internet, about a minute).
A black window opens (keep it open) and the app opens in your browser.
On a Mac or Linux: `pip install numpy opencv-python` then
`python analysis/clonetron_analysis.py`.

1. **Open a scan.** USB sticks with a `Clonetron` folder are listed at the top;
   any other folder can be typed in or browsed to.
2. **Stitch and count.** Removes the pink corners (flat-field), lines the
   photos up into one picture per well, then finds the colonies.
3. **Check each well.** Scroll to zoom, drag to move. Click a colony and press
   Delete to remove it (it stays visible, dashed red, and isn't counted);
   switch to *Add colony* and drag from a colony's centre to its edge to add one.
   Ctrl+Z undoes. Tick *Checked by eye* when a well is done.
4. **Export CSV.** `counts.csv` (one row per well) and `colonies.csv` (one row per
   colony, with size in mm) go into the scan's results folder.

Everything is saved in a `<scan name> results` folder next to the scan file,
and edits are saved as you make them. The `.clonescan` file is never changed.
Counting settings (sensitivity, smallest colony size) are under
*Counting settings*; a recount keeps colonies you added by hand.

**How colonies are found** (`colonies.py`): each pixel's darkness against the
local background, a threshold set from the background noise, nearby stained
cells joined into one colony, holes filled, then anything smaller than the
minimum colony size (default 0.05 mm², about 50 cells) or outside the well is
dropped. Two colonies that touch are counted as one; split them by removing
the joined one and adding both by hand.

Stitching alone, without the app:

```bash
python analysis/stitch_scan.py "E:\Clonetron\2026-11-26_101500_GL261_4Gy.clonescan"
```

## Roadmap

1. **Repo and settings file** (done)
2. **Scanner app** (done: live view, jogging, focus tools, setup, USB scanning, start at boot, LCD status): browser control from any laptop on the network: live view,
   jog, focus, scan queue, saving to USB, password protected
3. **Analysis core** (first version done: stitching, colony detection, hand
   corrections, per-well and per-colony CSV). Still to come: splitting touching
   colonies, cells per colony
4. **Review screen:** clickable plots linked to the colony images, hand
   corrections with a change log
5. **Clone types and survival:** holo/mero/paraclone classification, PE, SF and
   linear-quadratic fits for all colonies and per clone type

The analysis app runs on Windows from a copied folder; the only thing it
installs is numpy and OpenCV, for the current user, on first start.

## Credits

Built by Geofrey John (Adelaide University).

The design started from the Incubot: Merces G.O.T. et al. (2021) The incubot: a
3D printer-based microscope for long-term live cell imaging within a tissue
culture incubator. *HardwareX* 9:e00189.
[doi:10.1016/j.ohx.2021.e00189](https://doi.org/10.1016/j.ohx.2021.e00189)

## Licence

MIT. See [LICENSE](LICENSE).
