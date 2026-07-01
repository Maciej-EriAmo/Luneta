"""
luneta.py — Punkt wejścia Windows CLI dla Lunety v2.3
======================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Wersja 2.3: Tryb płótna graficznego z reaktywnym podgrzewaniem
substratu pod kursorem (hover-heat). Najechanie na fragment tekstu
podgrzewa atom powiązany z węzłem DOM — sygnał uwagi mapowany na
ciepło. Ciepło aplikowane jest na WEJŚCIU w węzeł (zmiana stanu)
z throttlingiem, nigdy w pętli klatka-po-klatce.

Wersja 2.2: Automatyczny start w trybie płótna graficznego.
Przeglądarka natychmiast przejmuje ekran, a terminal REPL
pozostaje w tle jako fallback pod klawiszem ESC.
"""

import os
import sys
import re
import urllib.parse

if os.name == 'nt':
    os.system("")

try:
    import pygame
except ImportError:
    pass

try:
    from luneta_runtime import LunetaRuntime
    from karmazyn_browser import LunetaBrowser, NodeType
    from karmazyn_dom import attach_to_browser, cmd_dom
    from karmazyn_display import KarmazynDisplay, DrawCtx, PYGAME_OK
except ImportError as e:
    print(f"Błąd importu środowiska KarmazynOS: {e}")
    sys.exit(1)

try:
    from luneta_async_bridge import install_async_engine_on_browser, cmd_async
    _HAS_ASYNC = True
except ImportError:
    _HAS_ASYNC = False
    def cmd_async(args, bridge):
        return None

try:
    from luneta_image_loader import ImageLoader
    _HAS_IMAGES = True
except ImportError:
    _HAS_IMAGES = False
    ImageLoader = None

try:
    from karmazyn_gif import gif_aid as _gif_aid
except Exception:
    def _gif_aid(url):
        import hashlib
        return "gif:" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]

try:
    from karmazyn_svg import svg_aid as _svg_aid
except Exception:
    def _svg_aid(url):
        import hashlib
        return "svg:" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]

try:
    from karmazyn_css import (sprite_from_decls as _sprite_from_decls,
                              resolve as _css_resolve,
                              parse_declarations as _css_inline,
                              element_classes as _css_classes,
                              sprite_aid as _sprite_aid,
                              bg_color_from_decls as _css_bgcolor)
    _HAS_CSS = True
except Exception:
    _HAS_CSS = False

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
def strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', str(text))


# ─── 0. STYL INLINE (bitmaska na LayoutBox) ─────────────────────────────────
STYLE_NONE   = 0
STYLE_BOLD   = 1
STYLE_ITALIC = 2

# Znaki zerowej szerokości / formatujące — usuwamy je ze słów, bo pygame-ce
# rzuca "Text has zero width" przy renderze tekstu o zerowej szerokości, a same
# tworzyłyby widmowe pudełka. ZWSP, ZWNJ, ZWJ, word-joiner, BOM, miękki dywiz.
_ZERO_WIDTH = "\u200b\u200c\u200d\u2060\ufeff\u00ad"
_ZW_TABLE = {ord(c): None for c in _ZERO_WIDTH}


def _brighten(color, amount: int):
    """Rozjaśnia kolor o stałą wartość z nasyceniem na 255."""
    return (
        min(255, color[0] + amount),
        min(255, color[1] + amount),
        min(255, color[2] + amount),
    )


def _inline_style_bits(node) -> int:
    """Wkład węzła INLINE w styl jego potomków.

    Parser tworzy SemanticNode(NodeType.INLINE, tag) dla <b>/<strong>/<i>/<em>.
    Sam węzeł INLINE nie niesie tekstu — styl spływa na potomne węzły TEXT.
    """
    if getattr(node, "typ", None) == NodeType.INLINE:
        tag = getattr(node, "tag", "")
        if tag in ("b", "strong"):
            return STYLE_BOLD
        if tag in ("i", "em"):
            return STYLE_ITALIC
    return STYLE_NONE


# ─── 0c. POLA FORMULARZA ────────────────────────────────────────────────────
_FIELD_TAGS = ("input", "textarea", "button", "select")


def _field_kind(node):
    """Klasyfikuje węzeł INLINE pola formularza na rodzaj obsługiwany w v1.

    Zwraca: 'text' | 'password' | 'textarea' | 'submit' | 'button' | 'select'
    | 'hidden' albo None (rodzaj pominięty w v1: checkbox/radio/file).
    """
    tag = getattr(node, "tag", "")
    attrs = getattr(node, "attrs", {}) or {}
    if tag == "input":
        t = (attrs.get("type", "text") or "text").lower()
        if t == "hidden":             return "hidden"
        if t in ("submit", "image"):  return "submit"
        if t in ("button", "reset"):  return "button"
        if t in ("checkbox", "radio"):  return t
        if t == "file":                 return None  # upload plików — niewspierany
        if t == "password":             return "password"
        return "text"
    if tag == "textarea":
        return "textarea"
    if tag == "button":
        t = (attrs.get("type", "submit") or "submit").lower()
        return "submit" if t == "submit" else "button"
    if tag == "select":
        return "select"
    return None


def _gather_options(node):
    """Zwraca (lista_opcji, indeks_wybrany) dla węzła <select>.

    Każda opcja to (value, label). value bierzemy z atrybutu, a gdy go brak —
    z tekstu opcji (zgodnie z HTML). 'selected' wskazuje domyślny wybór.
    Skanujemy rekurencyjnie (np. przez <optgroup>).
    """
    opts = []
    sel = -1

    def walk(n):
        nonlocal sel
        for c in getattr(n, "children", []) or []:
            ctag = getattr(c, "tag", "")
            cattrs = getattr(c, "attrs", {}) or {}
            if ctag == "option":
                label = c.get_plain_text().strip()
                raw = cattrs.get("value")
                value = raw if raw is not None else label
                if "selected" in cattrs:
                    sel = len(opts)
                opts.append((value, label or value))
            else:
                walk(c)

    walk(node)
    if sel < 0:
        sel = 0 if opts else -1
    return opts, sel


# ─── 0d. WYBÓR ADRESU OBRAZU (lazy-load / srcset) ───────────────────────────
def _is_svg(url: str) -> bool:
    u = url.split("?", 1)[0].split("#", 1)[0].lower()
    return u.endswith(".svg") or u.endswith(".svgz") or url[:14].lower() == "data:image/svg"


def _parse_srcset(srcset: str, base_url: str):
    """Parsuje atrybut srcset -> [(url_absolutny, score)].

    Deskryptor 'w' = szerokość w px, 'x' = gęstość, brak = 1.0. Parsowanie
    pragmatyczne (split po przecinku, potem po spacji) — pokrywa przytłaczającą
    większość realnych srcset (przecinki w URL są rzadkie i zwykle zakodowane).
    """
    out = []
    for part in srcset.split(","):
        part = part.strip()
        if not part:
            continue
        toks = part.split()
        url = toks[0]
        score = 1.0
        if len(toks) > 1:
            d = toks[1].strip().lower()
            try:
                if d.endswith("w"):
                    score = float(d[:-1])
                elif d.endswith("x"):
                    score = float(d[:-1])
            except ValueError:
                pass
        out.append((urllib.parse.urljoin(base_url, url), score))
    return out


def _pick_image_src(attrs: dict, base_url: str) -> str:
    """Wybiera najlepszy adres obrazu: lazy-load i srcset przed surowym src,
    raster przed SVG, a wśród rastrów największy wariant.

    Priorytet źródła (gdy rozmiary równe): srcset/data-* (realny cel) > src
    (który przy lazy-loadzie bywa tylko placeholderem 1x1).
    """
    candidates = []   # (priorytet_źródła, score, url)
    for key in ("data-srcset", "srcset"):
        v = attrs.get(key)
        if v:
            for url, score in _parse_srcset(v, base_url):
                candidates.append((2, score, url))
    for key in ("data-src", "data-original", "data-lazy-src", "data-lazy"):
        v = attrs.get(key)
        if v and v.strip():
            candidates.append((2, 1.0, urllib.parse.urljoin(base_url, v.strip())))
    v = attrs.get("src")
    if v and v.strip():
        candidates.append((1, 1.0, urllib.parse.urljoin(base_url, v.strip())))

    if not candidates:
        return ""
    # Preferuj rastry; jeśli są wyłącznie SVG, dopiero wtedy bierzemy SVG.
    raster = [c for c in candidates if not _is_svg(c[2])]
    pool = raster if raster else candidates
    pool.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return pool[0][2]


def _media_matches(media: str, viewport_w: int) -> bool:
    """Dopasowanie <source media> do szerokości widoku. Obsługuje min/max-width
    (też -device-width). Cechy nie-szerokościowe (orientation, resolution...) są
    ignorowane = traktowane jako spełnione, żeby nie odrzucać źródła zbyt ostro.
    Klauzule łączone 'and' — wszystkie ograniczenia szerokości muszą zachodzić."""
    m = (media or "").strip().lower()
    if not m:
        return True
    ok = True
    for mm in re.finditer(r'(min|max)-(?:device-)?width\s*:\s*(\d+(?:\.\d+)?)\s*px', m):
        bound, val = mm.group(1), float(mm.group(2))
        if bound == "min" and viewport_w < val:
            ok = False
        elif bound == "max" and viewport_w > val:
            ok = False
    return ok


def _source_type_ok(type_str: str) -> bool:
    """Czy potrafimy zdekodować typ <source type>? Odrzucamy znane nierenderowalne
    (SVG, AVIF); pusty/nieznany akceptujemy (próbujemy — błąd skończy się alt)."""
    t = (type_str or "").strip().lower()
    if not t:
        return True
    return ("svg" not in t) and ("avif" not in t)


def _pick_picture_src(node, base_url: str, viewport_w: int) -> str:
    """Rozwiązuje adres dla <picture>: pierwszy <source> pasujący typem i media,
    z największym wariantem rastrowym srcset; w razie braku — wewnętrzny <img>
    (przez zwykłe _pick_image_src). Zwraca '' gdy nie ma żadnego źródła."""
    img_attrs = None
    for child in getattr(node, "children", []) or []:
        ctag = (getattr(child, "tag", "") or "").lower()
        if ctag == "source":
            a = getattr(child, "attrs", {}) or {}
            if not _source_type_ok(a.get("type", "")):
                continue
            media = a.get("media", "")
            if media and not _media_matches(media, viewport_w):
                continue
            srcset = a.get("srcset") or a.get("data-srcset")
            if srcset:
                cands = _parse_srcset(srcset, base_url)
                raster = [c for c in cands if not _is_svg(c[0])]
                pool = raster if raster else cands
                if pool:
                    pool.sort(key=lambda c: c[1], reverse=True)
                    return pool[0][0]
            raw = a.get("src") or a.get("data-src")
            if raw and raw.strip():
                return urllib.parse.urljoin(base_url, raw.strip())
        elif ctag == "img" and img_attrs is None:
            img_attrs = getattr(child, "attrs", {}) or {}
    if img_attrs is not None:
        return _pick_image_src(img_attrs, base_url)
    return ""


# ─── 1. STRUKTURY DANYCH (Visual Box Tree) ──────────────────────────────────
class LayoutBox:
    __slots__ = ['rect', 'text', 'color', 'node', 'box_type', 'style', 'meta']
    def __init__(self, rect, text, color, node=None, box_type="text", style=STYLE_NONE, meta=None):
        self.rect = rect
        self.text = text
        self.color = color
        self.node = node
        self.box_type = box_type
        self.style = style
        self.meta = meta


# ─── 2. SILNIK UKŁADU (Iteracyjny DFS z przepływem Inline) ──────────────────
class LayoutEngine:
    def __init__(self, max_w: int, char_w: int, line_h: int, image_loader=None, base_url="", css_rules=None, measure_fn=None):
        self.max_w = max_w
        self.char_w = char_w
        self.line_h = line_h
        self.image_loader = image_loader
        self.base_url = base_url or ""
        self.css_rules = css_rules or []
        self.measure_fn = measure_fn

    def build(self, root_node, start_x: int, start_y: int):
        boxes = []
        # Krotka stosu: (węzeł, wcięcie, czy_post, styl_odziedziczony, form)
        # form = najbliższy przodek <form> (SemanticNode) albo None.
        stack = [(root_node, 0, False, STYLE_NONE, None)]
        bg_stack = []          # równoległy do post-markerów: ładunek tła (idx, top, kolor, indent) lub None
        current_y = start_y
        current_x = start_x

        def flush_line(indent):
            nonlocal current_x, current_y
            if current_x > start_x + indent:
                current_x = start_x + indent
                current_y += self.line_h

        def add_text_chunk(text, color, node, btype, indent, style=STYLE_NONE):
            nonlocal current_x, current_y
            space_w = self.measure_fn(" ", style) if self.measure_fn else self.char_w
            words = text.replace('\n', ' ').split(' ')
            for w in words:
                if not w:
                    continue
                w = w.translate(_ZW_TABLE)   # usuń znaki zerowej szerokości
                if not w:
                    continue
                w_len = self.measure_fn(w, style) if self.measure_fn else (len(w) * self.char_w)
                # Łamanie wiersza, gdy słowo nie mieści się w dokumencie
                if current_x + w_len > start_x + self.max_w - indent and current_x > start_x + indent:
                    flush_line(indent)
                r = pygame.Rect(current_x, current_y, w_len, self.line_h)
                boxes.append(LayoutBox(r, w, color, node, btype, style))
                current_x += w_len + space_w

        def add_field(node, kind, form, indent):
            nonlocal current_x, current_y
            attrs = getattr(node, "attrs", {}) or {}

            # Ukryte pole — nie rysujemy, ale bierze udział w wysyłce.
            if kind == "hidden":
                node.attrs.setdefault("value", attrs.get("value", ""))
                r = pygame.Rect(current_x, current_y, 0, 0)
                boxes.append(LayoutBox(r, "", (0, 0, 0), node, "hidden_field",
                                       STYLE_NONE, {"form": form, "kind": kind}))
                return

            # Textarea — traktujemy jak blok: własna linia, pełna szerokość, 3 wiersze.
            if kind == "textarea":
                flush_line(indent)
                node.attrs.setdefault("value", node.get_plain_text())
                w = max(self.char_w * 8, self.max_w - indent)
                h = self.line_h * 3 + 6
                r = pygame.Rect(start_x + indent, current_y, w, h)
                boxes.append(LayoutBox(r, "", (210, 210, 210), node, "textarea",
                                       STYLE_NONE, {"form": form, "kind": kind}))
                current_y += h + 4
                current_x = start_x + indent
                return

            # Checkbox / radio — mały kwadrat / koło, stan w _checked.
            if kind in ("checkbox", "radio"):
                node.attrs.setdefault("_checked", "checked" in attrs)
                w = self.line_h
                h = self.line_h
                if current_x + w > start_x + self.max_w - indent and current_x > start_x + indent:
                    flush_line(indent)
                r = pygame.Rect(current_x, current_y, w, h)
                boxes.append(LayoutBox(r, "", (210, 210, 210), node, kind,
                                       STYLE_NONE, {"form": form, "kind": kind}))
                current_x += w + self.char_w
                return

            # Select — lista opcji; w v1 wybór cyklicznie klikiem (bez popupu).
            if kind == "select":
                opts, sel = _gather_options(node)
                node.attrs.setdefault("_selected", sel if sel >= 0 else 0)
                cur_label = opts[node.attrs["_selected"]][1] if (opts and 0 <= node.attrs["_selected"] < len(opts)) else "—"
                label = cur_label + "  ▾"
                w = min(max(6, len(label) + 2) * self.char_w, self.max_w - indent)
                h = self.line_h
                if current_x + w > start_x + self.max_w - indent and current_x > start_x + indent:
                    flush_line(indent)
                r = pygame.Rect(current_x, current_y, w, h)
                boxes.append(LayoutBox(r, label, (225, 225, 235), node, "select",
                                       STYLE_NONE, {"form": form, "kind": "select", "options": opts}))
                current_x += w + self.char_w
                return

            # Przyciski — szerokość wg etykiety.
            if kind in ("submit", "button"):
                default_label = {"submit": "Wyślij", "button": "OK"}[kind]
                label = (attrs.get("value") or node.get_plain_text() or default_label).strip()
                w = max(4, len(label) + 2) * self.char_w
                btype = "button"
            else:
                # Pole tekstowe / hasło.
                node.attrs.setdefault("value", attrs.get("value", ""))
                try:
                    size = int(attrs.get("size", ""))
                except (TypeError, ValueError):
                    size = 0
                cols = size if size > 0 else 24
                w = cols * self.char_w
                label = ""
                btype = "input"

            w = min(w, self.max_w - indent)
            h = self.line_h
            # Zawijanie inline jak dla słowa.
            if current_x + w > start_x + self.max_w - indent and current_x > start_x + indent:
                flush_line(indent)
            r = pygame.Rect(current_x, current_y, w, h)
            boxes.append(LayoutBox(r, label, (210, 210, 210), node, btype,
                                   STYLE_NONE, {"form": form, "kind": kind}))
            current_x += w + self.char_w

        def add_image(node, indent, src_override=None):
            nonlocal current_x, current_y
            attrs = getattr(node, "attrs", {}) or {}
            # Wybór adresu: lazy-load (data-src/srcset) i największy wariant rastrowy
            # przed surowym src; src bywa przy lazy-loadzie placeholderem 1x1.
            # src_override (z <picture>) ma pierwszeństwo, gdy podany.
            src = src_override if src_override is not None else _pick_image_src(attrs, self.base_url)
            alt = attrs.get("alt", "") or ""

            def _px(v):
                try:
                    return int(str(v).strip().lower().replace("px", ""))
                except (TypeError, ValueError):
                    return 0

            aw, ah = _px(attrs.get("width")), _px(attrs.get("height"))
            iw = ih = 0
            if self.image_loader is not None and src:
                isz = self.image_loader.intrinsic_size(src)
                if isz:
                    iw, ih = isz

            # Rozmiar docelowy: atrybuty > realne wymiary > placeholder 16:9.
            if aw and ah:
                w, h = aw, ah
            elif iw and ih:
                if aw:
                    w, h = aw, max(1, round(aw * ih / iw))
                elif ah:
                    h, w = ah, max(1, round(ah * iw / ih))
                else:
                    w, h = iw, ih
            else:
                w = aw or 320
                h = ah or max(1, round(w * 9 / 16))

            avail = self.max_w - indent
            if w > avail and w > 0:
                h = max(1, round(h * avail / w))
                w = avail

            meta = {"src": src, "alt": alt}
            # Animowany GIF -> medium termiczne (oscylator). Pudełko zostaje typu
            # "image" (layout/rozmiar bez zmian); renderer skieruje je do MediaLayer.
            if src:
                low = src.split("?", 1)[0].lower()
                if low.endswith(".gif"):
                    meta["media_kind"] = "gif"
                    meta["media_aid"] = _gif_aid(src)
                elif low.endswith(".svg"):
                    meta["media_kind"] = "vector"   # SVG -> łamane (ThermalVector)
                    meta["media_aid"] = _svg_aid(src)
            # Małe obrazy płyną inline (ikony); duże zachowują się jak blok.
            if h <= self.line_h:
                if current_x + w > start_x + self.max_w - indent and current_x > start_x + indent:
                    flush_line(indent)
                r = pygame.Rect(current_x, current_y, w, h)
                boxes.append(LayoutBox(r, "", (60, 60, 75), node, "image", STYLE_NONE, meta))
                current_x += w + self.char_w
            else:
                flush_line(indent)
                r = pygame.Rect(start_x + indent, current_y, w, h)
                boxes.append(LayoutBox(r, "", (60, 60, 75), node, "image", STYLE_NONE, meta))
                current_y += h + 4
                current_x = start_x + indent

        def add_picture(node, indent):
            # <picture>: wybierz adres z rodzeństwa <source> (media/type) lub
            # z wewnętrznego <img>; wyemituj JEDNO pudełko obrazu (wymiary/alt z
            # <img>, jeśli jest). Nie schodzimy potem w dzieci picture.
            inner_img = None
            for child in getattr(node, "children", []) or []:
                if (getattr(child, "tag", "") or "").lower() == "img":
                    inner_img = child
                    break
            resolved = _pick_picture_src(node, self.base_url, self.max_w)
            target = inner_img if inner_img is not None else node
            add_image(target, indent, src_override=resolved)

        def add_sprite(node, indent, spr):
            """Emisja wycinka atlasu (sprite CSS) jako pudełko-medium w przepływie.
            Rozmiar = width/height z CSS (crop[2:]); atom-sprite trafia do substratu,
            atlas do kolejki pobrań. Renderuje SpriteProvider (crop-blit z termiką)."""
            nonlocal current_x, current_y
            src = spr.get("src"); crop = spr.get("crop")
            if not src or not crop:
                return
            w = crop[2] if crop[2] > 0 else 16
            h = crop[3] if crop[3] > 0 else 16
            aid = _sprite_aid(src, crop)
            rt = getattr(self.image_loader, "_runtime", None)
            if rt is not None and not rt.matrix.has(aid):
                rt.matrix.create(aid, S="media:sprite", E=src, T=70.0, metadata=spr)
            if self.image_loader is not None:
                try:
                    self.image_loader.request(src)      # atlas pobierany jak obraz
                except Exception:
                    pass
            meta = {"src": src, "media_kind": "sprite", "media_aid": aid}
            avail = self.max_w - indent
            if w > avail and w > 0:
                h = max(1, round(h * avail / w)); w = avail
            if h <= self.line_h:                        # ikona -> inline
                if current_x + w > start_x + self.max_w - indent and current_x > start_x + indent:
                    flush_line(indent)
                r = pygame.Rect(current_x, current_y, w, h)
                boxes.append(LayoutBox(r, "", (60, 60, 75), node, "image", STYLE_NONE, meta))
                current_x += w + self.char_w
            else:                                       # duży -> blok
                flush_line(indent)
                r = pygame.Rect(start_x + indent, current_y, w, h)
                boxes.append(LayoutBox(r, "", (60, 60, 75), node, "image", STYLE_NONE, meta))
                current_y += h + 4
                current_x = start_x + indent

        while stack:
            node, indent, is_post, style, form = stack.pop()
            
            is_block = node.typ in (NodeType.BLOCK, NodeType.LIST, NodeType.PRE, NodeType.TABLE, NodeType.HR)
            
            # Post-processing bloków
            if is_post:
                bg = bg_stack.pop() if bg_stack else None
                if is_block:
                    flush_line(indent)
                if bg is not None:
                    _idx, _top, _col, _ind = bg
                    if current_y >= _top:
                        _fr = pygame.Rect(start_x + _ind, _top,
                                          self.max_w - _ind, max(self.line_h, current_y - _top))
                        boxes.insert(_idx, LayoutBox(_fr, "", _col, node, "fill", STYLE_NONE, None))
                if is_block:
                    current_y += 6
                continue
                
            # Pre-processing bloków
            if is_block:
                flush_line(indent)
                
            typ = node.typ
            tag = getattr(node, "tag", "")
            
            # Czy to renderowane pole formularza? (przesądza też o NIE schodzeniu w dzieci)
            is_field = False
            field_kind = None
            if typ == NodeType.INLINE and tag in _FIELD_TAGS:
                field_kind = _field_kind(node)
                is_field = field_kind is not None
            is_image = (typ == NodeType.INLINE and tag == "img")
            is_picture = (typ == NodeType.INLINE and tag == "picture")

            # ── CSS: display:none/visibility:hidden (pomiń poddrzewo) + sprite'y + tło ──
            _bg_payload = None
            if _HAS_CSS and typ != NodeType.TEXT and tag:
                _attrs = getattr(node, "attrs", {}) or {}
                _decls = {}
                if self.css_rules:
                    _decls = _css_resolve(self.css_rules, tag,
                                          _attrs.get("id"), _css_classes(_attrs))
                if _attrs.get("style"):
                    _decls = {**_decls, **_css_inline(_attrs["style"])}   # inline wygrywa
                if _decls:
                    if (_decls.get("display", "").strip().lower() == "none"
                            or _decls.get("visibility", "").strip().lower() == "hidden"):
                        continue                                    # element i dzieci pominięte
                    if not is_image and not is_picture:
                        _spr = _sprite_from_decls(_decls, self.base_url)
                        if _spr:
                            add_sprite(node, indent, _spr)
                    if is_block:                                    # tło bloku -> wypełnienie za treścią
                        _col = _css_bgcolor(_decls)
                        if _col is not None:
                            _bg_payload = (len(boxes), current_y, _col, indent)

            if typ == NodeType.TEXT:
                text = (node.text or "").strip()
                if text:
                    add_text_chunk(text, (230, 230, 230), node, "text", indent, style)
                    
            elif typ == NodeType.HEADING:
                flush_line(indent)
                current_y += self.line_h // 2
                text = node.get_plain_text().strip()
                if text:
                    add_text_chunk(text, (255, 195, 90), node, "heading", indent, style)
                flush_line(indent)
                current_y += self.line_h // 4
                
            elif typ == NodeType.LINK:
                text = node.get_plain_text().strip()
                if text:
                    add_text_chunk(text, (90, 165, 255), node, "link", indent, style)
                    
            elif typ == NodeType.HR:
                flush_line(indent)
                current_y += self.line_h // 2
                r = pygame.Rect(start_x + indent, current_y, self.max_w, 2)
                boxes.append(LayoutBox(r, "", (80, 80, 95), node, "hr"))
                current_y += self.line_h // 2
                
            elif is_field:
                add_field(node, field_kind, form, indent)
                
            elif is_image:
                add_image(node, indent)

            elif is_picture:
                add_picture(node, indent)
                
            next_indent = indent + (20 if typ == NodeType.LIST else 0)
            # Styl spływa w dół: węzeł INLINE <b>/<i> dokłada swój bit potomkom.
            child_style = style | _inline_style_bits(node)
            # Kontekst formularza: wejście w <form> ustawia go dla potomków.
            child_form = node if (typ == NodeType.INLINE and tag == "form") else form
            
            stack.append((node, indent, True, style, form))
            bg_stack.append(_bg_payload)      # równolegle: tło bloku (lub None)
            # Nie schodzimy w dzieci LINK/HEADING (tekst brany przez get_plain_text),
            # pól, obrazów ani <picture> (zawartość już skonsumowana).
            if typ not in (NodeType.LINK, NodeType.HEADING) and not is_field and not is_image and not is_picture:
                for child in reversed(node.children):
                    stack.append((child, next_indent, False, child_style, child_form))
                    
        flush_line(0)
        return boxes, current_y


# ─── 3. RENDERER DOM Z CACHE FONTÓW I CULLINGIEM ───────────────────────────
class DOMRenderer:
    def __init__(self, font, fonts=None, bold_via_color=True):
        # font: czcionka bazowa (fallback dla każdego stylu).
        # fonts: opcjonalny słownik {"regular","bold","italic","bold_italic"}.
        #        Gdy None, każdy styl używa czcionki bazowej (zachowanie sprzed
        #        formatowania — nic się nie psuje, jeśli display nie poda wariantów).
        # bold_via_color: gdy True, <b>/<strong> wyróżniamy jaśniejszym kolorem,
        #        bo baza monospace jest już bold i nie ma cięższego wariantu.
        self.font = font
        self.fonts = fonts or {}
        self.bold_via_color = bold_via_color
        self.font_cache = {}

    def _font_for(self, style: int):
        if not self.fonts:
            return self.font
        bold = bool(style & STYLE_BOLD)
        ital = bool(style & STYLE_ITALIC)
        if bold and ital:
            return self.fonts.get("bold_italic") or self.fonts.get("italic") or self.font
        if ital:
            return self.fonts.get("italic") or self.font
        if bold:
            return self.fonts.get("bold") or self.font
        return self.fonts.get("regular") or self.font

    @staticmethod
    def _render_text(font, text, color):
        """Render odporny na pygame-ce: render('' lub znaku zerowej szerokości)
        rzuca 'Text has zero width'. W takim wypadku zwracamy 1×h przezroczystą
        powierzchnię zamiast wywracać całą pętlę renderu."""
        import pygame
        try:
            return font.render(text, True, color)
        except pygame.error:
            return pygame.Surface((1, max(1, font.get_height())), pygame.SRCALPHA)

    def _get_text_surface(self, text: str, color: tuple, style: int = STYLE_NONE):
        # Klucz cache obejmuje styl — inaczej bold/italic kolidowałyby z regular.
        key = (text, color, style)
        if key not in self.font_cache:
            self.font_cache[key] = self._render_text(self._font_for(style), text, color)
        return self.font_cache[key]

    def draw(self, ctx: DrawCtx, boxes: list, scroll_y: int, clip_rect, hovered_box=None, focused_box=None, image_loader=None, media=None):
        import pygame
        old_clip = ctx.surface.get_clip()
        ctx.surface.set_clip(clip_rect)
        blink_on = (pygame.time.get_ticks() // 500) % 2 == 0
        
        for box in boxes:
            box_y = box.rect.y - scroll_y
            
            if box_y + box.rect.h < clip_rect.y:
                continue
            if box_y > clip_rect.bottom:
                break
            
            # Kolor: najpierw ewentualne wyróżnienie bold (jaśniej), potem
            # poświata pod kursorem. Bazowy box.color pozostaje nietknięty.
            draw_color = box.color
            if self.bold_via_color and (box.style & STYLE_BOLD):
                draw_color = _brighten(draw_color, 35)
            if hovered_box is not None and box is hovered_box:
                draw_color = _brighten(draw_color, 80)
                
            if box.box_type in ("text", "heading", "link"):
                surf = self._get_text_surface(box.text, draw_color, box.style)
                ctx.surface.blit(surf, (box.rect.x, box_y))
                
                if box.box_type == "link":
                    # Podkreślenie liczymy z wysokości użytej czcionki wariantu.
                    line_y = box_y + self._font_for(box.style).get_height()
                    pygame.draw.line(
                        ctx.surface, draw_color,
                        (box.rect.x, line_y),
                        (box.rect.x + surf.get_width(), line_y)
                    )

            elif box.box_type == "hr":
                pygame.draw.line(
                    ctx.surface, draw_color,
                    (box.rect.x, box_y), 
                    (box.rect.x + box.rect.w, box_y)
                )

            elif box.box_type in ("input", "textarea", "button", "select", "checkbox", "radio"):
                focused = (focused_box is not None and box is focused_box)
                self._draw_field(ctx, box, box_y, focused, blink_on)

            elif box.box_type == "image":
                self._draw_image(ctx, box, box_y, image_loader, media)
            elif box.box_type == "fill":
                ctx.box(pygame.Rect(box.rect.x, box_y, box.rect.w, box.rect.h),
                        fill=box.color)
            # box_type == "hidden_field" -> nic nie rysujemy

        ctx.surface.set_clip(old_clip)

    def _draw_field(self, ctx, box, box_y, focused, blink_on):
        import pygame
        kind = box.meta.get("kind") if box.meta else ""
        rect = pygame.Rect(box.rect.x, box_y, box.rect.w, box.rect.h)
        font = self._font_for(STYLE_NONE)
        attrs = getattr(box.node, "attrs", {}) if box.node else {}

        # Checkbox / radio — wskaźnik stanu.
        if box.box_type in ("checkbox", "radio"):
            checked = bool(attrs.get("_checked"))
            outline = (120, 200, 255) if focused else (150, 150, 170)
            pad = 3
            inner = pygame.Rect(rect.x + pad, rect.y + pad, rect.h - 2 * pad, rect.h - 2 * pad)
            if box.box_type == "checkbox":
                pygame.draw.rect(ctx.surface, (22, 22, 32), inner, 0, 2)
                pygame.draw.rect(ctx.surface, outline, inner, 1, 2)
                if checked:
                    x, y, w, h = inner.x, inner.y, inner.w, inner.h
                    pygame.draw.lines(ctx.surface, (120, 220, 150), False,
                                      [(x + 3, int(y + h * 0.55)),
                                       (int(x + w * 0.42), y + h - 3),
                                       (x + w - 2, y + 2)], 2)
            else:
                cx, cy = inner.center
                rad = inner.w // 2
                pygame.draw.circle(ctx.surface, (22, 22, 32), (cx, cy), rad)
                pygame.draw.circle(ctx.surface, outline, (cx, cy), rad, 1)
                if checked:
                    pygame.draw.circle(ctx.surface, (120, 200, 255), (cx, cy), max(2, rad - 3))
            return

        # Select — pudełko z bieżącą etykietą i strzałką (czytane na żywo z węzła).
        if box.box_type == "select":
            opts = box.meta.get("options") if box.meta else []
            idx = attrs.get("_selected", 0)
            cur = opts[idx][1] if (opts and 0 <= idx < len(opts)) else "—"
            fill    = (70, 70, 100) if focused else (40, 40, 60)
            outline = (120, 200, 255) if focused else (90, 90, 120)
            pygame.draw.rect(ctx.surface, fill, rect, 0, 4)
            pygame.draw.rect(ctx.surface, outline, rect, 1, 4)
            ls = self._render_text(font, cur + "  ▾", (225, 225, 235))
            old = ctx.surface.get_clip()
            ctx.surface.set_clip(rect)
            ctx.surface.blit(ls, (rect.x + 6, rect.y + max(0, (rect.h - ls.get_height()) // 2)))
            ctx.surface.set_clip(old)
            return

        # Przycisk submit/zwykły — wypełnione pudełko z wyśrodkowaną etykietą.
        if box.box_type == "button":
            fill    = (70, 70, 100) if focused else (40, 40, 60)
            outline = (120, 200, 255) if focused else (90, 90, 120)
            pygame.draw.rect(ctx.surface, fill, rect, 0, 4)
            pygame.draw.rect(ctx.surface, outline, rect, 1, 4)
            ls = self._render_text(font, box.text, (225, 225, 235))
            ctx.surface.blit(ls, (rect.x + max(4, (rect.w - ls.get_width()) // 2),
                                  rect.y + max(0, (rect.h - ls.get_height()) // 2)))
            return

        # Pola edytowalne (input / textarea) — ramka, wartość, kursor.
        outline = (120, 200, 255) if focused else (70, 70, 95)
        pygame.draw.rect(ctx.surface, (22, 22, 32), rect, 0, 3)
        pygame.draw.rect(ctx.surface, outline, rect, 1, 3)

        val = attrs.get("value", "")
        if kind == "password":
            shown = "•" * len(val)
            color = (235, 235, 240)
        elif val:
            shown = val
            color = (235, 235, 240)
        elif focused:
            shown = ""
            color = (235, 235, 240)
        else:
            shown = attrs.get("placeholder", "")
            color = (110, 110, 125)

        pad = 6
        inner = pygame.Rect(rect.x + pad, rect.y, rect.w - 2 * pad, rect.h)
        old = ctx.surface.get_clip()
        ctx.surface.set_clip(inner)

        ty = rect.y + max(0, (self.line_height_hint() - font.get_height()) // 2)
        ts = self._render_text(font, shown, color)
        tw = ts.get_width()
        caret_w = font.size("|")[0]
        # Przewijanie poziome, by koniec tekstu (kursor) był widoczny w polu skupionym.
        shift = max(0, tw - (inner.w - caret_w - 2)) if focused else 0
        ctx.surface.blit(ts, (inner.x - shift, ty))
        if focused and blink_on:
            cs = self._render_text(font, "|", (235, 235, 240))
            ctx.surface.blit(cs, (inner.x - shift + tw, ty))

        ctx.surface.set_clip(old)

    def line_height_hint(self):
        # Wysokość pojedynczego wiersza tekstu (do pionowego centrowania w polach).
        return self._font_for(STYLE_NONE).get_height() + 2

    def _draw_image(self, ctx, box, box_y, loader, media=None):
        import pygame
        rect = pygame.Rect(box.rect.x, box_y, box.rect.w, box.rect.h)
        meta = box.meta or {}
        src = meta.get("src", "")
        alt = meta.get("alt", "")

        # Medium termiczne (GIF/wideo/wektor): widoczność grzeje, MediaLayer rysuje
        # bieżącą klatkę. Rodzaj z meta (po rozszerzeniu) albo z ładowarki (po treści
        # — łapie SVG/GIF bez rozszerzenia). Gdy niezaładowane -> placeholder niżej.
        kind = meta.get("media_kind")
        aid = meta.get("media_aid")
        if not kind and loader is not None and src:
            k = loader.media_kind(src)
            if k:
                kind, aid = k, loader.media_aid(src)
        if kind and media is not None:
            media.note_visible(kind, aid)
            if media.draw(ctx, kind, aid, rect):
                return

        surf, status = (None, "empty")
        if loader is not None and src:
            surf, status = loader.get(src, box.rect.w, box.rect.h)

        if surf is not None:
            ctx.surface.blit(surf, (rect.x, rect.y))
            return

        # Placeholder / ładowanie / błąd — ramka + tekst alt.
        pygame.draw.rect(ctx.surface, (28, 28, 38), rect, 0, 3)
        border = (120, 60, 60) if status == "error" else (70, 70, 95)
        pygame.draw.rect(ctx.surface, border, rect, 1, 3)

        font = self._font_for(STYLE_NONE)
        if status == "error":
            label = "✕ " + (alt or "obraz")
            col = (200, 120, 120)
        elif status == "loading":
            label = "… " + (alt or "ładowanie")
            col = (140, 140, 160)
        else:
            label = alt or "obraz"
            col = (120, 120, 140)

        old = ctx.surface.get_clip()
        ctx.surface.set_clip(rect)
        ls = self._render_text(font, label, col)
        ctx.surface.blit(ls, (rect.x + 6, rect.y + max(0, (rect.h - ls.get_height()) // 2)))
        ctx.surface.set_clip(old)


# ─── 4. ADAPTER WIDOKU (GraphicPageViewer) ──────────────────────────────────
class GraphicPageViewer:
    def __init__(self, browser, close_callback, mapper=None):
        self.browser = browser
        self.close_callback = close_callback
        # Mapper DOM↔atom (autorytet z attach_to_browser). Trzymany dla
        # ewentualnych przyszłych zapytań; samo hover-heat go nie potrzebuje —
        # rozwiązuje atomy przez węzłowe _atom_refs i browser.runtime.get_atom.
        self.mapper = mapper
        
        self.scroll_y = 0
        self.max_scroll = 0
        self.nav_h = 45
        
        self.layout_boxes = []
        self.last_page_url = None
        self.last_dom_version = -1
        self.dom_renderer = None
        
        self.url_active = False
        self.url_text = ""
        self.btn_back_rect = None
        self.input_url_rect = None

        # ── Stan hover-heat ──────────────────────────────────────────────
        self.hovered_box = None          # box aktualnie pod kursorem (do poświaty)
        self._last_heated_node = None    # węzeł ostatnio podgrzany (detekcja wejścia)
        self._last_heat_ms = 0           # znacznik czasu ostatniego podgrzania
        self.heat_repeat_ms = 400        # min. odstęp ponownego podgrzania TEGO SAMEGO węzła
        self.hover_weight = 1.0          # siła sygnału uwagi (świadomie nie 2.0 — patrz uwagi)

        # ── Stan formularzy ──────────────────────────────────────────────
        self.focused_field = None        # LayoutBox pola z focusem (input/textarea) albo None
        self._post_pending = None        # (target, pary) gdy method=POST — v2 to obsłuży

        # ── Stan grafiki (E1) ────────────────────────────────────────────
        self._images_dirty = False       # obraz się zdekodował -> przelicz layout
        self._js_dirty = False           # JS zmutował DOM -> przelicz layout (Szew B)
        self.show_hot = False            # overlay "GORĄCE TERAZ" (klawisz H)
        try:
            rt = getattr(self.browser, "runtime", None)
            self.image_loader = ImageLoader(runtime=rt) if _HAS_IMAGES else None
        except Exception:
            self.image_loader = None
        # Wspólny szew mediów (GIF/wektor/...) — renderer i pętla dotykają tylko jego.
        self.media = getattr(getattr(self.browser, "runtime", None), "media", None)
        if self.media is not None:
            sp = self.media.provider_for("sprite")
            if sp is not None:
                sp.set_loader(self.image_loader)   # sprite tnie z tekstur ładowarki
        # Asynchroniczne pobieranie zewnętrznych arkuszy CSS.
        try:
            from karmazyn_css_loader import CssLoader
            self.css_loader = CssLoader(runtime=getattr(self.browser, "runtime", None))
        except Exception:
            self.css_loader = None
        self._css_page_url = None

    def wants_keys(self):
        return True

    def on_key(self, event):
        import pygame
        if self.url_active:
            if event.key == pygame.K_RETURN:
                if self.url_text.strip():
                    self.browser.go(self.url_text.strip())
                self.url_active = False
            elif event.key == pygame.K_BACKSPACE:
                self.url_text = self.url_text[:-1]
            elif event.key == pygame.K_ESCAPE:
                self.url_active = False
            elif event.unicode and event.unicode.isprintable():
                self.url_text += event.unicode
            return True

        # Pole formularza z focusem przechwytuje edycję; klawisze nieobsłużone
        # (np. strzałki) przepuszczamy niżej do obsługi scrolla.
        if self.focused_field is not None:
            if self._field_key(event):
                return True

        if event.key == pygame.K_UP:
            self.scroll_y = max(0, self.scroll_y - 40)
        elif event.key == pygame.K_DOWN:
            self.scroll_y = min(self.max_scroll, self.scroll_y + 40)
        elif event.key == pygame.K_PAGEUP:
            self.scroll_y = max(0, self.scroll_y - 300)
        elif event.key == pygame.K_PAGEDOWN:
            self.scroll_y = min(self.max_scroll, self.scroll_y + 300)
        elif event.key == pygame.K_h:
            self.show_hot = not self.show_hot     # overlay "GORĄCE TERAZ"
        elif event.key == pygame.K_ESCAPE:
            if self.show_hot:
                self.show_hot = False             # ESC zamyka najpierw overlay
            else:
                self.close_callback()
            
        return True

    # ── FORMULARZE: edycja pola z focusem ────────────────────────────────
    def _field_key(self, event) -> bool:
        """Obsługuje klawisz dla pola z focusem. Zwraca True, gdy skonsumowano."""
        import pygame
        box = self.focused_field
        if box is None or box.box_type not in ("input", "textarea") or box.node is None:
            return False
        val = box.node.attrs.get("value", "")
        if event.key == pygame.K_RETURN:
            # Enter zatwierdza formularz (v1: także w textarea — multiline w v2).
            self._submit_form(box)
            return True
        if event.key == pygame.K_BACKSPACE:
            box.node.attrs["value"] = val[:-1]
            return True
        if event.key == pygame.K_ESCAPE:
            self.focused_field = None
            return True
        if event.key == pygame.K_TAB:
            self._focus_next_field()
            return True
        if event.unicode and event.unicode.isprintable():
            box.node.attrs["value"] = val + event.unicode
            return True
        return False

    def _focus_next_field(self):
        """Przenosi focus na kolejne pole edytowalne (Tab), z zawijaniem."""
        fields = [b for b in self.layout_boxes if b.box_type in ("input", "textarea")]
        if not fields:
            self.focused_field = None
            return
        if self.focused_field in fields:
            i = fields.index(self.focused_field)
            self.focused_field = fields[(i + 1) % len(fields)]
        else:
            self.focused_field = fields[0]

    def _submit_form(self, trigger_box) -> bool:
        """Zbiera pola tego samego formularza, buduje dane i wysyła.

        GET -> query doklejony do action. POST -> browser.post (jeśli dostępny;
        starszy browser bez POST zapisuje zamiar w self._post_pending).
        Reguły pól: checkbox/radio wchodzą tylko gdy zaznaczone; select wnosi
        wartość wybranej opcji; pola bez atrybutu name są pomijane.
        """
        form = trigger_box.meta.get("form") if trigger_box.meta else None

        pairs = []
        for b in self.layout_boxes:
            bt = b.box_type
            if bt not in ("input", "textarea", "hidden_field", "checkbox", "radio", "select"):
                continue
            if (b.meta.get("form") if b.meta else None) is not form:
                continue
            if b.node is None:
                continue
            name = b.node.attrs.get("name")
            if not name:
                continue
            if bt in ("checkbox", "radio"):
                if b.node.attrs.get("_checked"):
                    pairs.append((name, b.node.attrs.get("value", "on")))
            elif bt == "select":
                opts = b.meta.get("options") if b.meta else []
                idx = b.node.attrs.get("_selected", 0)
                if opts and 0 <= idx < len(opts):
                    pairs.append((name, opts[idx][0]))
            else:
                pairs.append((name, b.node.attrs.get("value", "")))

        # Para name=value przycisku submit — tylko gdy wyzwalaczem był przycisk
        # (pole tekstowe jest już policzone w pętli powyżej; inaczej duplikat).
        if trigger_box.box_type == "button" and trigger_box.node is not None:
            sname = trigger_box.node.attrs.get("name")
            if sname:
                pairs.append((sname, trigger_box.node.attrs.get("value", "")))

        page = getattr(self.browser, "_current", None)
        base = page.url if page else ""
        action = (form.attrs.get("action") if form is not None else "") or ""
        method = (form.attrs.get("method", "get") if form is not None else "get").lower()
        target = urllib.parse.urljoin(base, action) if action else base
        if not target:
            return False

        if method == "post":
            poster = getattr(self.browser, "post", None)
            if callable(poster):
                self.focused_field = None
                poster(target, pairs)
                return True
            # Starszy browser bez POST — zachowujemy dotychczasowy fallback.
            self._post_pending = (target, pairs)
            return False

        query = urllib.parse.urlencode(pairs)
        if query:
            sep = "&" if ("?" in target) else "?"
            target = target + sep + query
        self.focused_field = None
        self.browser.go(target)
        return True

    def on_mouse(self, event, rect):
        import pygame
        if event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 4:
                self.scroll_y = max(0, self.scroll_y - 60)
                return True
            elif event.button == 5:
                self.scroll_y = min(self.max_scroll, self.scroll_y + 60)
                return True
            elif event.button == 1:
                mx, my = event.pos
                
                if self.btn_back_rect and self.btn_back_rect.collidepoint(mx, my):
                    self.browser.back()
                    self.url_active = False
                    return True
                    
                if self.input_url_rect and self.input_url_rect.collidepoint(mx, my):
                    self.url_active = True
                    self.focused_field = None
                    page = getattr(self.browser, "_current", None)
                    if not self.url_text and page:
                        self.url_text = page.url
                    return True
                else:
                    self.url_active = False
                    # Klik poza polem odfocusowuje; pętla niżej może focus przywrócić.
                    self.focused_field = None

                current_page = getattr(self.browser, "_current", None)
                if not current_page:
                    return False
                    
                for box in self.layout_boxes:
                    hit_rect = pygame.Rect(box.rect.x, box.rect.y - self.scroll_y, box.rect.w, box.rect.h)
                    
                    if hit_rect.bottom < rect.y:
                        continue
                    if hit_rect.top > rect.bottom:
                        break
                        
                    if box.box_type in ("input", "textarea") and hit_rect.collidepoint(mx, my):
                        self.focused_field = box
                        return True
                        
                    if box.box_type == "checkbox" and hit_rect.collidepoint(mx, my):
                        if box.node is not None:
                            box.node.attrs["_checked"] = not bool(box.node.attrs.get("_checked"))
                        return True
                        
                    if box.box_type == "radio" and hit_rect.collidepoint(mx, my):
                        self._select_radio(box)
                        return True
                        
                    if box.box_type == "select" and hit_rect.collidepoint(mx, my):
                        self._cycle_select(box)
                        return True
                        
                    if box.box_type == "button" and hit_rect.collidepoint(mx, my):
                        kind = box.meta.get("kind") if box.meta else ""
                        if kind == "submit":
                            self._submit_form(box)
                        return True
                        
                    if box.box_type == "link" and hit_rect.collidepoint(mx, my):
                        href = box.node.attrs.get('href') if box.node else None
                        if href:
                            target_url = urllib.parse.urljoin(current_page.url, href)
                            self.browser.go(target_url)
                        return True
        return False

    # ── FORMULARZE: stan checkbox / radio / select ───────────────────────
    def _select_radio(self, box):
        """Zaznacza ten radio i odznacza pozostałe w tej samej grupie (form+name)."""
        node = box.node
        if node is None:
            return
        form = box.meta.get("form") if box.meta else None
        name = node.attrs.get("name")
        for b in self.layout_boxes:
            if b.box_type != "radio" or b.node is None:
                continue
            if (b.meta.get("form") if b.meta else None) is not form:
                continue
            if b.node.attrs.get("name") != name:
                continue
            b.node.attrs["_checked"] = (b is box)

    def _cycle_select(self, box):
        """Przełącza wybór <select> na kolejną opcję (klik), z zawijaniem."""
        opts = box.meta.get("options") if box.meta else []
        if not opts or box.node is None:
            return
        idx = box.node.attrs.get("_selected", 0)
        box.node.attrs["_selected"] = (idx + 1) % len(opts)

    # ── HOVER-HEAT: detekcja (czysta) ────────────────────────────────────
    def _find_hovered_box(self, mx: int, my: int, top_limit: int, bottom_limit: int):
        """Zwraca box tekstowy pod kursorem albo None. Bez efektów ubocznych.

        Granice (top_limit/bottom_limit) są dynamiczne — bierzemy je z
        rzeczywistego clip_rect renderu, więc nic nie jest zaszyte na sztywno.
        """
        import pygame
        if my < top_limit or my > bottom_limit:
            return None
        for box in self.layout_boxes:
            if box.box_type not in ("text", "heading", "link"):
                continue
            draw_top = box.rect.y - self.scroll_y
            if draw_top + box.rect.h < top_limit:
                continue
            if draw_top > bottom_limit:
                break
            hit = pygame.Rect(box.rect.x, draw_top, box.rect.w, box.rect.h)
            if hit.collidepoint(mx, my):
                return box
        return None

    # ── HOVER-HEAT: etykiety atomów węzła ────────────────────────────────
    def _atom_labels_for_node(self, node):
        """Zwraca krotkę etykiet atomów powiązanych z węzłem DOM.

        Wiązanie pochodzi z DOMMapper._walk, który przy mapowaniu strony
        stempluje węzeł polem _atom_refs (lista etykiet). To JEDYNE wiążące
        źródło — etykiety atomów są syntetyczne i sekwencyjne
        (np. 'dom_<hash>_txt3'), więc nie da się ich odtworzyć z treści węzła.
        Gdy węzeł nie był zmapowany (np. tekst nieczytelny albo remap-guard
        300 s pominął ponowny walk po re-parsie), zwracamy pustą krotkę i
        podgrzewanie po prostu milczy.
        """
        refs = getattr(node, "_atom_refs", None)
        if not refs:
            return ()
        return tuple(refs)

    # ── HOVER-HEAT: aplikacja ciepła (na wejściu + throttle) ─────────────
    def _apply_hover_heat(self, box, now_ms: int):
        """Podgrzewa atomy pod kursorem.

        Kluczowa różnica względem naiwnej wersji: ciepło leci TYLKO przy
        zmianie węzła (wejście kursora na nowy fragment) albo po upływie
        heat_repeat_ms, gdy kursor wciąż stoi nad tym samym węzłem.
        Nigdy 60×/s — przy HEAT_READ=10.0 to byłby natychmiastowy przegrzew
        substratu do T_MAX.
        """
        node = getattr(box, "node", None) if box is not None else None
        if node is None:
            self._last_heated_node = None
            return

        changed = node is not self._last_heated_node
        if not changed and (now_ms - self._last_heat_ms) < self.heat_repeat_ms:
            return

        labels = self._atom_labels_for_node(node)
        runtime = getattr(self.browser, "runtime", None)
        getter = getattr(runtime, "get_atom", None) if runtime is not None else None

        if labels and callable(getter):
            # Jeden węzeł tekstowy może odpowiadać kilku atomom zdaniowym —
            # podgrzewamy wszystkie, bo layout nie wie, które słowo to które zdanie.
            for label in labels:
                try:
                    atom = getter(label)
                except Exception:
                    continue
                if atom is None:
                    continue
                touch = getattr(atom, "touch", None)
                if callable(touch):
                    try:
                        touch(weight=self.hover_weight)
                    except Exception:
                        pass

        self._last_heated_node = node
        self._last_heat_ms = now_ms

    def _rebuild_layout_if_needed(self, ctx: DrawCtx, page):
        version = getattr(self.browser, '_dom_version', 0)
        page_changed = (page.url != self.last_page_url or version != self.last_dom_version)
        if not page_changed and not self._images_dirty and not self._js_dirty:
            return

        if self.dom_renderer is None:
            self.dom_renderer = DOMRenderer(ctx.font, getattr(ctx, "fonts", None), bold_via_color=False)

        self.last_page_url = page.url
        self.last_dom_version = version
        self._images_dirty = False
        self._js_dirty = False

        # Reset scrolla/hoveru/focusu TYLKO przy zmianie strony; przebudowa
        # wywołana zdekodowaniem obrazu zachowuje pozycję i stan.
        if page_changed:
            self.scroll_y = 0
            self.hovered_box = None
            self._last_heated_node = None
            self.focused_field = None
            if self.dom_renderer:
                self.dom_renderer.font_cache.clear() # Zwalnianie osieroconych tekstur

        tree = getattr(page, "semantic_tree", None)
        if not tree:
            self.layout_boxes = []
            self.max_scroll = 0
            return

        cx = ctx.rect.x + 40
        start_y = ctx.rect.y + self.nav_h + 20
        max_w = ctx.rect.w - 80
        char_w = max(1, ctx.font.size("A")[0])
        
        # Wstrzykujemy funkcję pomiarową opartą o natywne właściwości czcionek
        def measure_text(text, style):
            return self.dom_renderer._font_for(style).size(text)[0]

        engine = LayoutEngine(max_w, char_w, ctx._line_h,
                              image_loader=self.image_loader, base_url=page.url,
                              css_rules=(getattr(page, "_css_all", None)
                                         or getattr(page, "css_rules", [])),
                              measure_fn=measure_text)
        self.layout_boxes, max_y = engine.build(tree, cx, start_y)

        # Zleć pobranie wszystkich obrazów strony (src już absolutny po add_image).
        if self.image_loader is not None:
            for b in self.layout_boxes:
                if b.box_type == "image" and b.meta:
                    src = b.meta.get("src", "")
                    if src:
                        self.image_loader.request(src)
        
        view_h = ctx.rect.h - self.nav_h
        content_h = max_y - start_y
        self.max_scroll = max(0, content_h - view_h + 50)
        # Po przeliczeniu (np. obrazy zmieniły wysokości) utrzymaj scroll w zakresie.
        self.scroll_y = max(0, min(self.scroll_y, self.max_scroll))

    @staticmethod
    def _heat_color(T):
        """Kolor wg temperatury: zimne stalowoniebieskie -> ciepłe bursztynowe
        -> gorące karmazynowe (#8B0000)."""
        t = max(0.0, min(1.0, T / 100.0))
        if t < 0.5:
            f = t / 0.5
            return (int(70 + f * 170), int(80 + f * 90), int(110 - f * 60))
        f = (t - 0.5) / 0.5
        return (int(240 - f * 100), int(170 - f * 150), int(50 - f * 30))

    def _draw_hot_overlay(self, ctx, page):
        """Powierzchnia paradygmatu: panel 'GORĄCE TERAZ' — treść, którą czytelnik
        czytał / na którą patrzył (hover-heat), rankowana temperaturą uwagi.
        Czyta dom_mapper.hot_content; rysowane na wierzchu strony."""
        import pygame
        mapper = getattr(self.browser, "dom_mapper", None)
        if mapper is None or page is None:
            return
        try:
            items = mapper.hot_content(url=getattr(page, "url", None), limit=14)
        except Exception:
            return
        pw = min(470, ctx.rect.w - 32)
        px = ctx.rect.right - pw - 16
        py = ctx.rect.y + self.nav_h + 14
        ph = ctx.rect.bottom - py - 16
        ctx.box(pygame.Rect(px, py, pw, ph), fill=(16, 11, 13),
                outline=(139, 0, 0), radius=6)
        tx, ty = px + 14, py + 10
        ctx.text("\u25e2 GOR\u0104CE TERAZ \u2014 co czyta\u0142e\u015b", (210, 70, 70), x=tx, y=ty)
        ty += self.line_h + 8
        row_h = self.line_h + 6
        sw = 10
        maxw = max(8, (pw - 30 - sw) // self.char_w)
        if not items:
            ctx.text("(pole jeszcze ch\u0142odne \u2014 poczytaj chwil\u0119)",
                     (120, 110, 110), x=tx, y=ty)
            return
        for T, role, text, _u in items:
            if ty + row_h > py + ph - 8:
                break
            ctx.box(pygame.Rect(tx, ty + 3, sw, self.line_h - 4),
                    fill=self._heat_color(T))
            label = f"[{role}] {text}"
            if len(label) > maxw:
                label = label[:maxw - 1] + "\u2026"
            shade = 130 + int(min(1.0, T / 100.0) * 110)
            ctx.text(label, (shade, shade - 25, shade - 25), x=tx + sw + 8, y=ty)
            ty += row_h

    def _draw_all(self, ctx: DrawCtx):
        import pygame
        ctx.clear((12, 12, 18), alpha=215)
        
        page = getattr(self.browser, "_current", None)

        # ── CSS: zewnętrzne arkusze pobierane asynchronicznie (jak obrazy) ──
        if page is not None and self.css_loader is not None:
            if getattr(page, "url", None) != self._css_page_url:
                self._css_page_url = getattr(page, "url", None)
                self.css_loader.reset()
                self.css_loader.request_many(getattr(page, "stylesheet_links", []))
                page._css_all = list(getattr(page, "css_rules", []))   # reguły z <style> od razu
            if self.css_loader.poll():
                # doszły zewnętrzne reguły -> scal i przelicz layout (sprite'y się doresolwują)
                page._css_all = list(getattr(page, "css_rules", [])) + self.css_loader.rules()
                self._images_dirty = True

        nav_rect = pygame.Rect(ctx.rect.x, ctx.rect.y, ctx.rect.w, self.nav_h)
        ctx.box(nav_rect, fill=(24, 24, 38), outline=(50, 50, 75))
        
        self.btn_back_rect = pygame.Rect(ctx.rect.x + 10, ctx.rect.y + 8, 30, 28)
        ctx.box(self.btn_back_rect, fill=(40, 40, 60), outline=(80, 80, 100), radius=4)
        ctx.text("<", (200, 200, 200), x=self.btn_back_rect.x + 10, y=self.btn_back_rect.y + 6)
        
        self.input_url_rect = pygame.Rect(ctx.rect.x + 50, ctx.rect.y + 8, ctx.rect.w - 200, 28)
        input_color = (35, 35, 50) if not self.url_active else (20, 20, 30)
        input_outline = (60, 60, 80) if not self.url_active else (100, 180, 255)
        ctx.box(self.input_url_rect, fill=input_color, outline=input_outline, radius=4)
        
        old_clip = ctx.surface.get_clip()
        ctx.surface.set_clip(self.input_url_rect)
        
        if self.url_active:
            display_text = self.url_text + ("_" if (pygame.time.get_ticks() // 500) % 2 == 0 else "")
            ctx.text(display_text, (255, 255, 255), x=self.input_url_rect.x + 10, y=self.input_url_rect.y + 6)
        else:
            if page:
                ctx.text(f"{page.url}", (180, 180, 180), x=self.input_url_rect.x + 10, y=self.input_url_rect.y + 6)
            else:
                ctx.text("Wpisz adres i wciśnij Enter...", (100, 100, 100), x=self.input_url_rect.x + 10, y=self.input_url_rect.y + 6)

        ctx.surface.set_clip(old_clip)
        ctx.text("ESC=Konsola", (140, 140, 160), x=ctx.rect.right - 130, y=ctx.rect.y + 12)

        if not page:
            ctx.text("Luneta (Karmin-engine) — Płótno gotowe.", (220, 220, 220), x=ctx.rect.x + 40, y=ctx.rect.y + self.nav_h + 40)
            ctx.text("Kliknij pasek adresu na górze i wpisz URL, aby rozpocząć nawigację.", (140, 140, 140), x=ctx.rect.x + 40, y=ctx.rect.y + self.nav_h + 70)
            ctx.text("Wciskając ESC, w każdej chwili powrócisz do konsoli systemowej.", (100, 100, 100), x=ctx.rect.x + 40, y=ctx.rect.y + self.nav_h + 95)
            return

        # ── SZEW B: pompuj pętlę JS co klatkę; mutacje DOM -> przelicz layout ──
        jsb = getattr(self.browser, "js_bridge", None)
        if jsb is not None and getattr(jsb, "_active", False):
            pump = getattr(jsb, "pump_and_sync", None)
            if callable(pump):
                try:
                    if pump():
                        self._js_dirty = True
                except Exception:
                    pass

        # Odbierz świeżo zdekodowane obrazy; jeśli dotyczą bieżącej strony,
        # oznacz layout do przeliczenia (intrinsic -> właściwe wymiary pudełek).
        if self.image_loader is not None:
            changed = self.image_loader.poll()
            if changed:
                srcs = {b.meta.get("src") for b in self.layout_boxes
                        if b.box_type == "image" and b.meta}
                if changed & srcs:
                    self._images_dirty = True

        self._rebuild_layout_if_needed(ctx, page)

        # ── E2: tekstury obecne w layoucie są osiągalne; reszta stygnie i jest
        # żęta przez reach-GC substratu (zwalnia Surface). Robione po przebudowie,
        # gdy layout_boxes jest aktualny dla bieżącej strony.
        if self.image_loader is not None:
            img_srcs = {b.meta.get("src") for b in self.layout_boxes
                        if b.box_type == "image" and b.meta and b.meta.get("src")}
            try:
                self.image_loader.update_reach(img_srcs)
            except Exception:
                pass

        # ── Media termiczne (GIF/wektor/...): pompa czasowych co klatkę + osiągalność
        # mediów obecnych w layoucie (poza layoutem stygną i są żęte przez reach-GC).
        if self.media is not None:
            try:
                rt = getattr(self.browser, "runtime", None)
                if rt is not None:
                    rt.pump_media()
                pairs = []
                for b in self.layout_boxes:
                    if b.box_type != "image" or not b.meta:
                        continue
                    k = b.meta.get("media_kind"); a = b.meta.get("media_aid")
                    if not k and self.image_loader is not None:
                        s = b.meta.get("src")
                        k = self.image_loader.media_kind(s)
                        a = self.image_loader.media_aid(s)
                    if k:
                        pairs.append((k, a))
                self.media.update_reach(pairs)
            except Exception:
                pass
        
        if not self.layout_boxes:
            ctx.text(f"Brak drzewa DOM dla: {page.title}", (255, 100, 100), x=ctx.rect.x + 40, y=ctx.rect.y + self.nav_h + 30)
            return

        clip_rect = pygame.Rect(ctx.rect.x, ctx.rect.y + self.nav_h, ctx.rect.w, ctx.rect.h - self.nav_h)

        # ── HOVER-HEAT: detekcja + aplikacja, granice z realnego clip_rect ──
        mx, my = pygame.mouse.get_pos()
        top_limit = ctx.rect.y + self.nav_h
        self.hovered_box = self._find_hovered_box(mx, my, top_limit, clip_rect.bottom)
        self._apply_hover_heat(self.hovered_box, pygame.time.get_ticks())

        self.dom_renderer.draw(ctx, self.layout_boxes, self.scroll_y, clip_rect,
                               self.hovered_box, self.focused_field, self.image_loader,
                               self.media)

        if self.show_hot:
            self._draw_hot_overlay(ctx, page)


def show_help(term_state):
    help_text = """=== LUNETA — Przeglądarka phi-space v2.3 ===
Wpisz URL, aby przejść do strony.
Komenda 'canvas' lub 'view' otwiera pełnoekranowe płótno graficzne.
W trybie płótna naciśnij ESC lub F1, aby powrócić do konsoli.
Najechanie kursorem na tekst podgrzewa odpowiadający mu atom substratu."""
    term_state.append(help_text, (200, 200, 200))


def main():
    if not PYGAME_OK:
        print("Brak PyGame. Instalacja: pip install pygame")
        sys.exit(1)

    display = KarmazynDisplay()
    if not display.init(title="Luneta (Karmin-engine)"):
        print("Nie udało się zainicjalizować KarmazynDisplay.")
        sys.exit(1)

    runtime = LunetaRuntime()
    browser = LunetaBrowser(runtime)
    
    browser.gui_mode = True
    mapper = attach_to_browser(browser, runtime)
    
    if _HAS_ASYNC and install_async_engine_on_browser(browser):
        print("Silnik async aktywny...")

    display.bind_phi(runtime)
    display.bind_browser(browser)

    viewer = GraphicPageViewer(browser, display.renderer.release_fullscreen, mapper=mapper)

    # NATYCHMIASTOWE WYMUSZENIE WIDOKU GRAFICZNEGO PRZY STARCIE
    display.renderer.claim_fullscreen(viewer)

    def thread_shell_main(term_state):
        term_state.prompt = "LUNETA> "
        term_state.append("LUNETA gotowa. Wpisz URL lub 'canvas'.", (255, 255, 255))
        
        while True:
            try:
                cmd_in = term_state.get_input_blocking().strip()
                if not cmd_in:
                    continue
                
                parts = cmd_in.split()
                cmd_upper = parts[0].upper()

                if cmd_upper in ("HELP", "H", "?"):
                    show_help(term_state)
                elif cmd_upper in ("EXIT", "QUIT", "Q"):
                    break
                elif cmd_upper in ("CANVAS", "VIEW"):
                    display.renderer.claim_fullscreen(viewer)
                elif cmd_upper == "DOM":
                    msg = cmd_dom(parts[1:], browser, mapper)
                    term_state.append(strip_ansi(msg), (200, 200, 200))
                elif cmd_upper == "JS":
                    if getattr(browser, "_has_js", False) and browser.js_bridge:
                        async_out = cmd_async(parts[1:], browser.js_bridge)
                        if async_out is not None:
                            term_state.append(strip_ansi(async_out), (200, 200, 200))
                        else:
                            from karmazyn_js_web import cmd_js_bridge
                            msg = cmd_js_bridge(parts[1:], browser.js_bridge)
                            term_state.append(strip_ansi(msg), (200, 200, 200))
                    else:
                        term_state.append("Silnik JS niedostępny.", (255, 50, 50))
                else:
                    ok, msg = browser.go(cmd_in)
                    if msg:
                        term_state.append(strip_ansi(msg), (200, 200, 200))
                    if ok:
                        display.renderer.claim_fullscreen(viewer)

            except Exception as e:
                term_state.append(f"Błąd krytyczny REPL: {e}", (255, 50, 50))

        term_state.shutdown()

    display.run(shell_main=thread_shell_main)


if __name__ == "__main__":
    main()