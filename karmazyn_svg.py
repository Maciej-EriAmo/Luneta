"""
karmazyn_svg.py — Front SVG -> łamane dla termicznej geometrii Lunety
=====================================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Zamienia SVG na listę łamanych [(x,y), ...], które potem rysuje VectorProvider
z termicznym LOD (ThermalVector). To NIE jest rasteryzator (brak wypełnień,
gradientów, filtrów) — wyciąga GEOMETRIĘ konturów, spójnie z paradygmatem:
kształt jako atom, szczegółowość sterowana temperaturą.

Obsługa:
  • <path d="...">  — M/m L/l H/h V/v C/c S/s Q/q T/t A/a Z/z
    (béziery sześcienne/kwadratowe i łuki eliptyczne spłaszczane do łamanych),
  • figury: <line> <polyline> <polygon> <rect> <circle> <ellipse>,
  • viewBox / width / height (przestrzeń współrzędnych do dopasowania).

Wynik: {"polylines": [[(x,y),...], ...], "viewbox": (x,y,w,h)|None}.
Degradacja: przy błędzie parsowania zwraca {"polylines": [], ...}.
"""

import re
import math
import hashlib
import xml.etree.ElementTree as ET

_BEZ_STEPS = 18          # podział béziera na segmenty (LOD termiczny dalej rzedzi)
_ARC_STEPS = 24          # podział łuku eliptycznego

_TOK = re.compile(r'([MmLlHhVvCcSsQqTtAaZz])|(-?\d*\.?\d+(?:[eE][-+]?\d+)?)')


def svg_aid(url: str) -> str:
    """Identyfikator atomu-wektora wyprowadzony z URL (jak gif_aid dla GIF-ów)."""
    return "svg:" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]


# ─── Spłaszczanie krzywych ────────────────────────────────────────────────────

def _cubic(p0, p1, p2, p3, n=_BEZ_STEPS):
    out = []
    for i in range(1, n + 1):
        t = i / n; mt = 1 - t
        a, b, c, d = mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                    a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def _quad(p0, p1, p2, n=_BEZ_STEPS):
    out = []
    for i in range(1, n + 1):
        t = i / n; mt = 1 - t
        a, b, c = mt * mt, 2 * mt * t, t * t
        out.append((a * p0[0] + b * p1[0] + c * p2[0],
                    a * p0[1] + b * p1[1] + c * p2[1]))
    return out


def _arc(p0, rx, ry, phi_deg, large, sweep, p1, n=_ARC_STEPS):
    """Łuk eliptyczny SVG (endpoint -> center param), spłaszczony do łamanej."""
    x0, y0 = p0; x1, y1 = p1
    if rx == 0 or ry == 0 or (x0 == x1 and y0 == y1):
        return [p1]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg)
    cosp, sinp = math.cos(phi), math.sin(phi)
    dx, dy = (x0 - x1) / 2.0, (y0 - y1) / 2.0
    x1p = cosp * dx + sinp * dy
    y1p = -sinp * dx + cosp * dy
    # korekta promieni
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        s = math.sqrt(lam); rx *= s; ry *= s
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp = co * rx * y1p / ry
    cyp = -co * ry * x1p / rx
    cx = cosp * cxp - sinp * cyp + (x0 + x1) / 2.0
    cy = sinp * cxp + cosp * cyp + (y0 + y1) / 2.0

    def ang(ux, uy, vx, vy):
        d = math.hypot(ux, uy) * math.hypot(vx, vy)
        if d == 0:
            return 0.0
        c = max(-1.0, min(1.0, (ux * vx + uy * vy) / d))
        a = math.acos(c)
        return -a if (ux * vy - uy * vx) < 0 else a

    th1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dth > 0:
        dth -= 2 * math.pi
    elif sweep and dth < 0:
        dth += 2 * math.pi

    out = []
    for i in range(1, n + 1):
        th = th1 + dth * (i / n)
        x = cosp * rx * math.cos(th) - sinp * ry * math.sin(th) + cx
        y = sinp * rx * math.cos(th) + cosp * ry * math.sin(th) + cy
        out.append((x, y))
    return out


# ─── Parser ścieżki ───────────────────────────────────────────────────────────

def parse_path(d: str):
    """Lista łamanych (podścieżek) z atrybutu d."""
    toks = []
    for m in _TOK.finditer(d or ""):
        toks.append(("cmd", m.group(1)) if m.group(1) else ("num", float(m.group(2))))
    i, nT = 0, len(toks)

    def num():
        nonlocal i
        v = toks[i][1]; i += 1; return v

    polylines = []
    cur = []
    cx = cy = 0.0
    sx = sy = 0.0
    pcx = pcy = None        # poprzedni punkt kontrolny (S/T)
    cmd = None
    while i < nT:
        if toks[i][0] == "cmd":
            cmd = toks[i][1]; i += 1
            if cmd in "Zz":
                if cur:
                    cur.append((sx, sy)); polylines.append(cur); cur = []
                cx, cy = sx, sy
                pcx = pcy = None
                continue
        rel = cmd.islower()
        C = cmd.upper()
        if C == "M":
            x, y = num(), num()
            if rel: x, y = cx + x, cy + y
            if cur: polylines.append(cur)
            cur = [(x, y)]
            cx, cy = x, y; sx, sy = x, y
            cmd = "l" if rel else "L"     # kolejne pary to lineto
            pcx = pcy = None
        elif C == "L":
            x, y = num(), num()
            if rel: x, y = cx + x, cy + y
            cur.append((x, y)); cx, cy = x, y; pcx = pcy = None
        elif C == "H":
            x = num()
            if rel: x = cx + x
            cur.append((x, cy)); cx = x; pcx = pcy = None
        elif C == "V":
            y = num()
            if rel: y = cy + y
            cur.append((cx, y)); cy = y; pcx = pcy = None
        elif C == "C":
            x1, y1, x2, y2, x, y = num(), num(), num(), num(), num(), num()
            if rel: x1, y1, x2, y2, x, y = cx+x1, cy+y1, cx+x2, cy+y2, cx+x, cy+y
            cur += _cubic((cx, cy), (x1, y1), (x2, y2), (x, y))
            pcx, pcy = x2, y2; cx, cy = x, y
        elif C == "S":
            x2, y2, x, y = num(), num(), num(), num()
            if rel: x2, y2, x, y = cx+x2, cy+y2, cx+x, cy+y
            x1, y1 = (2*cx - pcx, 2*cy - pcy) if pcx is not None else (cx, cy)
            cur += _cubic((cx, cy), (x1, y1), (x2, y2), (x, y))
            pcx, pcy = x2, y2; cx, cy = x, y
        elif C == "Q":
            x1, y1, x, y = num(), num(), num(), num()
            if rel: x1, y1, x, y = cx+x1, cy+y1, cx+x, cy+y
            cur += _quad((cx, cy), (x1, y1), (x, y))
            pcx, pcy = x1, y1; cx, cy = x, y
        elif C == "T":
            x, y = num(), num()
            if rel: x, y = cx+x, cy+y
            x1, y1 = (2*cx - pcx, 2*cy - pcy) if pcx is not None else (cx, cy)
            cur += _quad((cx, cy), (x1, y1), (x, y))
            pcx, pcy = x1, y1; cx, cy = x, y
        elif C == "A":
            rx, ry, rot, large, sweep, x, y = (num(), num(), num(),
                                               num(), num(), num(), num())
            if rel: x, y = cx + x, cy + y
            cur += _arc((cx, cy), rx, ry, rot, large, sweep, (x, y))
            cx, cy = x, y; pcx = pcy = None
        else:
            i += 1                        # nieznana komenda — pomiń liczbę
    if cur:
        polylines.append(cur)
    return polylines


# ─── Figury podstawowe ────────────────────────────────────────────────────────

def _floats(s):
    return [float(x) for x in re.findall(r'-?\d*\.?\d+(?:[eE][-+]?\d+)?', s or "")]


def _shape_polyline(tag, a):
    t = tag.split("}")[-1].lower()       # bez namespace
    f = lambda k, d=0.0: float(a.get(k, d) or d)
    if t == "line":
        return [[(f("x1"), f("y1")), (f("x2"), f("y2"))]]
    if t in ("polyline", "polygon"):
        nums = _floats(a.get("points", ""))
        pts = list(zip(nums[0::2], nums[1::2]))
        if t == "polygon" and pts:
            pts = pts + [pts[0]]
        return [pts] if pts else []
    if t == "rect":
        x, y, w, h = f("x"), f("y"), f("width"), f("height")
        if w <= 0 or h <= 0:
            return []
        return [[(x, y), (x+w, y), (x+w, y+h), (x, y+h), (x, y)]]
    if t == "circle":
        cx, cy, r = f("cx"), f("cy"), f("r")
        if r <= 0:
            return []
        return [[(cx + r*math.cos(2*math.pi*k/_ARC_STEPS),
                  cy + r*math.sin(2*math.pi*k/_ARC_STEPS)) for k in range(_ARC_STEPS+1)]]
    if t == "ellipse":
        cx, cy, rx, ry = f("cx"), f("cy"), f("rx"), f("ry")
        if rx <= 0 or ry <= 0:
            return []
        return [[(cx + rx*math.cos(2*math.pi*k/_ARC_STEPS),
                  cy + ry*math.sin(2*math.pi*k/_ARC_STEPS)) for k in range(_ARC_STEPS+1)]]
    return []


# ─── Wejście ──────────────────────────────────────────────────────────────────

def _parse_viewbox(root):
    vb = root.get("viewBox")
    if vb:
        n = _floats(vb)
        if len(n) == 4:
            return tuple(n)
    w = root.get("width"); h = root.get("height")
    try:
        if w and h:
            return (0.0, 0.0, float(re.sub(r'[^\d.]', '', w)),
                    float(re.sub(r'[^\d.]', '', h)))
    except Exception:
        pass
    return None


def parse_svg(data):
    """Bajty/str SVG -> {'polylines': [...], 'viewbox': (x,y,w,h)|None}."""
    out = {"polylines": [], "viewbox": None}
    try:
        if isinstance(data, (bytes, bytearray)):
            data = data.decode("utf-8", "replace")
        # usuń deklarację DOCTYPE/encoding, które bywają problemem dla ET
        root = ET.fromstring(data)
    except Exception:
        return out
    out["viewbox"] = _parse_viewbox(root)
    polys = []
    for el in root.iter():
        tag = (el.tag or "").split("}")[-1].lower()
        try:
            if tag == "path":
                polys += parse_path(el.get("d", ""))
            elif tag in ("line", "polyline", "polygon", "rect", "circle", "ellipse"):
                polys += _shape_polyline(el.tag, el.attrib)
        except Exception:
            continue
    out["polylines"] = [p for p in polys if len(p) >= 2]
    return out
