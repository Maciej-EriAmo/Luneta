"""
karmazyn_css.py — Minimalny czytnik reguł CSS dla Lunety (sprite'y / tła)
=========================================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

NIE jest to silnik CSS. Wyłuskuje dokładnie jeden, bardzo częsty wzorzec:
element z `background-image: url(...)`, `background-position` i `width/height`
— czyli wycinek arkusza sprite'ów (atlasu). To zapala logo/ikony/strzałki,
które w sieci żyją w tłach CSS (np. nav_logo Google), a których dotąd nie
renderowaliśmy.

Obsługa selektorów: proste `tag`, `.class`, `#id` i ich łączenia (`a.foo#bar`),
listy przez przecinek; kombinatory (spacja/>/+/~) redukujemy do ostatniego
prostego selektora, pseudo-klasy ignorujemy. Specyficzność: #id > .class > tag,
przy remisie wygrywa później zadeklarowana reguła.

Wynik wyłuskania: {"src": <url absolutny>, "crop": (sx, sy, sw, sh)} albo None.
"""

import re
import hashlib
import urllib.parse

_URL = re.compile(r'url\(\s*[\'"]?([^\'")]+)[\'"]?\s*\)', re.I)
_COMMENT = re.compile(r'/\*.*?\*/', re.S)

# At-reguły z blokiem, których WNĘTRZE to normalne reguły (wchodzimy do środka).
_NESTED_AT = ("@media", "@supports", "@document", "@-moz-document")


def parse_declarations(text: str) -> dict:
    out = {}
    for part in (text or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _extract_rules(css: str):
    """Świadome klamer wyłuskanie reguł. Wchodzi do @media/@supports (ich reguły
    są aktywne — warunek ignorujemy), pomija @keyframes/@font-face/@page oraz
    @import; (bez bloku). Nie rozjeżdża się na zagnieżdżonych klamrach."""
    rules = []
    i, n, start = 0, len(css), 0
    while i < n:
        c = css[i]
        if c == "{":
            prelude = css[start:i].strip()
            depth, j = 1, i + 1
            while j < n and depth:
                if css[j] == "{":
                    depth += 1
                elif css[j] == "}":
                    depth -= 1
                j += 1
            block = css[i + 1:j - 1]
            if prelude.startswith("@"):
                at = prelude.split(None, 1)[0].lower()
                if at in _NESTED_AT:
                    rules.extend(_extract_rules(block))   # reguły w środku aktywne
                # @keyframes/@font-face/@page/... -> pomijamy cały blok
            else:
                sels = [s.strip() for s in prelude.split(",") if s.strip()]
                decls = parse_declarations(block)
                if sels and decls:
                    rules.append((sels, decls))
            i, start = j, j
        elif c == ";":
            start = i + 1                                  # @import ...; i luźne ;
            i += 1
        else:
            i += 1
    return rules


def parse_stylesheet(css: str):
    """Tekst CSS -> [(selektory:list, deklaracje:dict)]."""
    return _extract_rules(_COMMENT.sub("", css or ""))



def _last_simple(sel: str) -> str:
    """Ostatni prosty selektor (bez kombinatorów) i bez pseudo."""
    sel = re.split(r'[\s>+~]+', sel.strip())[-1]
    return sel.split(":")[0]


def _selector_matches(sel: str, tag: str, idv: str, classes) -> bool:
    s = _last_simple(sel)
    if not s or s == "*":
        return s == "*"
    m = re.match(r'^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)$', s)
    if not m:
        return False
    t = m.group(1)
    parts = re.findall(r'[.#][\w-]+', m.group(2) or "")
    if t and t.lower() != (tag or "").lower():
        return False
    if not t and not parts:
        return False
    for p in parts:
        if p[0] == "#" and p[1:] != idv:
            return False
        if p[0] == "." and p[1:] not in classes:
            return False
    return True


def _specificity(sel: str):
    s = _last_simple(sel)
    return (s.count("#"), s.count("."), 1 if re.match(r'^[a-zA-Z]', s) else 0)


def resolve(rules, tag: str, idv: str, classes) -> dict:
    """Scalone deklaracje pasujących reguł wg specyficzności (później = silniej)."""
    matched = []
    for sels, decls in rules:
        best = None
        for s in sels:
            if _selector_matches(s, tag, idv, classes):
                sp = _specificity(s)
                best = sp if best is None or sp > best else best
        if best is not None:
            matched.append((best, decls))
    matched.sort(key=lambda x: x[0])
    out = {}
    for _, decls in matched:
        out.update(decls)
    return out


def _px(v) -> int:
    if not v:
        return 0
    m = re.search(r'(-?\d*\.?\d+)\s*px', v) or re.search(r'(-?\d*\.?\d+)', v)
    return int(round(float(m.group(1)))) if m else 0


_POS_KW = {"left": 0, "top": 0, "center": None, "right": None, "bottom": None}


def _bg_pos(v):
    """(bx, by) w px. Słowa left/top -> 0; center/right/bottom i % -> None
    (rozwiązywane przy rysowaniu, gdy znamy rozmiar atlasu i pudełka). Sprite'y
    prawie zawsze podają ujemne px, więc to jest główna ścieżka."""
    toks = [t for t in re.split(r'\s+', (v or "").strip()) if t]
    toks = [t for t in toks if t.lower() not in
            ("no-repeat", "repeat", "repeat-x", "repeat-y", "scroll", "fixed", "/")]

    def one(t):
        tl = t.lower()
        if tl in _POS_KW:
            return _POS_KW[tl]
        if tl.endswith("%"):
            return None
        m = re.match(r'-?\d*\.?\d+', t)
        return int(round(float(m.group()))) if m else 0

    bx = one(toks[0]) if len(toks) >= 1 else 0
    by = one(toks[1]) if len(toks) >= 2 else 0
    return bx, by


def _bg_size(v):
    """(sw, sh) w px albo None (contain/cover/auto/% -> None = bez skalowania)."""
    if not v:
        return None
    vl = v.strip().lower()
    if vl in ("cover", "contain", "auto") or "%" in vl:
        return None
    nums = re.findall(r'-?\d*\.?\d+', vl)
    if len(nums) >= 2:
        return (int(round(float(nums[0]))), int(round(float(nums[1]))))
    if len(nums) == 1:
        s = int(round(float(nums[0])))
        return (s, s)
    return None


def _bg_layer(decls: dict):
    """Scala background-* i skrót `background` -> (url, pozycja_str, rozmiar_str)."""
    shorthand = decls.get("background", "")
    img = decls.get("background-image") or shorthand
    mu = _URL.search(img)
    url = mu.group(1).strip() if mu else None

    pos = decls.get("background-position")
    size = decls.get("background-size")
    if (pos is None or size is None) and shorthand:
        s = _URL.sub("", shorthand)             # usuń url(...) ze skrótu
        # 'pozycja / rozmiar'
        if "/" in s:
            p, _, z = s.partition("/")
        else:
            p, z = s, ""
        if pos is None:
            pos = p
        if size is None and z.strip():
            size = z
    return url, pos, size


def sprite_from_decls(decls: dict, base_url: str = ""):
    """Wyłuskaj sprite z deklaracji (CSS lub inline). Wymaga background-image(url)
    + width + height. Zwraca {src, crop:(x,y,w,h) w px CSS, size:(sw,sh)|None}.
    background-position (ujemny) daje offset w atlasie; background-size niesiony do
    providera, bo skalę atlasu (retina!) rozwiązujemy dopiero znając jego piksele."""
    url, pos, size = _bg_layer(decls)
    if not url:
        return None
    w, h = _px(decls.get("width")), _px(decls.get("height"))
    if w <= 0 or h <= 0:
        return None
    src = urllib.parse.urljoin(base_url, url)
    bx, by = _bg_pos(pos if pos is not None else "0 0")
    bx = bx or 0
    by = by or 0
    return {"src": src, "crop": (-bx, -by, w, h), "size": _bg_size(size)}


def sprite_aid(src: str, crop) -> str:
    return "spr:" + hashlib.sha1(f"{src}|{crop}".encode("utf-8")).hexdigest()[:16]


# ── Kolory (background-color) ─────────────────────────────────────────────────
_NAMED = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0),
    "green": (0, 128, 0), "lime": (0, 255, 0), "blue": (0, 0, 255),
    "yellow": (255, 255, 0), "cyan": (0, 255, 255), "aqua": (0, 255, 255),
    "magenta": (255, 0, 255), "fuchsia": (255, 0, 255), "silver": (192, 192, 192),
    "gray": (128, 128, 128), "grey": (128, 128, 128), "maroon": (128, 0, 0),
    "olive": (128, 128, 0), "navy": (0, 0, 128), "teal": (0, 128, 128),
    "purple": (128, 0, 128), "orange": (255, 165, 0), "pink": (255, 192, 203),
    "brown": (165, 42, 42), "gold": (255, 215, 0), "beige": (245, 245, 220),
    "coral": (255, 127, 80), "crimson": (220, 20, 60), "indigo": (75, 0, 130),
    "khaki": (240, 230, 140), "lavender": (230, 230, 250), "salmon": (250, 128, 114),
    "tomato": (255, 99, 71), "turquoise": (64, 224, 208), "violet": (238, 130, 238),
    "tan": (210, 180, 140), "skyblue": (135, 206, 235), "lightgray": (211, 211, 211),
    "lightgrey": (211, 211, 211), "darkgray": (169, 169, 169), "darkgrey": (169, 169, 169),
    "whitesmoke": (245, 245, 245), "gainsboro": (220, 220, 220),
    "dodgerblue": (30, 144, 255), "royalblue": (65, 105, 225),
    "steelblue": (70, 130, 180), "slategray": (112, 128, 144),
    "darkred": (139, 0, 0), "darkblue": (0, 0, 139), "darkgreen": (0, 100, 0),
    "midnightblue": (25, 25, 112), "rebeccapurple": (102, 51, 153),
}


def _hsl_rgb(h, s, l):
    h = (h % 360) / 360.0
    s = max(0.0, min(1.0, s)); l = max(0.0, min(1.0, l))

    def hue(p, q, t):
        if t < 0: t += 1
        if t > 1: t -= 1
        if t < 1 / 6: return p + (q - p) * 6 * t
        if t < 1 / 2: return q
        if t < 2 / 3: return p + (q - p) * (2 / 3 - t) * 6
        return p
    if s == 0:
        r = g = b = l
    else:
        q = l * (1 + s) if l < 0.5 else l + s - l * s
        p = 2 * l - q
        r, g, b = hue(p, q, h + 1 / 3), hue(p, q, h), hue(p, q, h - 1 / 3)
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def parse_color(v):
    """CSS -> (r,g,b) albo None (transparent/none/currentColor/nieznane)."""
    if not v:
        return None
    v = v.strip().lower()
    if v in ("transparent", "none", "currentcolor", "inherit", "initial"):
        return None
    if v in _NAMED:
        return _NAMED[v]
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h[:3])
        if len(h) >= 6:
            try:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
            except ValueError:
                return None
        return None
    m = re.match(r'rgba?\(([^)]+)\)', v)
    if m:
        parts = re.findall(r'-?\d*\.?\d+%?', m.group(1))
        if len(parts) >= 3:
            def comp(p):
                if p.endswith("%"):
                    return int(round(float(p[:-1]) * 255 / 100))
                return int(round(float(p)))
            return (max(0, min(255, comp(parts[0]))),
                    max(0, min(255, comp(parts[1]))),
                    max(0, min(255, comp(parts[2]))))
    m = re.match(r'hsla?\(([^)]+)\)', v)
    if m:
        parts = re.findall(r'-?\d*\.?\d+', m.group(1))
        if len(parts) >= 3:
            return _hsl_rgb(float(parts[0]), float(parts[1]) / 100, float(parts[2]) / 100)
    return None


def bg_color_from_decls(decls: dict):
    """Kolor tła z background-color albo z pierwszego koloru w skrócie background."""
    c = parse_color(decls.get("background-color"))
    if c is not None:
        return c
    shorthand = decls.get("background", "")
    if shorthand:
        s = _URL.sub("", shorthand)
        # spróbuj rgb()/hsl()/#hex/nazwa
        m = re.search(r'(rgba?\([^)]*\)|hsla?\([^)]*\)|#[0-9a-fA-F]{3,8})', s)
        if m:
            return parse_color(m.group(1))
        for tok in re.split(r'\s+', s.strip()):
            if tok in _NAMED:
                return _NAMED[tok]
    return None


def element_classes(attrs: dict):
    return set((attrs.get("class", "") or "").split())