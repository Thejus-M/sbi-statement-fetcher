"""
captha.py — offline captcha solver for 5-char (A-Z 0-9) captchas.
Ensemble: ddddocr + EasyOCR + Tesseract. Quiet by default.

Usage:      uv run captha.py <image-or-folder> [--debug]
Debug:      adds stage dumps (v7_*.png), per-glyph reads, all candidates
Library:    from captha import decode
            code = decode("captcha.png")   # "" if nothing found
"""

import warnings
warnings.filterwarnings("ignore")     # MUST run before torch/easyocr get imported

import contextlib
import os
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import pytesseract

N_CHARS = 5
CHARSET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
ENG_W = {"dddd": 1.2, "easy": 1.0, "tess": 0.9}

USE_DDDD = True      # flip any of these to drop an engine
USE_EASY = True      # (faster startup, fewer deps)
USE_TESS = True

DEBUG = "--debug" in sys.argv


# ------------------------------------------------------------ engine loading

_DDDD, _EASY = None, None
_tried = {"dddd": False, "easy": False}


@contextlib.contextmanager
def _mute():
    """Swallow everything third-party libs print (banners, progress bars)."""
    with open(os.devnull, "w") as devnull, \
         contextlib.redirect_stdout(devnull), \
         contextlib.redirect_stderr(devnull):
        yield


def get_dddd():
    global _DDDD
    if USE_DDDD and not _tried["dddd"]:
        _tried["dddd"] = True
        try:
            import ddddocr
            try:
                with _mute():
                    _DDDD = ddddocr.DdddOcr(show_ad=False)
            except TypeError:                       # older ddddocr: no show_ad
                with _mute():
                    _DDDD = ddddocr.DdddOcr()
        except Exception as e:
            if DEBUG:
                print(f"[init] ddddocr unavailable: {e}")
    return _DDDD


def get_easy():
    global _EASY
    if USE_EASY and not _tried["easy"]:
        _tried["easy"] = True
        try:
            import easyocr
            # First-ever run downloads models (~100 MB) silently;
            # after that they're cached and this is instant.
            with _mute():
                _EASY = easyocr.Reader(["en"], gpu=False, verbose=False)
        except Exception as e:
            if DEBUG:
                print(f"[init] easyocr unavailable: {e}")
    return _EASY


# ----------------------------------------------------------------- readers

def clean_str(s):
    return "".join(ch for ch in s.upper() if ch in CHARSET)


def to_png(bw_ink):
    ok, buf = cv2.imencode(".png", 255 - bw_ink)
    return buf.tobytes()


def dddd_read(bw_ink):
    d = get_dddd()
    if d is None:
        return []
    try:
        t = clean_str(d.classification(to_png(bw_ink)))
    except Exception:
        return []
    return [(t, 65.0)] if t else []


def easy_read(bw_ink):
    e = get_easy()
    if e is None:
        return []
    img = cv2.resize(255 - bw_ink, None, fx=2, fy=2,
                     interpolation=cv2.INTER_CUBIC)
    try:
        with _mute():
            res = e.readtext(img, detail=1, paragraph=False, allowlist=CHARSET)
    except Exception:
        return []
    if not res:
        return []
    res = sorted(res, key=lambda r: min(p[0] for p in r[0]))
    text = clean_str("".join(r[1] for r in res))
    if not text:
        return []
    return [(text, 100.0 * sum(float(r[2]) for r in res) / len(res))]


def easy_char(crop_ink):
    e = get_easy()
    if e is None:
        return "", 0.0
    g = cv2.copyMakeBorder(255 - crop_ink, 24, 24, 24, 24,
                           cv2.BORDER_CONSTANT, value=255)
    h, w = g.shape
    try:
        with _mute():
            r = e.recognize(g, [[0, w, 0, h]], [], allowlist=CHARSET, detail=1)
        for item in r:
            t = clean_str(item[1])
            if t:
                return t[0], 100.0 * float(item[2])
    except Exception:
        pass
    return "", 0.0


def _tess(bow, psm):
    cfg = f"--oem 3 --psm {psm} -c tessedit_char_whitelist={CHARSET}"
    d = pytesseract.image_to_data(bow, config=cfg,
                                   output_type=pytesseract.Output.DICT)
    words, confs = [], []
    for t, c in zip(d["text"], d["conf"]):
        t = clean_str(t)
        if t:
            words.append(t)
            try:
                confs.append(max(float(c), 0.0))
            except (TypeError, ValueError):
                confs.append(0.0)
    return "".join(words), (sum(confs) / len(confs) if confs else 0.0)


def tess_char(crop_ink):
    big = cv2.resize(crop_ink, None, fx=6, fy=6, interpolation=cv2.INTER_CUBIC)
    big = cv2.GaussianBlur(big, (3, 3), 0)
    _, big = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    big = cv2.copyMakeBorder(big, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=0)
    best = ("", 0.0)
    for psm in (10, 8):
        t, c = _tess(255 - big, psm)
        if t and c > best[1]:
            best = (t[0], c)
    return best


# --------------------------------------------------- localization (proven)

def load_ink(path):
    img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"could not read image: {path!r}")
    if img.mean() < 127:
        img = 255 - img
    img = cv2.medianBlur(img, 3)
    _, ink = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    return ink


def remove_lines(ink):
    H, W = ink.shape
    out = ink.copy()
    segs = cv2.HoughLinesP(ink, 1, np.pi / 180, threshold=30,
                            minLineLength=int(0.45 * max(H, W)), maxLineGap=3)
    if segs is not None:
        for x1, y1, x2, y2 in segs.reshape(-1, 4):
            cv2.line(out, (int(x1), int(y1)), (int(x2), int(y2)), 0, 2)
    return out


def components(bw):
    n, lab, st, _ = cv2.connectedComponentsWithStats(bw, connectivity=8)
    return lab, st[1:]


def cluster_x(boxes):
    boxes = sorted(boxes, key=lambda b: b[0])
    merged = []
    for x, y, w, h, a in boxes:
        if merged:
            px, py, pw, ph, pa = merged[-1]
            gap = x - (px + pw)
            overlap = min(px + pw, x + w) - x
            if overlap > 0.30 * min(w, pw) or gap < 0.08 * max(h, ph):
                nx, ny = min(px, x), min(py, y)
                nx2, ny2 = max(px + pw, x + w), max(py + ph, y + h)
                merged[-1] = (nx, ny, nx2 - nx, ny2 - ny, pa + a)
                continue
        merged.append((x, y, w, h, a))
    return merged


def split_wide(boxes):
    out = []
    for x, y, w, h, a in boxes:
        if w > 1.45 * h:
            parts = max(2, int(round(w / (0.75 * h))))
            pw = int(w) // parts
            for i in range(parts):
                ww = pw if i < parts - 1 else int(w) - pw * (parts - 1)
                out.append((int(x) + i * pw, y, ww, h, a / parts))
        else:
            out.append((x, y, w, h, a))
    return out


def find_char_boxes(nolines):
    H, W = nolines.shape
    best_boxes, best_opened = [], None
    for k in (3, 5, 7):
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        opened = cv2.morphologyEx(nolines, cv2.MORPH_OPEN, ker)
        _, st = components(opened)
        cand = [tuple(int(v) for v in s) for s in st
                if s[3] >= 0.30 * H and s[2] <= 0.60 * W
                and max(s[2], s[3]) <= 4.0 * max(1, min(s[2], s[3]))]
        boxes = split_wide(cluster_x(cand))
        if len(boxes) >= N_CHARS:
            if len(boxes) > N_CHARS:
                boxes = sorted(boxes, key=lambda b: b[3], reverse=True)[:N_CHARS]
                boxes.sort(key=lambda b: b[0])
            return boxes, opened
        if len(boxes) > len(best_boxes):
            best_boxes, best_opened = boxes, opened
    return best_boxes, best_opened


def restore_strokes(seeds, nolines):
    ker = np.ones((3, 3), np.uint8)
    cur = seeds
    for _ in range(80):
        grown = cv2.bitwise_and(cv2.dilate(cur, ker), nolines)
        if np.array_equal(grown, cur):
            return cur
        cur = grown
    return cur


def crop_box(src, box, shape):
    x, y, w, h = (int(v) for v in box[:4])
    H, W = shape
    pad = max(3, h // 8)
    return src[max(0, y - pad):min(H, y + h + pad),
               max(0, x - pad):min(W, x + w + pad)]


# ---------------------------------------------------------------- combining

def combine(cands, pos_cands):
    if cands:
        groups = defaultdict(lambda: [0, 0.0])
        for conf, eng, t in cands:
            groups[t][0] += 1
            groups[t][1] += conf * ENG_W.get(eng, 1.0)
        t, (n, s) = max(groups.items(), key=lambda kv: (kv[1][0], kv[1][1]))
        if n >= 2:
            return t, s / n
    votes = defaultdict(lambda: defaultdict(float))
    for pos, conf, eng, ch in pos_cands:
        if pos < N_CHARS:
            votes[pos][ch] += conf * ENG_W.get(eng, 1.0)
    if votes and all(i in votes for i in range(N_CHARS)):
        text = "".join(max(votes[i].items(), key=lambda kv: kv[1])[0]
                       for i in range(N_CHARS))
        score = sum(max(votes[i].values()) for i in range(N_CHARS)) / N_CHARS
        return text, score
    if cands:
        conf, eng, t = max(cands, key=lambda c: c[0])
        return t, conf
    return None


# ------------------------------------------------------------------ driver

def decode(path):
    """Return the captcha code for an image file ('' if nothing found)."""
    ink = load_ink(path)
    nolines = remove_lines(ink)
    boxes, seeds = find_char_boxes(nolines)
    clean = restore_strokes(seeds, nolines) if seeds is not None else nolines

    if DEBUG:
        cv2.imwrite("v7_nolines.png", 255 - nolines)
        cv2.imwrite("v7_clean.png", 255 - clean)

    cands, pos_cands = [], []

    for im in (ink, nolines, clean):                 # whole-image reads
        for t, c in dddd_read(im):
            if len(t) == N_CHARS:
                cands.append((c, "dddd", t))
        for t, c in easy_read(im):
            if len(t) == N_CHARS:
                cands.append((c, "easy", t))

    reads = {"dddd": {}, "easy": {}, "tess": {}}
    for pos, box in enumerate(boxes or []):
        crop = crop_box(clean, box, ink.shape)
        r = dddd_read(crop)
        if r and len(r[0][0]) == 1:
            reads["dddd"][pos] = r[0]
        ch, c = easy_char(crop)
        if ch:
            reads["easy"][pos] = (ch, c)
        ch, c = tess_char(crop)
        if ch:
            reads["tess"][pos] = (ch, c)
        if DEBUG:
            cv2.imwrite(f"v7_glyph{pos}.png", 255 - crop)
            print(f"    glyph{pos}: dddd={reads['dddd'].get(pos, ('', 0))[0]!r} "
                  f"easy={reads['easy'].get(pos, ('', 0))[0]!r} "
                  f"tess={reads['tess'].get(pos, ('', 0))[0]!r}")

    for eng, rd in reads.items():
        if len(rd) == N_CHARS:
            text = "".join(rd[i][0] for i in range(N_CHARS))
            conf = sum(rd[i][1] for i in range(N_CHARS)) / N_CHARS
            cands.append((conf, eng, text))
        for pos, (ch, c) in rd.items():
            pos_cands.append((pos, c, eng, ch))
    for conf, eng, t in cands:
        for i, ch in enumerate(t):
            pos_cands.append((i, conf, eng, ch))

    r = combine(cands, pos_cands)
    if r is None:
        return ""
    text, score = r
    if DEBUG:
        for c, e, t in sorted(cands, key=lambda x: -x[0]):
            print(f"    [cand] {t!r} conf={c:.0f} via {e}")
        print(f"    [chosen] {text!r} score={score:.0f}")
    return text


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--debug"]
    paths = []
    for a in args:
        p = Path(a)
        paths += sorted(str(x) for x in p.glob("*.png")) if p.is_dir() else [str(p)]
    if not paths:
        print("usage: uv run captha.py <image-or-folder> [--debug]")
        sys.exit(1)
    for p in paths:
        try:
            code = decode(p) or "???"
        except Exception as e:
            code = f"ERROR: {e}"
        print(f"{Path(p).name}: {code}")