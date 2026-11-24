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

## Stitching on a laptop

```bash
pip install numpy opencv-python
python analysis/stitch_scan.py "path\to\plate_folder"
```

## Roadmap

1. **Repo and settings file** (done)
2. **Scanner app:** browser control from any laptop on the network: live view,
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
