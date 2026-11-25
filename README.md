# Clonetron

Open-source automated scanner and analysis app for clonogenic assays. A
Raspberry Pi and a GRBL stage image crystal-violet stained 6-well plates; a
portable laptop app counts colonies and cells, classifies holo-, mero- and
paraclones, and fits LQ survival curves. Every data point links back to its
image, so any measurement can be checked and corrected by hand.

> **Status: early development.** The scanner scripts work on our machine. The
> browser control app and the analysis app are being built (see Roadmap).

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
  install_service.sh     makes the app start when the Pi boots
  plate_scan.py          focus map, scan, blur check and re-shoot
  live_focus.py          live view, jogging and manual focus checks
  flatfield_calibrate.py camera colour and flat-field calibration (plate out)
  focus_repeat_test.py   checks that autofocus gives the same answer each time
  tile_focus_chart.py    per-photo sharpness chart for a finished scan
analysis/                runs on a laptop
  stitch_scan.py         stitches a scan folder into well and plate images
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

**Scans are saved only to a USB stick** plugged into the Pi, in
`Clonetron/<date>_<time>_<plate name>/`, never to the SD card. The app won't
start without a stick, checks there's enough space first, and stops cleanly if
the stick is pulled out or fills up. Press **Eject** before unplugging it. Each
scan folder also gets copies of the calibration files, so the stick alone is
enough for analysis on a laptop.

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

## Stitching on a laptop

```bash
pip install numpy opencv-python
python analysis/stitch_scan.py "path\to\plate_folder"
```

## Roadmap

1. **Repo and settings file** (done)
2. **Scanner app** (done: live view, jogging, focus tools, setup, USB scanning, start at boot, LCD status): browser control from any laptop on the network: live view,
   jog, focus, scan queue, saving to USB, password protected
3. **Analysis core:** stitching, well and colony detection, cells per colony,
   measurements table
4. **Review screen:** clickable plots linked to the colony images, hand
   corrections with a change log
5. **Clone types and survival:** holo/mero/paraclone classification, PE, SF and
   linear-quadratic fits for all colonies and per clone type

The analysis app will run on Windows from a copied folder, without installing
anything.

## Credits

Built by Geofrey John (Adelaide University).

The design started from the Incubot: Merces G.O.T. et al. (2021) The incubot: a
3D printer-based microscope for long-term live cell imaging within a tissue
culture incubator. *HardwareX* 9:e00189.
[doi:10.1016/j.ohx.2021.e00189](https://doi.org/10.1016/j.ohx.2021.e00189)

## Licence

MIT. See [LICENSE](LICENSE).
