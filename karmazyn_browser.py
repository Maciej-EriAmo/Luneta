"""
karmazyn_browser.py — Luneta: Przeglądarka KarmazynOS v4.5
===========================================================
Przeglądarka tekstowa zintegrowana z phi-space.

Filozofia:
  Luneta nie renderuje pikseli — renderuje znaczenie.
  Każda odwiedzona strona staje się atomem phi-space.
  Temperatura atomu rośnie przy każdej wizycie.
  Historia przeglądania = termodynamika dostępu.

Izomorfizm phi-space:
  URL      ≡ atom.id      (adres)
  Treść    ≡ atom.E       (emanacja)
  Wizyty   ≡ atom.T       (temperatura = częstość)

Skróty (vi-style):
  j/k     — scroll w dół/górę
  b/h     — wstecz
  N       — podążaj za linkiem N
  o/u     — pokaż aktualny URL
  r       — przeładuj

Komendy:
  LUNETA <url>           — otwórz stronę
  LUNETA FOLLOW <n>      — podążaj za linkiem n
  LUNETA BACK            — wstecz
  LUNETA LINKS           — lista linków
"""

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
karmazyn_browser.py — Tekstowa Przegladarka HTTP KarmazynOS v4.5
================================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Bazuje na v4.2 (semantic tree, ANSI-aware, tabele, redirect, gzip).
Nowe w v4.3:
  - Integracja DOMMapper: każda strona automatycznie mapowana do phi-space
  - Subkomendy DOM w cmd_browse (DOM MAP/OUTLINE/READER/FIND/PHI/STATS)
  - fix: forward() używa add_to_history=False (był ping-pong z back())
  - fix: _update_atom_temp używa "WARM"/"COLD" zamiast "stygnie"
  - fix: ANSIState.apply() wywołane przy łamaniu linii (kontynuacja stylu)

Architektura:
  SemanticHTMLParser → SemanticNode tree
  ANSIRenderer       → List[TextChunk]  (surowe bloki z flagą preformatted)
  ParsedPage.lines() → zawijanie ANSI-aware z cache
  DOMMapper          → phi-space (atomy/bąble/hologramy) z temperatury semantycznej
"""

import gzip
import html
import json
import os
import re
import sys
import time
import urllib.parse
from collections import deque, OrderedDict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

# ── Stałe ────────────────────────────────────────────────────────────────────

MAX_PAGE_SIZE   = 5 * 1024 * 1024
BOOKMARKS_LABEL = "browser_bookmarks"
HISTORY_LABEL   = "browser_history"
CACHE_DIR       = ".bubbles/store/browser_cache"
DISPLAY_WIDTH   = 80
PAGE_SIZE       = 40
CACHE_MAX_SIZE  = 100

COLORS = {
    'reset':   '\033[0m',
    'bold':    '\033[1m',
    'red':     '\033[91m',
    'green':   '\033[92m',
    'yellow':  '\033[93m',
    'blue':    '\033[94m',
    'magenta': '\033[95m',
    'cyan':    '\033[96m',
    'gray':    '\033[90m',
}

# ── CJK / ANSI helpers ────────────────────────────────────────────────────────

try:
    import wcwidth
    HAS_WCWIDTH = True
except ImportError:
    HAS_WCWIDTH = False

ANSI_RE          = re.compile(r'\x1b\[[0-9;]*m')
DANGEROUS_ANSI_RE = re.compile(
    r'\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\|\x1b[PX^_]'
)


def sanitize_ansi(text: str) -> str:
    return DANGEROUS_ANSI_RE.sub('', text)


def visible_len(s: str) -> int:
    stripped = ANSI_RE.sub('', s)
    if HAS_WCWIDTH:
        total = 0
        for ch in stripped:
            w = wcwidth.wcwidth(ch)
            total += max(w, 0)
        return total
    return len(stripped)


class ANSIState:
    """Stan parsera ANSI — śledzi aktywne sekwencje escape."""
    def __init__(self):
        self.codes: List[str] = []

    def apply(self, text: str) -> str:
        """Poprzedza tekst aktywnym stanem ANSI — używane przy łamaniu linii."""
        if not self.codes:
            return text
        prefix = '\033[' + ';'.join(self.codes) + 'm'
        return prefix + text

    def update(self, seq: str):
        m = re.match(r'\033\[([0-9;]*)m', seq)
        if m:
            codes = m.group(1).split(';') if m.group(1) else ['0']
            if '0' in codes:
                self.codes = []
            else:
                for code in codes:
                    if code not in self.codes:
                        self.codes.append(code)


def ansi_wrap(text: str, width: int) -> List[str]:
    """
    Zawija tekst z zachowaniem stylów ANSI i poprawnym liczeniem znaków CJK.
    BUG FIX v4.3: state.apply() wywołane przy łamaniu linii — styl
    kontynuowany po złamaniu (wcześniej ginął po pierwszym złamaniu).
    """
    parts = []
    pos = 0
    for m in ANSI_RE.finditer(text):
        if m.start() > pos:
            parts.append(('text', text[pos:m.start()]))
        parts.append(('ansi', m.group(0)))
        pos = m.end()
    if pos < len(text):
        parts.append(('text', text[pos:]))

    lines: List[str] = []
    current_line     = ''
    current_visible  = 0
    state            = ANSIState()

    for typ, chunk in parts:
        if typ == 'ansi':
            state.update(chunk)
            current_line += chunk
            continue

        tokens = re.split(r'(\s+)', chunk)
        for token in tokens:
            if not token:
                continue
            token_visible = visible_len(token)
            if token.isspace():
                current_line    += token
                current_visible += token_visible
                continue
            if current_visible + token_visible > width and current_visible > 0:
                lines.append(current_line)
                # BUG FIX v4.3: odtwórz aktywny styl ANSI na początku nowej linii
                current_line    = state.apply('')
                current_visible = 0
            current_line    += token
            current_visible += token_visible

    if current_line or current_visible > 0:
        lines.append(current_line)
    return lines if lines else [text]


# ── HTTP fallback ─────────────────────────────────────────────────────────────

try:
    from karmazyn_net import http_get, HttpResponse
    HAS_NET = True
except ImportError:
    HAS_NET = False
    import urllib.request
    import urllib.error
    import socket

    @dataclass
    class HttpResponse:
        url:          str
        status:       int
        content_type: str
        body:         bytes
        headers:      dict
        elapsed_ms:   float
        truncated:    bool = False

        @property
        def text(self) -> str:
            body = self.body
            ce   = self.headers.get('content-encoding', '').lower()
            if ce == 'gzip':
                try:
                    body = gzip.decompress(body)
                except Exception:
                    pass
            elif ce == 'deflate':
                try:
                    import zlib
                    body = zlib.decompress(body)
                except Exception:
                    pass
            charset = None
            ct = self.headers.get('content-type', '').lower()
            if 'charset=' in ct:
                charset = ct.split('charset=')[-1].split(';')[0].strip()
            for enc in (charset, 'utf-8', 'utf-8-sig', 'latin-1'):
                if enc:
                    try:
                        return body.decode(enc)
                    except (UnicodeDecodeError, LookupError):
                        continue
            return body.decode('utf-8', errors='replace')

        def ok(self) -> bool:
            return 200 <= self.status < 300

    def http_get(url, headers=None, timeout=15.0):
        hdrs = {
            'User-Agent':      'KarmazynBrowser/4.3',
            'Accept-Encoding': 'gzip, deflate',
            **(headers or {}),
        }
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data      = bytearray()
                truncated = False
                while True:
                    chunk = r.read(8192)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_PAGE_SIZE:
                        data      = data[:MAX_PAGE_SIZE]
                        truncated = True
                        break
                return HttpResponse(
                    url=url, status=r.status,
                    content_type=r.headers.get('Content-Type', ''),
                    body=bytes(data), headers=dict(r.headers),
                    elapsed_ms=0, truncated=truncated,
                )
        except socket.timeout:
            return HttpResponse(url=url, status=0, content_type='',
                                body=b'Timeout', headers={},
                                elapsed_ms=timeout * 1000)
        except Exception as e:
            return HttpResponse(url=url, status=0, content_type='',
                                body=str(e).encode(), headers={},
                                elapsed_ms=0)


# ── Semantyczny model drzewa ──────────────────────────────────────────────────

class NodeType:
    """Typy węzłów semantycznych DOM."""
    DOCUMENT = 0
    BLOCK    = 1
    INLINE   = 2
    HEADING  = 3
    LINK     = 4
    LIST     = 5
    TABLE    = 6
    PRE      = 7
    TEXT     = 8
    HR       = 9


@dataclass
class SemanticNode:
    """Węzeł semantyczny — odpowiednik elementu DOM z typem i treścią."""
    typ:      int
    tag:      str                     = ''
    attrs:    dict                    = field(default_factory=dict)
    text:     str                     = ''
    children: List['SemanticNode']   = field(default_factory=list)

    def get_plain_text(self) -> str:
        if self.typ == NodeType.TEXT:
            return self.text or ''
        return ''.join(c.get_plain_text() for c in self.children)


@dataclass
class TextChunk:
    """Fragment tekstu z atrybutami wizualnymi (kolor, bold)."""
    text:         str
    preformatted: bool = False
    style:        str  = ''


# ── ParsedPage ────────────────────────────────────────────────────────────────

@dataclass
class ParsedPage:
    """Sparsowana strona — tytuł, linki, nagłówki, treść."""
    title:         str
    chunks:        List[TextChunk]
    links:         List[Tuple[str, str]]
    headings:      List[str]
    raw_html:      str
    url:           str
    width:         int                    = DISPLAY_WIDTH
    semantic_tree: Optional[SemanticNode] = field(default=None, repr=False)
    truncated:     bool                   = False
    _lines_cache:  Optional[List[str]]    = field(default=None, repr=False)

    def lines(self) -> List[str]:
        """Zwraca linie tekstu (zawijanie ANSI-aware) z cache."""
        if self._lines_cache is not None:
            return self._lines_cache

        # Krok 1: surowe linie z podziałem na \n
        raw_lines: List[Tuple[str, bool]] = []
        current   = ''
        for chunk in self.chunks:
            if chunk.preformatted:
                if current:
                    raw_lines.append((current, False))
                    current = ''
                for pline in chunk.text.splitlines():
                    raw_lines.append((pline, True))
            else:
                parts = chunk.text.split('\n')
                current += parts[0]
                for p in parts[1:]:
                    raw_lines.append((current, False))
                    current = p
        if current:
            raw_lines.append((current, False))

        # Krok 2: zawijanie
        wrapped: List[str] = []
        for line, is_pre in raw_lines:
            if is_pre:
                wrapped.append(line)
            elif visible_len(line) <= self.width:
                wrapped.append(line)
            else:
                wrapped.extend(ansi_wrap(line, self.width))

        # Krok 3: max 2 puste linie z rzędu
        cleaned: List[str] = []
        blanks = 0
        for line in wrapped:
            if line == '':
                blanks += 1
                if blanks <= 2:
                    cleaned.append('')
            else:
                blanks = 0
                cleaned.append(line)

        self._lines_cache = cleaned
        return cleaned


# ── Parser semantyczny ────────────────────────────────────────────────────────

class SemanticHTMLParser(HTMLParser):

    def __init__(self, base_url: str = '', width: int = DISPLAY_WIDTH):
        super().__init__()
        self.base_url   = base_url
        self.width      = width
        self.root       = SemanticNode(NodeType.DOCUMENT, 'document')
        self.node_stack: List[SemanticNode] = [self.root]
        self.skip_depth = 0
        self.pre_depth  = 0
        self.in_title   = False
        self.title      = ''

    def _is_hidden(self, attrs: dict) -> bool:
        if 'hidden' in attrs:
            return True
        if attrs.get('aria-hidden', '').lower() == 'true':
            return True
        if 'display:none' in attrs.get('style', '').replace(' ', ''):
            return True
        return False

    def handle_starttag(self, tag, attrs):
        tag        = tag.lower()
        attrs_dict = dict(attrs)

        if tag in {'script', 'style', 'noscript'}:
            self.skip_depth += 1
            return
        if self.skip_depth > 0:
            return
        if tag == 'title':
            self.in_title = True
            return
        if tag in {'pre', 'code'}:
            self.pre_depth += 1
        if self._is_hidden(attrs_dict):
            self.skip_depth += 1
            return

        node = SemanticNode(NodeType.INLINE, tag, attrs_dict)
        if tag in {'p','div','article','section','main','header','footer',
                   'nav','aside','br','li','tr','td','th','tbody','thead'}:
            node.typ = NodeType.BLOCK
        elif tag in {'h1','h2','h3','h4','h5','h6'}:
            node.typ = NodeType.HEADING
        elif tag == 'a':
            node.typ = NodeType.LINK
        elif tag in {'ul', 'ol'}:
            node.typ = NodeType.LIST
        elif tag == 'table':
            node.typ = NodeType.TABLE
        elif tag in {'pre', 'code'}:
            node.typ = NodeType.PRE
        elif tag == 'hr':
            node.typ = NodeType.HR

        self.node_stack[-1].children.append(node)
        if tag not in {'br','hr','img','meta','link','input'}:
            self.node_stack.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {'script', 'style', 'noscript'}:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth > 0:
            return
        if tag == 'title':
            self.in_title = False
            return
        # BUG FIX v4.3: pre_depth dekrementowany tylko raz przy zamknięciu
        # konkretnego tagu, nie przez scan stacku (błąd przy <pre><code>)
        if tag in {'pre', 'code'}:
            self.pre_depth = max(0, self.pre_depth - 1)
        for i in range(len(self.node_stack) - 1, 0, -1):
            if self.node_stack[i].tag == tag:
                self.node_stack = self.node_stack[:i]
                break

    def handle_data(self, data):
        if self.skip_depth > 0:
            return
        data = sanitize_ansi(data)
        if self.in_title:
            self.title += data
            return
        if self.pre_depth > 0:
            node = SemanticNode(NodeType.TEXT, text=data)
        else:
            clean = re.sub(r'\s+', ' ', data)
            if not clean:
                return
            node = SemanticNode(NodeType.TEXT, text=clean)
        self.node_stack[-1].children.append(node)

    def handle_entityref(self, name):
        self.handle_data(html.unescape(f'&{name};'))

    def handle_charref(self, name):
        self.handle_data(html.unescape(f'&#{name};'))


# ── Renderer ANSI ─────────────────────────────────────────────────────────────

class ANSIRenderer:
    """Renderer drzewa semantycznego → tekst z kolorami ANSI."""
    def __init__(self, base_url: str, width: int):
        self.base_url = base_url
        self.width    = width
        self.chunks:   List[TextChunk]       = []
        self.links:    List[Tuple[str, str]] = []
        self.headings: List[str]             = []

    def add_text(self, text: str, preformatted: bool = False):
        if text:
            self.chunks.append(TextChunk(text=text, preformatted=preformatted))

    def render(self, root: SemanticNode):
        self._walk(root)

    def _walk(self, node: SemanticNode):
        if node.typ == NodeType.TEXT:
            self.add_text(node.text)
            return

        if node.typ == NodeType.HR:
            self.add_text('\n' + '─' * self.width + '\n')
            return

        if node.typ == NodeType.PRE:
            self.add_text(node.get_plain_text(), preformatted=True)
            return

        if node.typ == NodeType.TABLE:
            self._render_table(node)
            return

        if node.typ == NodeType.HEADING:
            level = int(node.tag[1]) if len(node.tag) > 1 and node.tag[1].isdigit() else 1
            text  = node.get_plain_text().strip()
            if text:
                self.headings.append(text)
                prefix = '#' * level + ' '
                line   = prefix + text
                color  = COLORS['yellow'] if level == 1 else COLORS['green']
                sep    = ('═' if level == 1 else '─') * min(visible_len(line), self.width)
                self.add_text(f'\n{color}{line}{COLORS["reset"]}\n{sep}\n')
            return

        if node.typ == NodeType.LIST:
            self.add_text('\n')
            is_ol    = node.tag == 'ol'
            li_count = 0
            for c in node.children:
                if c.tag == 'li':
                    li_count += 1
                    marker = f'{li_count}. ' if is_ol else '  • '
                    self.add_text(marker)
                    self._walk(c)
                    self.add_text('\n')
                else:
                    self._walk(c)
            self.add_text('\n')
            return

        if node.typ == NodeType.LINK:
            url = _resolve_url(node.attrs.get('href', ''), self.base_url)
            if url:
                idx       = len(self.links) + 1
                link_text = node.get_plain_text().strip() or url
                self.links.append((url, link_text))
                for c in node.children:
                    self._walk(c)
                self.add_text(f'{COLORS["blue"]} [{idx}]{COLORS["reset"]}')
            else:
                for c in node.children:
                    self._walk(c)
            return

        is_block = (node.typ == NodeType.BLOCK
                    and node.tag not in ('li','tr','td','th','tbody','thead'))
        if is_block:
            self.add_text('\n')
        if node.typ == NodeType.INLINE:
            if node.tag in ('strong', 'b'):
                self.add_text(COLORS['bold'])
            elif node.tag in ('em', 'i'):
                self.add_text(COLORS['cyan'])
        if node.tag == 'br':
            self.add_text('\n')

        for c in node.children:
            self._walk(c)

        if node.typ == NodeType.INLINE:
            if node.tag in ('strong', 'b', 'em', 'i'):
                self.add_text(COLORS['reset'])
        if is_block:
            self.add_text('\n')

    def _render_table(self, table_node: SemanticNode):
        rows: List[SemanticNode] = []

        def _find_rows(n: SemanticNode):
            if n.tag == 'tr':
                rows.append(n)
            else:
                for child in n.children:
                    _find_rows(child)

        _find_rows(table_node)
        table_data = []
        for row in rows:
            cells = []
            for c in row.children:
                if c.tag in ('td', 'th'):
                    colspan   = int(c.attrs.get('colspan', 1))
                    cell_text = ANSI_RE.sub('', c.get_plain_text()).strip()
                    cells.append((cell_text, colspan))
            if cells:
                table_data.append(cells)

        if not table_data:
            return

        max_cols  = max(sum(cs for _, cs in row) for row in table_data)
        col_widths = [0] * max_cols
        for row in table_data:
            ci = 0
            for text, cs in row:
                w = visible_len(text) + 2
                for i in range(cs):
                    if ci + i < max_cols:
                        col_widths[ci + i] = max(col_widths[ci + i], w)
                ci += cs

        total = sum(col_widths) + max_cols - 1
        if total > self.width:
            factor     = (self.width - max_cols + 1) / total
            col_widths = [max(3, int(w * factor)) for w in col_widths]

        sep = '+' + '+'.join('-' * w for w in col_widths) + '+'
        self.add_text('\n' + sep + '\n')

        for row in table_data:
            ci      = 0
            cells_r = []
            for text, cs in row:
                w       = sum(col_widths[ci:ci + cs])
                vl      = visible_len(text)
                if vl > w:
                    text    = text[:w - 1] + '…'
                    padding = 0
                else:
                    padding = w - vl
                cells_r.append(text + ' ' * padding)
                ci += cs
            while len(cells_r) < max_cols:
                cells_r.append(' ' * col_widths[len(cells_r)])
            self.add_text('|' + '|'.join(cells_r) + '|\n')

        self.add_text(sep + '\n')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_url(href: str, base: str) -> str:
    if not href or href.startswith(('javascript:', 'mailto:', '#')):
        return ''
    try:
        return urllib.parse.urljoin(base, href)
    except Exception:
        return href


def parse_html(html_text: str, url: str = '',
               width: int = DISPLAY_WIDTH,
               truncated: bool = False) -> ParsedPage:
    parser = SemanticHTMLParser(base_url=url, width=width)
    try:
        parser.feed(html_text)
    except Exception as e:
        _log_error(f'parse_html: {e}')

    renderer = ANSIRenderer(url, width)
    renderer.render(parser.root)

    return ParsedPage(
        title         = parser.title.strip() or url,
        chunks        = renderer.chunks,
        links         = renderer.links,
        headings      = renderer.headings,
        raw_html      = html_text,
        url           = url,
        width         = width,
        semantic_tree = parser.root,
        truncated     = truncated,
        _lines_cache  = None,
    )


def _log_error(msg: str) -> None:
    try:
        print(f'[Browser Error] {msg}', file=sys.stderr)
    except Exception:
        pass


# ── Główna klasa przeglądarki ─────────────────────────────────────────────────

class KarmazynBrowser:
    """Przeglądarka phi-space — stan sesji, historia, cache."""
    T_FRESH  = 80.0
    T_CACHED = 45.0
    T_STALE  = 20.0

    def __init__(self, runtime, width: int = DISPLAY_WIDTH):
        self.runtime  = runtime
        self.width    = width
        self._history = deque(maxlen=50)
        self._forward = deque(maxlen=50)
        self._current: Optional[ParsedPage] = None
        self._scroll  = 0
        self._cache: OrderedDict[str, Tuple[ParsedPage, float]] = OrderedDict()
        self._bookmarks: Dict[str, str] = {}

        os.makedirs(CACHE_DIR, exist_ok=True)
        self._load_bookmarks()

        # DOMMapper — opcjonalny, dołączany jeśli karmazyn_dom dostępny
        try:
            from karmazyn_dom import DOMMapper
            self.dom_mapper = DOMMapper(runtime)
            self._has_dom   = True
        except ImportError:
            self.dom_mapper = None
            self._has_dom   = False

        # JSBridge — silnik JS dla stron (opcjonalny)
        try:
            from karmazyn_js_web import JSBridge
            self.js_bridge = JSBridge(runtime)
            self._has_js   = True
        except ImportError:
            self.js_bridge = None
            self._has_js   = False

    # ── Cache LRU ─────────────────────────────────────────────────────────────

    def _cache_put(self, url: str, page: ParsedPage):
        if len(self._cache) >= CACHE_MAX_SIZE:
            self._cache.popitem(last=False)
        self._cache[url] = (page, time.time())
        self._cache.move_to_end(url)

    def _cache_get(self, url: str) -> Optional[Tuple[ParsedPage, float]]:
        if url in self._cache:
            self._cache.move_to_end(url)
            return self._cache[url]
        return None

    # ── Atom phi-space ────────────────────────────────────────────────────────

    def _update_atom_temp(self, url: str, temp: float):
        """
        BUG FIX v4.3: poprawne stany termodynamiczne.
        Poprzednio: state = "stygnie" — nie istnieje w modelu KarmazynOS.
        Teraz: "HOT"/"WARM"/"COLD" zgodnie z modelem.
        """
        try:
            label = ('www_' + re.sub(r'[^a-z0-9]', '_',
                     url.lower().replace('https://', '').replace('http://', ''))[:20])
            if self.runtime.matrix.has_atom(label):
                atom = self.runtime.get_atom(label)
                if atom:
                    # heat/cool zamiast ręcznego T i state (karmazyn_atom unified model)
                    if temp > atom.T: atom.heat(temp - atom.T)
                    else:             atom.cool(atom.T - temp)
            else:
                self.runtime.create_atom(label, url[:64], url, temp)
                # state aktualizowany przez Atom.__init__ → state_for_T(T)
                atom = self.runtime.get_atom(label)
                if atom:
                    atom.heat(0)  # wymusza _update_state() bez zmiany T
        except Exception as e:
            _log_error(f'Atom update: {e}')

    # ── Ładowanie stron ───────────────────────────────────────────────────────

    def _load_url(self, url: str,
                  add_to_history: bool = True,
                  force_reload: bool   = False) -> Tuple[bool, str]:
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        current_url  = url
        max_redirects = 5
        resp          = None

        for _ in range(max_redirects):
            if not force_reload:
                cached = self._cache_get(current_url)
                if cached:
                    page, ts = cached
                    if time.time() - ts < 300:
                        if add_to_history and self._current:
                            self._add_to_history(self._current.url)
                            self._forward.clear()
                        self._current = page
                        self._scroll  = 0
                        self._clamp_scroll()
                        self._update_atom_temp(current_url, self.T_CACHED)
                        self._map_dom(page)
                        return True, self._render_current()

            resp = http_get(current_url,
                            headers={'User-Agent': 'KarmazynBrowser/4.3'})
            if resp.status in (301, 302, 303, 307, 308):
                location = (resp.headers.get('Location')
                            or resp.headers.get('location'))
                if location:
                    next_url = _resolve_url(location, current_url)
                    if next_url and next_url != current_url:
                        current_url = next_url
                        continue
            break

        if resp is None or not resp.ok():
            status = resp.status if resp else 0
            return False, f'HTTP {status}: {current_url}'

        ct = resp.content_type.lower()
        if 'html' not in ct and 'text' not in ct:
            return False, f'Nieobsługiwany typ: {resp.content_type}\nURL: {current_url}'

        page = parse_html(resp.text, url=current_url, width=self.width,
                          truncated=getattr(resp, 'truncated', False))
        self._cache_put(current_url, page)
        self._update_atom_temp(current_url, self.T_FRESH)

        if add_to_history and self._current:
            self._add_to_history(self._current.url)
            self._forward.clear()

        self._current = page
        self._scroll  = 0
        self._clamp_scroll()

        # DOMMapper: mapuj phi-space po każdym załadowaniu
        self._map_dom(page)

        return True, self._render_current()

    def _map_dom(self, page: ParsedPage) -> None:
        """Mapuje stronę do phi-space i dołącza JSBridge."""
        if self._has_dom and self.dom_mapper is not None:
            try:
                self.dom_mapper.map_page(page)
            except Exception as e:
                _log_error(f'DOM map: {e}')
        # JSBridge — buduje LiveDOM i uruchamia skrypty
        if self._has_js and self.js_bridge is not None:
            try:
                self.js_bridge.attach(page)
                self.js_bridge.run_scripts(page)
            except Exception as e:
                _log_error(f'JS bridge: {e}')

    def _add_to_history(self, url: str):
        if self._history and self._history[-1] == url:
            return
        self._history.append(url)

    def _clamp_scroll(self):
        if self._current is None:
            self._scroll = 0
            return
        total        = len(self._current.lines())
        max_scroll   = max(0, total - PAGE_SIZE)
        self._scroll = max(0, min(self._scroll, max_scroll))

    # ── Nawigacja ─────────────────────────────────────────────────────────────

    def go(self, url: str, force_reload: bool = False) -> Tuple[bool, str]:
        return self._load_url(url, add_to_history=True, force_reload=force_reload)

    def back(self) -> Tuple[bool, str]:
        if not self._history:
            return False, 'Brak historii.'
        if self._current:
            self._forward.appendleft(self._current.url)
        prev = self._history.pop()
        return self._load_url(prev, add_to_history=False)

    def forward(self) -> Tuple[bool, str]:
        """
        BUG FIX v4.3: add_to_history=False powstrzymuje ping-pong.
        BUG FIX v4.4: przed _load_url ręcznie odkładamy _current na _history.
        Bez tego: A→B, back→A, forward→B, back→? (A wyparowało ze stosu).
        _load_url z add_to_history=False nie czyści _forward (poprawne),
        ale też nie odkłada current — musimy to zrobić sami tutaj.
        """
        if not self._forward:
            return False, 'Brak stron do przodu.'
        if self._current:
            self._add_to_history(self._current.url)
        nxt = self._forward.popleft()
        return self._load_url(nxt, add_to_history=False)

    def reload(self) -> Tuple[bool, str]:
        if not self._current:
            return False, 'Brak strony.'
        url = self._current.url
        self._cache.pop(url, None)
        return self._load_url(url, add_to_history=False, force_reload=True)

    def follow_link(self, n: int) -> Tuple[bool, str]:
        if not self._current:
            return False, 'Brak strony.'
        if not self._current.links:
            return False, 'Brak linków.'
        if n < 1 or n > len(self._current.links):
            return False, f'Link {n} nie istnieje (1-{len(self._current.links)}).'
        url, _ = self._current.links[n - 1]
        if not url:
            return False, 'Pusty link.'
        return self.go(url)

    # ── Renderowanie ──────────────────────────────────────────────────────────

    def _render_current(self) -> str:
        if self._current is None:
            return 'Brak strony.'
        self._clamp_scroll()
        lines = self._current.lines()
        total = len(lines)

        url_short = self._current.url[:self.width - 10]
        header    = [
            COLORS['gray'] + '─' * self.width + COLORS['reset'],
            COLORS['bold'] + f'  {self._current.title[:self.width - 4]}' + COLORS['reset'],
            COLORS['cyan'] + f'  {url_short}' + COLORS['reset'],
            f'  Linki: {len(self._current.links)}  |  Linie: {total}',
            COLORS['gray'] + '─' * self.width + COLORS['reset'],
        ]
        if self._current.truncated:
            header.insert(3, COLORS['red'] + '  [STRONA PRZYCIĘTA – LIMIT 5 MB]'
                          + COLORS['reset'])
        # Wskaźnik phi-space (DOMMapper + JSBridge)
        if self._has_dom and self._current.url in getattr(self.dom_mapper, '_page_atoms', {}):
            n_atoms = len(self.dom_mapper._page_atoms[self._current.url])
            js_info = ""
            if self._has_js and self.js_bridge and self.js_bridge._active:
                s = self.js_bridge.status()
                js_info = f"  JS:{s.get('atoms',0)}at"
            header.insert(4, COLORS['gray'] + f'  φ: {n_atoms} atomów{js_info}' + COLORS['reset'])

        page_lines = lines[self._scroll:self._scroll + PAGE_SIZE]
        remaining  = max(0, total - self._scroll - PAGE_SIZE)
        footer     = COLORS['gray'] + '─' * self.width + COLORS['reset'] + '\n'
        footer    += f'[{self._scroll + 1}-{min(self._scroll + PAGE_SIZE, total)}/{total}]'
        if remaining:
            footer += f'  BROWSE SCROLL 1 aby kontynuować ({remaining} linii)'
        return '\n'.join(header + page_lines + [footer])

    def scroll(self, pages: int = 1) -> str:
        if not self._current:
            return 'Brak strony.'
        total        = len(self._current.lines())
        self._scroll += pages * PAGE_SIZE
        self._clamp_scroll()
        return self._render_current()

    def find(self, query: str) -> str:
        if not self._current:
            return 'Brak strony.'
        lines   = self._current.lines()
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        hits    = []
        for i, line in enumerate(lines, 1):
            stripped = ANSI_RE.sub('', line)
            if pattern.search(stripped):
                highlighted = pattern.sub(
                    lambda m: COLORS['red'] + m.group(0) + COLORS['reset'],
                    stripped,
                )
                hits.append((i, highlighted))
        if not hits:
            return f"Nie znaleziono: '{query}'"
        result = [COLORS['yellow'] + f"Znaleziono '{query}' ({len(hits)} wystąpień):"
                  + COLORS['reset']]
        for lineno, line in hits[:15]:
            result.append(f'  [{lineno:4}] {line[:self.width - 10]}')
        if len(hits) > 15:
            result.append(f'  ... i {len(hits)-15} więcej')
        return '\n'.join(result)

    def show_links(self) -> str:
        if not self._current:
            return 'Brak strony.'
        if not self._current.links:
            return 'Brak linków.'
        lines = [COLORS['bold'] + f'Linki ({len(self._current.links)}):' + COLORS['reset']]
        for i, (url, text) in enumerate(self._current.links, 1):
            url_s  = url[:50] if url else '(pusty)'
            text_s = text[:30]
            lines.append(
                f'  {COLORS["blue"]}[{i:3}]{COLORS["reset"]} '
                f'{text_s:<30} {COLORS["cyan"]}{url_s}{COLORS["reset"]}'
            )
        return '\n'.join(lines)

    def show_source(self, n: int = 50) -> str:
        if not self._current:
            return 'Brak strony.'
        return '\n'.join(self._current.raw_html.splitlines()[:n])

    # ── Zakładki ──────────────────────────────────────────────────────────────

    def _load_bookmarks(self):
        path = os.path.join(CACHE_DIR, 'bookmarks.json')
        if os.path.exists(path):
            try:
                with open(path, encoding='utf-8') as f:
                    self._bookmarks = json.load(f)
            except Exception as e:
                _log_error(f'Load bookmarks: {e}')
                self._bookmarks = {}

    def _save_bookmarks(self):
        path = os.path.join(CACHE_DIR, 'bookmarks.json')
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self._bookmarks, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log_error(f'Save bookmarks: {e}')

    def add_bookmark(self) -> str:
        if not self._current:
            return 'Brak strony.'
        url   = self._current.url
        title = self._current.title
        self._bookmarks[url] = title
        self._save_bookmarks()
        label = 'bm_' + re.sub(r'[^a-z0-9]', '_', url.lower())[:20]
        try:
            if not self.runtime.matrix.has_atom(label):
                self.runtime.create_atom(label, title[:64], url, self.T_CACHED)
        except Exception as e:
            _log_error(f'Bookmark atom: {e}')
        return f'Dodano zakładkę: {title}'

    def list_bookmarks(self) -> str:
        if not self._bookmarks:
            return 'Brak zakładek.'
        lines = [COLORS['bold'] + f'Zakładki ({len(self._bookmarks)}):' + COLORS['reset']]
        for i, (url, title) in enumerate(self._bookmarks.items(), 1):
            lines.append(
                f'  {COLORS["green"]}[{i:3}]{COLORS["reset"]} '
                f'{title[:35]:<35} {COLORS["cyan"]}{url[:45]}{COLORS["reset"]}'
            )
        return '\n'.join(lines)

    def go_bookmark(self, n: int) -> Tuple[bool, str]:
        urls = list(self._bookmarks.keys())
        if n < 1 or n > len(urls):
            return False, f'Zakładka {n} nie istnieje (1-{len(urls)}).'
        return self.go(urls[n - 1])

    # ── Historia ──────────────────────────────────────────────────────────────

    def show_history(self) -> str:
        if not self._history:
            return 'Historia pusta.'
        lines = [COLORS['bold'] + f'Historia ({len(self._history)}):' + COLORS['reset']]
        for url in reversed(self._history):
            cached = url in self._cache
            mark   = COLORS['green'] + '✓' + COLORS['reset'] if cached else ' '
            lines.append(f'  [{mark}] {url[:self.width - 8]}')
        return '\n'.join(lines)


# ── Komenda shella ────────────────────────────────────────────────────────────

# ─── Publiczny interfejs dla programs.json (type=object) ─────────────────────

class _BrowserCmd:
    """Singleton-wrapper: programs.json ładuje KarmazynBrowser.cmd jako metodę."""
    def __init__(self, runtime):
        self._browser = KarmazynBrowser(runtime)

    def cmd(self, args) -> str:
        return cmd_browse(args, self._browser)

# alias dla programs.json: class=KarmazynBrowser → używa cmd()
KarmazynBrowser.cmd = lambda self, args: cmd_browse(args, self)


def cmd_browse(args, browser: KarmazynBrowser) -> str:
    """
    BROWSE <url>            — otwórz stronę
    BROWSE BACK             — wstecz
    BROWSE FORWARD / FWD   — naprzód
    BROWSE RELOAD           — odśwież
    BROWSE LINKS            — lista linków
    BROWSE FOLLOW <n>       — podążaj za linkiem nr n
    BROWSE FIND <tekst>     — szukaj na stronie (tekst)
    BROWSE SCROLL [n]       — przewiń o n stron (domyślnie 1)
    BROWSE SOURCE [n]       — źródło HTML (pierwsze n linii)
    BROWSE BM               — dodaj zakładkę
    BROWSE BOOKMARKS        — lista zakładek
    BROWSE GOTO <n>         — idź do zakładki nr n
    BROWSE HISTORY          — historia
    BROWSE SAVE [label]     — zapisz stronę jako atom
    BROWSE DOM [subkomenda] — operacje phi-space na DOM (patrz: DOM ?)
    BROWSE JS [subkomenda]  — silnik JS [STATUS|THERMAL|TICK|DOM|RUN]
    """
    if not args:
        return browser._render_current() if browser._current else 'BROWSE <url>'

    sub = args[0].upper()

    # Aliasy UX — vi-style + skróty
    if sub in ('B', 'H'):   sub = 'BACK'
    if sub == 'F':          sub = 'FOLLOW'
    if sub in ('S', 'J'):   sub = 'SCROLL'
    if sub == 'K':          return browser.scroll(-1)
    if sub in ('R',):       sub = 'RELOAD'
    if sub == 'L':          sub = 'LINKS'
    if sub in ('O', 'U'):   sub = 'URL'

    # Numer bez komendy → FOLLOW N  (np. "3" → podążaj za linkiem 3)
    if args[0].isdigit():
        return cmd_browse(['FOLLOW', args[0]], browser)

    # Bezpośredni URL (nie subkomenda)
    if sub.startswith('HTTP') or (
        '.' in args[0] and sub not in {
            'BACK', 'FORWARD', 'FWD', 'RELOAD', 'LINKS', 'FOLLOW',
            'FIND', 'SCROLL', 'SOURCE', 'BM', 'BOOKMARKS', 'GOTO',
            'HISTORY', 'SAVE', 'DOM', 'URL', 'O', 'U',
            'J', 'K', 'H', 'L', 'R', 'B', 'F', 'S',
        }
    ):
        _, msg = browser.go(args[0])
        return msg

    if sub == 'BACK':
        _, msg = browser.back()
        return msg

    if sub in ('FORWARD', 'FWD'):
        _, msg = browser.forward()
        return msg

    if sub == 'RELOAD':
        _, msg = browser.reload()
        return msg

    if sub == 'LINKS':
        return browser.show_links()

    if sub == 'FOLLOW':
        if len(args) < 2:
            return 'BROWSE FOLLOW <numer>'
        try:
            _, msg = browser.follow_link(int(args[1]))
            return msg
        except ValueError:
            return f'Nieprawidłowy numer: {args[1]}'

    if sub == 'FIND':
        if len(args) < 2:
            return 'BROWSE FIND <tekst>'
        return browser.find(' '.join(args[1:]))

    if sub == 'READER':
        if not browser._has_dom or browser.dom_mapper is None:
            return "DOMMapper niedostępny. Tryb Reader wymaga modułu karmazyn_dom."
        try:
            from karmazyn_dom import cmd_dom
            return cmd_dom(["READER"] + args[1:], browser, browser.dom_mapper)
        except ImportError:
            return 'Błąd importu karmazyn_dom.'

    if sub == 'SCROLL':
        n = int(args[1]) if len(args) > 1 else 1
        return browser.scroll(n)

    if sub == 'SOURCE':
        n = int(args[1]) if len(args) > 1 else 50
        return browser.show_source(n)

    if sub == 'BM':
        return browser.add_bookmark()

    if sub == 'BOOKMARKS':
        return browser.list_bookmarks()

    if sub == 'GOTO':
        if len(args) < 2:
            return 'BROWSE GOTO <numer>'
        try:
            _, msg = browser.go_bookmark(int(args[1]))
            return msg
        except ValueError:
            return f'Nieprawidłowy numer: {args[1]}'

    if sub == 'HISTORY':
        return browser.show_history()

    if sub == 'SAVE':
        if not browser._current:
            return 'Brak strony.'
        label = (args[1] if len(args) > 1
                 else 'www_' + re.sub(r'[^a-z0-9]', '_',
                                      browser._current.url.lower()
                                      .replace('https://', '')
                                      .replace('http://', ''))[:20])
        try:
            browser.runtime.create_atom(
                label,
                browser._current.title[:64],
                browser._current.url,
                browser.T_CACHED,
            )
            return f'Zapisano jako atom: {label}'
        except Exception as e:
            return f'Błąd zapisu: {e}'

    # ── DOM — phi-space operacje ───────────────────────────────────────────────
    if sub == 'DOM':
        if not browser._has_dom or browser.dom_mapper is None:
            return ('DOMMapper niedostępny (brak karmazyn_dom.py).\n'
                    'Skopiuj karmazyn_dom.py do katalogu projektu.')
        try:
            from karmazyn_dom import cmd_dom
            return cmd_dom(args[1:], browser, browser.dom_mapper)
        except ImportError:
            return 'Błąd importu karmazyn_dom.'


    # ── JS — silnik JavaScript ─────────────────────────────────────────────────
    if sub == 'JS':
        if not browser._has_js or browser.js_bridge is None:
            return ('JSBridge niedostępny (brak karmazyn_js_web.py).\n'
                    'Wymagane: karmazyn_js_core.py, karmazyn_js_phi.py, karmazyn_js_web.py')
        try:
            from karmazyn_js_web import cmd_js_bridge
            return cmd_js_bridge(args[1:], browser.js_bridge)
        except ImportError:
            return 'Błąd importu karmazyn_js_web.'

    if sub == 'URL':
        url = browser._current.url if browser._current else ''
        return "URL: " + url + "\n(wpisz LUNETA <adres> aby otworzyc)"

    # Fallback — traktuj jako URL
    _, msg = browser.go(args[0])
    return msg