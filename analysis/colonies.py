"""
Find colonies in a stitched well picture.

Works on stained colonies (crystal violet or similar) on a white background, i.e.
a stitched well after flat-field correction. Steps:

1. darkness = how much darker than the local background each pixel is
   (green channel, which crystal violet absorbs most; background estimated with
   a wide blur so uneven light doesn't matter)
2. pixels darker than  sensitivity x the background noise  count as "stained"
3. stained pixels close together are joined (cells of one colony touch or nearly
   touch), holes filled
4. each joined blob bigger than the minimum colony size is a colony
5. only blobs inside the well are kept (the rim is left out)

All sizes are in mm, using the scale the stitcher saved next to the picture.
The result is a list of colonies with centre, area, diameter, how dark they are
and an outline to draw.
"""
import math

import cv2
import numpy as np

DEFAULTS = {
    "sensitivity": 4.0,        # x background noise; lower finds fainter colonies
    "min_area_mm2": 0.05,      # smallest colony (about 250 µm across, ~50+ cells)
    "join_um": 40.0,           # cells closer than this are joined into one colony
    "edge_mm": 1.2,            # ignore this close to the rim
}


def _disk(r):
    r = max(1, int(round(r)))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


def darkness(img):
    """How much darker than the local background each pixel is (float32, >= 0)."""
    g = img[..., 1].astype(np.float32) if img.ndim == 3 else img.astype(np.float32)
    h, w = g.shape
    k = 600.0 / max(h, w)                       # background from a small copy: fast
    small = cv2.resize(g, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    # a high percentile filter would be ideal; dilate (local max) + blur is close and fast
    bg = cv2.dilate(small, _disk(9))
    bg = cv2.GaussianBlur(bg, (0, 0), 12)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.maximum(bg - g, 0)


def well_mask(shape, centre, radius_px):
    m = np.zeros(shape[:2], np.uint8)
    cv2.circle(m, (int(round(centre[0])), int(round(centre[1]))), int(radius_px), 255, -1)
    return m


def detect(img, px_per_mm, centre=None, well_radius_mm=17.4, params=None):
    """Colonies in a stitched well. img: BGR uint8. Returns (colonies, stats)."""
    p = dict(DEFAULTS, **(params or {}))
    h, w = img.shape[:2]
    if centre is None:
        centre = (w / 2, h / 2)
    d = darkness(img)
    inside = well_mask(img.shape, centre, (well_radius_mm - p["edge_mm"]) * px_per_mm)
    inside &= (img[..., 1] > 8).astype(np.uint8) * 255 if img.ndim == 3 else 255  # skip unscanned black

    vals = d[inside > 0]
    if vals.size == 0:
        return [], {"noise": 0, "threshold": 0}
    med = float(np.median(vals))
    noise = 1.4826 * float(np.median(np.abs(vals - med))) + 0.5
    thr = med + p["sensitivity"] * noise
    stained = ((d > thr) & (inside > 0)).astype(np.uint8) * 255

    join_px = p["join_um"] / 1000.0 * px_per_mm
    stained = cv2.morphologyEx(stained, cv2.MORPH_OPEN, _disk(1))          # single-pixel noise
    stained = cv2.morphologyEx(stained, cv2.MORPH_CLOSE, _disk(join_px / 2))

    # fill holes (a colony's paler centre stays part of it)
    cnts, hier = cv2.findContours(stained, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(stained)
    for i, c in enumerate(cnts):
        if hier[0][i][3] < 0:
            cv2.drawContours(filled, cnts, i, 255, -1)

    min_px = p["min_area_mm2"] * px_per_mm ** 2
    cnts, _ = cv2.findContours(filled, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    out = []
    for c in cnts:
        a = cv2.contourArea(c)
        if a < min_px:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        m = np.zeros((bh, bw), np.uint8)
        cv2.drawContours(m, [c - [x, y]], -1, 255, -1)
        dd = d[y:y + bh, x:x + bw][m > 0]
        mom = cv2.moments(c)
        cx, cy = mom["m10"] / mom["m00"], mom["m01"] / mom["m00"]
        per = cv2.arcLength(c, True)
        eps = max(1.0, 0.004 * per)
        poly = cv2.approxPolyDP(c, eps, True).reshape(-1, 2)
        area_mm2 = a / px_per_mm ** 2
        out.append({
            "x": round(cx, 1), "y": round(cy, 1),
            "area_mm2": round(area_mm2, 4),
            "diameter_mm": round(2 * math.sqrt(area_mm2 / math.pi), 3),
            "darkness": round(float(dd.mean()), 1),
            "circularity": round(4 * math.pi * a / max(per, 1) ** 2, 3),
            "poly": poly.tolist(),
        })
    out.sort(key=lambda c: (c["y"], c["x"]))
    for i, c in enumerate(out, 1):
        c["id"] = i
        c["source"] = "auto"
    return out, {"noise": round(noise, 2), "threshold": round(thr, 2),
                 "px_per_mm": px_per_mm, "params": p}
