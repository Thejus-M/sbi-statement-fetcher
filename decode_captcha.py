"""
Captcha decoder v3 — fully offline (OpenCV + Tesseract).
Pipeline: median blur (dots) -> shape filter (lines) -> upscale -> OCR,
with several cleaning levels competing, ranked by OCR confidence.
A length sanity-check means noise reads are rejected, never selected.

Usage: uv run python decode_captcha.py [image]   (default: output/captcha_raw.png)
"""

import sys
import cv2
import numpy as np
import pytesseract

# Windows-only, uncomment if tesseract isn't on PATH:
# pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

DEBUG        = False
CHARSET      = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
MAX_LEN      = 8      # real answers are short; longer = OCR reading noise
EXPECTED_LEN = None   # set to e.g. 5 if all your captchas are 5 characters

H_FRAC = 0.20   # a letter is at least this tall (fraction of image height)
FILL   = 0.20   # a letter is solid; lines and dot-clusters are sparse


# ------------------------------------------------------------------ helpers

def load_ink(path):
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"could not read image: {path!r}")
    if img.mean() < 127:                          # dark background -> flip
        img = 255 - img
    _, bw = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return bw                                     # ink = 255


def components(bw):
    n, lab, st, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    return lab, st[1:]                           # row 0 is background


def is_glyph(s, H):
    """Letter-shaped: tall enough, solid enough. Dots fail height, lines fail fill."""
    x, y, w, h, a = s
    return h >= H_FRAC * H and a >= FILL * w * h


def keep_glyphs(bw):
    lab, comps = components(bw)
    out = np.zeros_like(bw)
    for i, s in enumerate(comps):
        if is_glyph(s, bw.shape[0]):
            out[lab == i + 1] = 255
    return out


def scrub_lines(bw):
    """Erase long straight lines — they cross letters and merge into one blob."""
    W = bw.shape[1]
    out = bw.copy()
    segs = cv2.HoughLinesP(out, 1, np.pi / 180, threshold=40,
                           minLineLength=max(50, int(0.35 * W)), maxLineGap=3)
    if segs is not None:
        for x1, y1, x2, y2 in segs.reshape(-1, 4):
            cv2.line(out, (int(x1), int(y1)), (int(x2), int(y2)), 0, 2)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    return cv2.morphologyEx(out, cv2.MORPH_CLOSE, k)   # heal the nicks


def opened(bw):
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    return cv2.morphologyEx(bw, cv2.MORPH_OPEN, k)


def ocr_input(bw, f=4):
    """Big, smooth, black-on-white — what Tesseract likes."""
    big = cv2.resize(bw, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
    big = cv2.GaussianBlur(big, (3, 3), 0)
    _, big = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return 255 - big


def ocr(bow, psm):
    """-> (text, mean confidence). bow = black text on white."""
    cfg = f"--oem 3 --psm {psm} -c tessedit_char_whitelist={CHARSET}"
    d = pytesseract.image_to_data(bow, config=cfg,
                                  output_type=pytesseract.Output.DICT)
    words, confs = [], []
    for t, c in zip(d["text"], d["conf"]):
        if t.strip():
            words.append("".join(ch for ch in t.upper() if ch in CHARSET))
            try:
                confs.append(max(float(c), 0.0))
            except (TypeError, ValueError):
                confs.append(0.0)
    return "".join(words), (sum(confs) / len(confs) if confs else 0.0)


def score(text, conf):
    if len(text) > MAX_LEN:
        return -1.0                      # noise read — can NEVER win
    s = conf
    if EXPECTED_LEN:
        s -= 15.0 * abs(len(text) - EXPECTED_LEN)
    return s


# ------------------------------------------------------------- candidate paths

def whole_image_candidates(ink):
    dots_gone = cv2.medianBlur(ink, 3)           # kills isolated dots

    variants = {
        "raw":     dots_gone,                          # blur only (fallback)
        "plain":   keep_glyphs(dots_gone),              # + dot/line filter
        "delined": keep_glyphs(scrub_lines(dots_gone)),# + lines crossing letters
        "opened":  keep_glyphs(opened(dots_gone)),      # + stubborn specks
    }

    results = []
    for name, v in variants.items():
        if DEBUG:
            cv2.imwrite(f"stage3_{name}.png", 255 - v)
        if not cv2.countNonZero(v):
            continue
        bow = ocr_input(v)
        for psm in (7, 8):                       # word/line modes only
            t, conf = ocr(bow, psm)
            if t:
                results.append((score(t, conf), name, psm, t, conf))
    return results


def per_char_candidate(ink):
    """Segment the kept glyphs and OCR them individually (psm 10)."""
    bw = keep_glyphs(cv2.medianBlur(ink, 3))
    lab, comps = components(bw)
    boxes = sorted((s[0], s[1], s[2], s[3], i + 1)
                   for i, s in enumerate(comps) if is_glyph(s, bw.shape[0]))
    chars, confs = [], []
    for x, y, w, h, _ in boxes:
        pad = 6
        crop = bw[max(0, y - pad):y + h + pad, max(0, x - pad):x + w + pad]
        t, conf = ocr(ocr_input(crop, 6), 10)
        if t:
            chars.append(t)
            confs.append(conf)
    if not chars:
        return []
    text, conf = "".join(chars), sum(confs) / len(confs)
    return [(score(text, conf), "by-char", 10, text, conf)]


# ------------------------------------------------------------------- driver

def decode(path):
    ink = load_ink(path)
    cands = whole_image_candidates(ink) + per_char_candidate(ink)

    if DEBUG:
        for s, name, psm, t, conf in sorted(cands, key=lambda c: c[0], reverse=True):
            print(f"  [cand] {name}/psm{psm}: {t!r} conf={conf:.0f} score={s:.0f}")

    valid = [c for c in cands if c[0] >= 0]
    if not valid:
        if not cands:
            print("[decode] no text found at all")
            return ""
        cands.sort(key=lambda c: c[4], reverse=True)
        s, name, psm, t, conf = cands[0]
        print(f"[decode] WARNING: low quality, best effort")
        return t[:MAX_LEN]

    valid.sort(key=lambda c: c[0], reverse=True)
    s, name, psm, t, conf = valid[0]
    print(f"[decode] {t!r}  conf={conf:.0f}  via {name}/psm{psm}")
    return t


if __name__ == "__main__":
    print(decode(sys.argv[1] if len(sys.argv) > 1 else "output/captcha_raw.png"))
