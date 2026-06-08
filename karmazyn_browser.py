"""
karmazyn_browser.py — Luneta v5.0 (decode_body, anty-podwójne-mapowanie)
=========================================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Zmiany v5.0 względem v4.9.1:
- HttpResponse.text deleguje do luneta_text.decode_body — naprawia
  CASE-SENSITIVE lookup nagłówka 'content-encoding' (serwery wysyłają
  'Content-Encoding' z dużej; dict(r.headers).get('content-encoding')
  gubił go → dekompresja pomijana → onet.pl/google.pl "binarne").
  decode_body czyta nagłówki case-insensitive + brotli + charset + PL fallback.
- _decode_ok na HttpResponse; _load_url ustawia page.decode_failed
  (DOMMapper pomija strony-błędy).
- _normalize_url: 'wp.pl' i 'wp.pl/' to jeden klucz cache i mapowania
  → koniec podwójnego mapowania i zaniżonego licznika φ.

Zmiany v4.9.1 (zachowane):
- dynamiczne Accept-Encoding, bezpieczny fallback dekompresji
- handle_startendtag (XHTML), LRU cache z TTL, cache renderu z wersjonowaniem DOM
"""

import gzip
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import urllib.error
import socket
from collections import deque, OrderedDict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

# brotli (opcjonalny)
try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

# Wspólne narzędzia tekstowe — dekodowanie odporne na case nagłówków
try:
    from luneta_text import decode_body
    HAS_TEXT_UTILS = True
except ImportError:
    HAS_TEXT_UTILS = False
    decode_body = None

# Stałe
MAX_PAGE_SIZE   = 5 * 1024 * 1024
CACHE_DIR       = ".bubbles/store/browser_cache"
DISPLAY_WIDTH   = 80
PAGE_SIZE       = 40
CACHE_MAX_SIZE  = 100
CACHE_TTL       = 300

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

# ANSI helpers
try:
    import wcwidth
    HAS_WCWIDTH = True
except ImportError:
    HAS_WCWIDTH = False

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
DANGEROUS_ANSI_RE = re.compile(r'\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\|\x1b[PX^_]')

def sanitize_ansi(text: str) -> str:
    return DANGEROUS_ANSI_RE.sub('', text)

def _wcwidth_fallback(ch: str) -> int:
    """Fallback dla wcwidth – używa unicodedata.east_asian_width."""
    ea = unicodedata.east_asian_width(ch)
    if ea in ('F', 'W'):
        return 2
    return 1

def visible_len(s: str) -> int:
    s = ANSI_RE.sub('', s)
    if HAS_WCWIDTH:
        total = 0
        for ch in s:
            w = wcwidth.wcwidth(ch)
            total += max(w, 0)
        return total
    total = 0
    for ch in s:
        total += _wcwidth_fallback(ch)
    return total

class ANSIState:
    def __init__(self):
        self.codes = []

    def apply(self, text: str) -> str:
        return text if not self.codes else '\033[' + ';'.join(self.codes) + 'm' + text

    def update(self, seq: str):
        m = re.match(r'\033\[([0-9;]*)m', seq)
        if m:
            codes = m.group(1).split(';') if m.group(1) else ['0']
            if '0' in codes:
                self.codes = []
            else:
                for c in codes:
                    if c not in self.codes:
                        self.codes.append(c)

def ansi_wrap(text: str, width: int) -> List[str]:
    parts = []
    pos = 0
    for m in ANSI_RE.finditer(text):
        if m.start() > pos:
            parts.append(('text', text[pos:m.start()]))
        parts.append(('ansi', m.group(0)))
        pos = m.end()
    if pos < len(text):
        parts.append(('text', text[pos:]))

    lines = []
    line = ''
    vis = 0
    state = ANSIState()
    for typ, chunk in parts:
        if typ == 'ansi':
            state.update(chunk)
            line += chunk
            continue
        tokens = re.split(r'(\s+)', chunk)
        for tok in tokens:
            if not tok:
                continue
            tv = visible_len(tok)
            if tok.isspace():
                line += tok
                vis += tv
                continue
            if vis + tv > width and vis > 0:
                lines.append(line)
                line = state.apply('')
                vis = 0
            line += tok
            vis += tv
    if line or vis > 0:
        lines.append(line)
    return lines if lines else [text]

# ── Normalizacja URL ──────────────────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    """
    Kanoniczny klucz URL dla cache i mapowania.
    'https://wp.pl' i 'https://wp.pl/' → ten sam klucz, więc strona
    mapuje się RAZ (koniec podwójnego mapowania i zaniżonego licznika φ).

    Zachowuje scheme+host+path bez końcowego slasha (poza samym '/'),
    porzuca fragment (#...). Nie rusza query (?...).
    """
    if not url:
        return url
    try:
        p = urllib.parse.urlsplit(url)
        scheme = p.scheme.lower()
        netloc = p.netloc.lower()
        # Zdejmij WSZYSTKIE końcowe slashe: '/' → '', '/foo/' → '/foo'
        # Dzięki temu 'wp.pl' (path '') i 'wp.pl/' (path '/') zwijają się w jedno.
        path = p.path.rstrip('/')
        return urllib.parse.urlunsplit((scheme, netloc, path, p.query, ''))
    except Exception:
        return url

# HTTP (dekompresja przez decode_body — odporna na case nagłówków)
@dataclass
class HttpResponse:
    url: str
    status: int
    content_type: str
    body: bytes
    headers: dict
    elapsed_ms: float
    truncated: bool = False
    _decode_ok: bool = True

    @property
    def text(self) -> str:
        """
        Dekodowanie przez luneta_text.decode_body — czyta Content-Encoding
        i charset CASE-INSENSITIVE (naprawia gubienie 'Content-Encoding'
        z dużej litery), obsługuje gzip/deflate/br + polskie fallbacki.
        Ustawia _decode_ok dla warstwy wyżej (page.decode_failed).
        """
        if HAS_TEXT_UTILS and decode_body is not None:
            txt, ok = decode_body(self.body, self.headers)
            self._decode_ok = ok
            return txt

        # Fallback gdyby luneta_text był niedostępny — case-insensitive ręcznie
        body = self.body
        ce = ''
        for k, v in (self.headers or {}).items():
            if str(k).lower() == 'content-encoding':
                ce = str(v).lower()
                break
        for enc in [e.strip() for e in ce.split(',') if e.strip()]:
            try:
                if enc == 'gzip':
                    body = gzip.decompress(body)
                elif enc == 'deflate':
                    import zlib
                    try:
                        body = zlib.decompress(body)
                    except zlib.error:
                        body = zlib.decompress(body, -zlib.MAX_WBITS)
                elif enc == 'br' and HAS_BROTLI:
                    body = brotli.decompress(body)
                elif enc == 'br' and not HAS_BROTLI:
                    self._decode_ok = False
                    return ("[Luneta] Serwer użył brotli (br), brak biblioteki. "
                            "pip install brotli")
            except Exception:
                pass
        charset = ''
        for k, v in (self.headers or {}).items():
            if str(k).lower() == 'content-type':
                m = re.search(r'charset=([\w\-]+)', str(v).lower())
                charset = m.group(1) if m else ''
                break
        for enc in (charset, 'utf-8', 'cp1250', 'iso-8859-2'):
            if enc:
                try:
                    text = body.decode(enc)
                    if text[:500].count('\ufffd') / max(1, min(500, len(text))) < 0.05:
                        self._decode_ok = True
                        return text
                except (UnicodeDecodeError, LookupError):
                    continue
        self._decode_ok = False
        return body.decode('utf-8', errors='replace')

    def ok(self) -> bool:
        return 200 <= self.status < 300

# Auto-prezentacja: przedstawiamy się jak zwykła przeglądarka, żeby filtry
# antybotowe nie odrzucały Lunety. Stały, wiarygodny string (Firefox ESR).
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
              "Gecko/20100101 Firefox/128.0")

def http_get(url, headers=None, timeout=15.0):
    supported = ['gzip', 'deflate']
    if HAS_BROTLI:
        supported.append('br')
    # Pełny zestaw nagłówków przeglądarki — sam UA czasem nie wystarcza,
    # serwery patrzą też na Accept / Accept-Language.
    hdrs = {
        'User-Agent': DEFAULT_UA,
        'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                   'image/avif,image/webp,*/*;q=0.8'),
        'Accept-Language': 'pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7',
        'Accept-Encoding': ', '.join(supported),
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }
    if headers:
        hdrs.update(headers)
    start = time.time()
    try:
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = bytearray()
            truncated = False
            while True:
                chunk = r.read(8192)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_PAGE_SIZE:
                    data = data[:MAX_PAGE_SIZE]
                    truncated = True
                    break
            elapsed = (time.time() - start) * 1000
            return HttpResponse(url=url, status=r.status,
                                content_type=r.headers.get('Content-Type', ''),
                                body=bytes(data), headers=dict(r.headers),
                                elapsed_ms=elapsed, truncated=truncated)
    except socket.timeout:
        elapsed = (time.time() - start) * 1000
        return HttpResponse(url=url, status=0, content_type='', body=b'Timeout', headers={}, elapsed_ms=elapsed)
    except Exception as e:
        elapsed = (time.time() - start) * 1000
        return HttpResponse(url=url, status=0, content_type='', body=str(e).encode(), headers={}, elapsed_ms=elapsed)

# Semantyczny DOM
class NodeType:
    DOCUMENT = 0
    BLOCK = 1
    INLINE = 2
    HEADING = 3
    LINK = 4
    LIST = 5
    TABLE = 6
    PRE = 7
    TEXT = 8
    HR = 9

@dataclass
class SemanticNode:
    typ: int
    tag: str = ''
    attrs: dict = field(default_factory=dict)
    text: str = ''
    children: List['SemanticNode'] = field(default_factory=list)
    _plain_cache: Optional[str] = None

    def get_plain_text(self) -> str:
        if self._plain_cache is not None:
            return self._plain_cache
        if self.typ == NodeType.TEXT:
            self._plain_cache = self.text or ''
            return self._plain_cache
        result = ''.join(c.get_plain_text() for c in self.children)
        self._plain_cache = result
        return result

@dataclass
class TextChunk:
    text: str
    preformatted: bool = False
    style: str = ''

@dataclass
class ParsedPage:
    title: str
    chunks: List[TextChunk]
    links: List[Tuple[str, str]]
    headings: List[str]
    raw_html: str
    url: str
    width: int = DISPLAY_WIDTH
    semantic_tree: Optional[SemanticNode] = None
    truncated: bool = False
    decode_failed: bool = False
    _lines_cache: Optional[List[str]] = None

    def invalidate_cache(self):
        self._lines_cache = None

    def lines(self) -> List[str]:
        if self._lines_cache is not None:
            return self._lines_cache
        raw = []
        cur = ''
        for ch in self.chunks:
            if ch.preformatted:
                if cur:
                    raw.append((cur, False))
                    cur = ''
                for pl in ch.text.splitlines():
                    raw.append((pl, True))
            else:
                parts = ch.text.split('\n')
                cur += parts[0]
                for p in parts[1:]:
                    raw.append((cur, False))
                    cur = p
        if cur:
            raw.append((cur, False))

        wrapped = []
        for line, pre in raw:
            if pre:
                wrapped.append(line)
            elif visible_len(line) <= self.width:
                wrapped.append(line)
            else:
                wrapped.extend(ansi_wrap(line, self.width))

        cleaned = []
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

# Parser semantyczny (naprawiony: handle_startendtag)
class SemanticHTMLParser(HTMLParser):
    def __init__(self, base_url='', width=DISPLAY_WIDTH):
        super().__init__()
        self.base_url = base_url
        self.width = width
        self.root = SemanticNode(NodeType.DOCUMENT, 'document')
        self.stack = [self.root]
        self.skip_depth = 0
        self.pre_depth = 0
        self.in_title = False
        self.title = ''

    def _is_hidden(self, attrs):
        style = attrs.get('style', '').replace(' ', '').lower()
        return ('hidden' in attrs or
                attrs.get('aria-hidden', '') == 'true' or
                'display:none' in style or
                'visibility:hidden' in style)

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        ad = dict(attrs)
        if tag in ('script', 'style', 'noscript') or self._is_hidden(ad):
            self.skip_depth += 1
            return
        if self.skip_depth > 0:
            return
        if tag == 'title':
            self.in_title = True
            return
        if tag in ('pre', 'code'):
            self.pre_depth += 1

        node = SemanticNode(NodeType.INLINE, tag, ad)
        if tag in ('p','div','article','section','main','header','footer','nav','aside','br','li','tr','td','th','tbody','thead'):
            node.typ = NodeType.BLOCK
        elif tag in ('h1','h2','h3','h4','h5','h6'):
            node.typ = NodeType.HEADING
        elif tag == 'a':
            node.typ = NodeType.LINK
        elif tag in ('ul','ol'):
            node.typ = NodeType.LIST
        elif tag == 'table':
            node.typ = NodeType.TABLE
        elif tag in ('pre','code'):
            node.typ = NodeType.PRE
        elif tag == 'hr':
            node.typ = NodeType.HR

        self.stack[-1].children.append(node)
        if tag not in ('br','hr','img','meta','link','input'):
            self.stack.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ('script', 'style', 'noscript'):
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth > 0:
            return
        if tag == 'title':
            self.in_title = False
            return
        if tag in ('pre', 'code'):
            self.pre_depth = max(0, self.pre_depth - 1)

        for i in range(len(self.stack)-1, 0, -1):
            if self.stack[i].tag == tag:
                self.stack = self.stack[:i]
                return

    def handle_startendtag(self, tag, attrs):
        """Kluczowa metoda dla tagów samodomykających (XHTML)."""
        tag = tag.lower()
        ad = dict(attrs)
        if tag in ('script', 'style', 'noscript'):
            self.skip_depth += 1
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

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
        self.stack[-1].children.append(node)

    def handle_entityref(self, name):
        self.handle_data(html.unescape(f'&{name};'))

    def handle_charref(self, name):
        self.handle_data(html.unescape(f'&#{name};'))

# Renderer ANSI
class ANSIRenderer:
    def __init__(self, base_url, width):
        self.base_url = base_url
        self.width = width
        self.chunks = []
        self.links = []
        self.headings = []

    def add_text(self, text, pre=False):
        if text:
            self.chunks.append(TextChunk(text, pre))

    def render(self, root):
        self._walk(root)

    def _walk(self, node):
        if node.typ == NodeType.TEXT:
            self.add_text(node.text)
            return
        if node.typ == NodeType.HR:
            self.add_text('\n' + '─'*self.width + '\n')
            return
        if node.typ == NodeType.PRE:
            self.add_text(node.get_plain_text(), True)
            return
        if node.typ == NodeType.TABLE:
            self._render_table(node)
            return
        if node.typ == NodeType.HEADING:
            level = int(node.tag[1]) if len(node.tag) > 1 and node.tag[1].isdigit() else 1
            text = node.get_plain_text().strip()
            if text:
                self.headings.append(text)
                prefix = '#'*level + ' '
                line = prefix + text
                color = COLORS['yellow'] if level == 1 else COLORS['green']
                sep = ('═' if level == 1 else '─') * min(visible_len(line), self.width)
                self.add_text(f'\n{color}{line}{COLORS["reset"]}\n{sep}\n')
            return
        if node.typ == NodeType.LIST:
            self.add_text('\n')
            is_ol = node.tag == 'ol'
            li_cnt = 0
            for c in node.children:
                if c.tag == 'li':
                    li_cnt += 1
                    self.add_text(f'{li_cnt}. ' if is_ol else '  • ')
                    self._walk(c)
                    self.add_text('\n')
                else:
                    self._walk(c)
            self.add_text('\n')
            return
        if node.typ == NodeType.LINK:
            url = _resolve_url(node.attrs.get('href', ''), self.base_url)
            if url:
                idx = len(self.links) + 1
                self.links.append((url, node.get_plain_text().strip() or url))
                for c in node.children:
                    self._walk(c)
                self.add_text(f'{COLORS["blue"]} [{idx}]{COLORS["reset"]}')
            else:
                for c in node.children:
                    self._walk(c)
            return
        is_block = (node.typ == NodeType.BLOCK and node.tag not in ('li','tr','td','th','tbody','thead'))
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
        if node.typ == NodeType.INLINE and node.tag in ('strong','b','em','i'):
            self.add_text(COLORS['reset'])
        if is_block:
            self.add_text('\n')

    def _render_table(self, tbl):
        rows = []
        def find_rows(n):
            if n.tag == 'tr':
                rows.append(n)
            else:
                for c in n.children:
                    find_rows(c)
        find_rows(tbl)
        data = []
        for r in rows:
            cells = []
            for c in r.children:
                if c.tag in ('td', 'th'):
                    cs = int(c.attrs.get('colspan', 1))
                    txt = ANSI_RE.sub('', c.get_plain_text()).strip()
                    cells.append((txt, cs))
            if cells:
                data.append(cells)
        if not data:
            return
        max_cols = max(sum(cs for _, cs in row) for row in data)
        col_w = [0] * max_cols
        for row in data:
            ci = 0
            for txt, cs in row:
                w = visible_len(txt) + 2
                for i in range(cs):
                    if ci + i < max_cols:
                        col_w[ci + i] = max(col_w[ci + i], w)
                ci += cs
        total = sum(col_w) + max_cols - 1
        if total > self.width:
            f = (self.width - max_cols + 1) / total
            col_w = [max(3, int(w * f)) for w in col_w]
        sep = '+' + '+'.join('-' * w for w in col_w) + '+'
        self.add_text('\n' + sep + '\n')
        for row in data:
            ci = 0
            cells_r = []
            for txt, cs in row:
                w = sum(col_w[ci:ci+cs])
                vl = visible_len(txt)
                if vl > w:
                    txt = txt[:w-1] + '…'
                    pad = 0
                else:
                    pad = w - vl
                cells_r.append(txt + ' ' * pad)
                ci += cs
            while len(cells_r) < max_cols:
                cells_r.append(' ' * col_w[len(cells_r)])
            self.add_text('|' + '|'.join(cells_r) + '|\n')
        self.add_text(sep + '\n')

def _resolve_url(href, base):
    if not href or href.startswith(('javascript:', 'mailto:', '#')):
        return ''
    if not base:
        return href
    try:
        return urllib.parse.urljoin(base, href)
    except Exception:
        return href

def parse_html(html_text, url='', width=DISPLAY_WIDTH, truncated=False):
    parser = SemanticHTMLParser(base_url=url, width=width)
    try:
        parser.feed(html_text)
    except Exception as e:
        _log_error(f'parse_html: {e}')
    renderer = ANSIRenderer(url, width)
    renderer.render(parser.root)
    return ParsedPage(
        title=parser.title.strip() or url,
        chunks=renderer.chunks,
        links=renderer.links,
        headings=renderer.headings,
        raw_html=html_text,
        url=url,
        width=width,
        semantic_tree=parser.root,
        truncated=truncated,
    )

def _log_error(msg):
    print(f'[Browser Error] {msg}', file=sys.stderr)

# LunetaBrowser – wersja 5.0
class LunetaBrowser:
    T_FRESH = 80.0
    T_CACHED = 45.0
    T_STALE = 20.0

    def __init__(self, runtime, width=DISPLAY_WIDTH):
        self.runtime = runtime
        self.width = width
        self._history = deque(maxlen=50)
        self._forward = deque(maxlen=50)
        self._current = None
        self._scroll = 0
        self._cache = OrderedDict()
        self._bookmarks = {}
        self._dom_version = 0
        self._render_cache = None
        self._render_cache_info = None
        os.makedirs(CACHE_DIR, exist_ok=True)
        self._load_bookmarks()

        try:
            from karmazyn_dom import DOMMapper
            self.dom_mapper = DOMMapper(runtime)
            self._has_dom = True
        except ImportError:
            self.dom_mapper = None
            self._has_dom = False
        try:
            from karmazyn_js_web import JSBridge
            self.js_bridge = JSBridge(runtime)
            self._has_js = True
        except ImportError:
            self.js_bridge = None
            self._has_js = False

    def resize(self, new_width):
        self.width = new_width
        if self._current:
            self._current.width = new_width
            self._current.invalidate_cache()
        self._invalidate_render_cache()

    def _invalidate_render_cache(self):
        self._render_cache = None
        self._render_cache_info = None

    def _increment_dom_version(self):
        self._dom_version += 1
        self._invalidate_render_cache()

    def _cache_purge_expired(self):
        now = time.time()
        expired = [u for u, (_, ts) in self._cache.items() if now - ts > CACHE_TTL]
        for u in expired:
            self._cache.pop(u, None)

    def _cache_put(self, url, page):
        self._cache_purge_expired()
        if len(self._cache) >= CACHE_MAX_SIZE:
            self._cache.popitem(last=False)
        self._cache[url] = (page, time.time())
        self._cache.move_to_end(url)

    def _cache_get(self, url):
        self._cache_purge_expired()
        if url in self._cache:
            page, ts = self._cache[url]
            if time.time() - ts <= CACHE_TTL:
                self._cache.move_to_end(url)
                return page, ts
            else:
                self._cache.pop(url, None)
        return None

    def _update_atom_temp(self, url, temp):
        try:
            label = 'www_' + re.sub(r'[^a-z0-9]', '_', url.lower().replace('https://', '').replace('http://', ''))[:20]
            if self.runtime.matrix.has_atom(label):
                atom = self.runtime.get_atom(label)
                if atom:
                    if temp > atom.T:
                        atom.heat(temp - atom.T)
                    else:
                        atom.cool(atom.T - temp)
            else:
                self.runtime.create_atom(label, url[:64], url, temp)
        except Exception:
            pass

    def _load_url(self, url, add_to_history=True, force_reload=False):
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        cur = _normalize_url(url)
        for _ in range(5):
            if not force_reload:
                cached = self._cache_get(cur)
                if cached:
                    page, _ = cached
                    if add_to_history and self._current:
                        self._add_to_history(self._current.url)
                        self._forward.clear()
                    self._current = page
                    self._scroll = 0
                    self._clamp_scroll()
                    self._update_atom_temp(cur, self.T_CACHED)
                    self._increment_dom_version()
                    self._map_dom(page)
                    self._invalidate_render_cache()
                    return True, self._render_current()
            resp = http_get(cur)
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get('Location') or resp.headers.get('location')
                if loc:
                    nxt = _normalize_url(_resolve_url(loc, cur))
                    if nxt and nxt != cur:
                        cur = nxt
                        continue
            break
        if not resp or not resp.ok():
            return False, f'HTTP {resp.status if resp else 0}: {cur}'
        ct = resp.content_type.lower()
        if 'html' not in ct and 'text' not in ct:
            return False, f'Nieobsługiwany typ: {resp.content_type}\nURL: {cur}'
        body_text = resp.text
        page = parse_html(body_text, url=cur, width=self.width, truncated=resp.truncated)
        # Flaga dla DOMMappera — strona-błąd dekodowania nie trafia do phi-space
        page.decode_failed = not getattr(resp, '_decode_ok', True)
        self._cache_put(cur, page)
        self._update_atom_temp(cur, self.T_FRESH)
        if add_to_history and self._current:
            self._add_to_history(self._current.url)
            self._forward.clear()
        self._current = page
        self._scroll = 0
        self._clamp_scroll()
        self._increment_dom_version()
        self._map_dom(page)
        self._invalidate_render_cache()
        return True, self._render_current()

    def _map_dom(self, page):
        if self._has_dom and self.dom_mapper:
            try:
                self.dom_mapper.map_page(page)
            except Exception as e:
                _log_error(f'DOM map: {e}')
        if self._has_js and self.js_bridge:
            try:
                self.js_bridge.attach(page)
                self.js_bridge.run_scripts(page)
            except Exception as e:
                _log_error(f'JS bridge: {e}')

    def _add_to_history(self, url):
        if self._history and self._history[-1] == url:
            return
        self._history.append(url)

    def _clamp_scroll(self):
        if not self._current:
            self._scroll = 0
            return
        total = len(self._current.lines())
        self._scroll = max(0, min(self._scroll, max(0, total - PAGE_SIZE)))

    def go(self, url, force_reload=False):
        return self._load_url(url, True, force_reload)

    def back(self):
        if not self._history:
            return False, 'Brak historii.'
        if self._current:
            self._forward.appendleft(self._current.url)
        return self._load_url(self._history.pop(), False)

    def forward(self):
        if not self._forward:
            return False, 'Brak stron do przodu.'
        if self._current:
            self._add_to_history(self._current.url)
        return self._load_url(self._forward.popleft(), False)

    def reload(self):
        if not self._current:
            return False, 'Brak strony.'
        self._cache.pop(self._current.url, None)
        return self._load_url(self._current.url, False, True)

    def follow_link(self, n):
        if not self._current:
            return False, 'Brak strony.'
        if not self._current.links:
            return False, 'Brak linków.'
        if n < 1 or n > len(self._current.links):
            return False, f'Link {n} nie istnieje (1-{len(self._current.links)}).'
        url, _ = self._current.links[n-1]
        if not url:
            return False, 'Pusty link.'
        return self.go(url)

    def _render_current(self):
        if not self._current:
            return 'Brak strony.'
        cache_key = (self._current.url, self._scroll, self.width, self._dom_version)
        if self._render_cache is not None and self._render_cache_info == cache_key:
            return self._render_cache

        self._clamp_scroll()
        lines = self._current.lines()
        total = len(lines)
        url_short = self._current.url[:self.width-10]
        header = [
            COLORS['gray'] + '─'*self.width + COLORS['reset'],
            COLORS['bold'] + f'  {self._current.title[:self.width-4]}' + COLORS['reset'],
            COLORS['cyan'] + f'  {url_short}' + COLORS['reset'],
            f'  Linki: {len(self._current.links)}  |  Linie: {total}',
            COLORS['gray'] + '─'*self.width + COLORS['reset'],
        ]
        if self._current.truncated:
            header.insert(3, COLORS['red'] + '  [STRONA PRZYCIĘTA – LIMIT 5 MB]' + COLORS['reset'])
        if self._has_dom and self.dom_mapper and self._current.url in getattr(self.dom_mapper, '_page_atoms', {}):
            n_atoms = len(self.dom_mapper._page_atoms[self._current.url])
            js_info = ''
            if self._has_js and self.js_bridge and self.js_bridge._active:
                s = self.js_bridge.status()
                js_info = f'  JS:{s.get("atoms", 0)}at'
            header.insert(4, COLORS['gray'] + f'  φ: {n_atoms} atomów{js_info}' + COLORS['reset'])
        page_lines = lines[self._scroll:self._scroll+PAGE_SIZE]
        remaining = max(0, total - self._scroll - PAGE_SIZE)
        footer = COLORS['gray'] + '─'*self.width + COLORS['reset'] + '\n'
        footer += f'[{self._scroll+1}-{min(self._scroll+PAGE_SIZE, total)}/{total}]'
        if remaining:
            footer += f'  LUNETA SCROLL 1 aby kontynuować ({remaining} linii)'
        result = '\n'.join(header + page_lines + [footer])
        self._render_cache = result
        self._render_cache_info = cache_key
        return result

    def scroll(self, pages=1):
        if not self._current:
            return 'Brak strony.'
        self._scroll += pages * PAGE_SIZE
        self._clamp_scroll()
        self._invalidate_render_cache()
        return self._render_current()

    def find(self, q):
        if not self._current:
            return 'Brak strony.'
        lines = self._current.lines()
        pat = re.compile(re.escape(q), re.I)
        hits = []
        for i, line in enumerate(lines, 1):
            stripped = ANSI_RE.sub('', line)
            if pat.search(stripped):
                hl = pat.sub(lambda m: COLORS['red'] + m.group(0) + COLORS['reset'], stripped)
                hits.append((i, hl))
        if not hits:
            return f"Nie znaleziono: '{q}'"
        res = [COLORS['yellow'] + f"Znaleziono '{q}' ({len(hits)} wystąpień):" + COLORS['reset']]
        for no, line in hits[:15]:
            res.append(f'  [{no:4}] {line[:self.width-10]}')
        if len(hits) > 15:
            res.append(f'  ... i {len(hits)-15} więcej')
        return '\n'.join(res)

    def show_links(self):
        if not self._current:
            return 'Brak strony.'
        if not self._current.links:
            return 'Brak linków.'
        lines = [COLORS['bold'] + f'Linki ({len(self._current.links)}):' + COLORS['reset']]
        for i, (url, text) in enumerate(self._current.links, 1):
            lines.append(f'  {COLORS["blue"]}[{i:3}]{COLORS["reset"]} {text[:30]:<30} {COLORS["cyan"]}{url[:50]}{COLORS["reset"]}')
        return '\n'.join(lines)

    def show_source(self, n=50):
        if not self._current:
            return 'Brak strony.'
        return '\n'.join(self._current.raw_html.splitlines()[:n])

    def _load_bookmarks(self):
        path = os.path.join(CACHE_DIR, 'bookmarks.json')
        if os.path.exists(path):
            try:
                with open(path, encoding='utf-8') as f:
                    self._bookmarks = json.load(f)
            except Exception:
                self._bookmarks = {}

    def _save_bookmarks(self):
        path = os.path.join(CACHE_DIR, 'bookmarks.json')
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self._bookmarks, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add_bookmark(self):
        if not self._current:
            return 'Brak strony.'
        url, title = self._current.url, self._current.title
        self._bookmarks[url] = title
        self._save_bookmarks()
        label = 'bm_' + re.sub(r'[^a-z0-9]', '_', url.lower())[:20]
        try:
            if not self.runtime.matrix.has_atom(label):
                self.runtime.create_atom(label, title[:64], url, self.T_CACHED)
        except Exception:
            pass
        return f'Dodano zakładkę: {title}'

    def list_bookmarks(self):
        if not self._bookmarks:
            return 'Brak zakładek.'
        lines = [COLORS['bold'] + f'Zakładki ({len(self._bookmarks)}):' + COLORS['reset']]
        for i, (url, title) in enumerate(self._bookmarks.items(), 1):
            lines.append(f'  {COLORS["green"]}[{i:3}]{COLORS["reset"]} {title[:35]:<35} {COLORS["cyan"]}{url[:45]}{COLORS["reset"]}')
        return '\n'.join(lines)

    def go_bookmark(self, n):
        urls = list(self._bookmarks.keys())
        if n < 1 or n > len(urls):
            return False, f'Zakładka {n} nie istnieje (1-{len(urls)}).'
        return self.go(urls[n-1])

    def show_history(self):
        if not self._history:
            return 'Historia pusta.'
        lines = [COLORS['bold'] + f'Historia ({len(self._history)}):' + COLORS['reset']]
        for url in reversed(self._history):
            cached = url in self._cache
            mark = COLORS['green'] + '✓' + COLORS['reset'] if cached else ' '
            lines.append(f'  [{mark}] {url[:self.width-8]}')
        return '\n'.join(lines)


# Komenda shella
class _LunetaCmd:
    def __init__(self, runtime):
        self._browser = LunetaBrowser(runtime)

    def cmd(self, args):
        return cmd_luneta(args, self._browser)


LunetaBrowser.cmd = lambda self, args: cmd_luneta(args, self)
# Alias kompatybilności
KarmazynBrowser = LunetaBrowser


def cmd_luneta(args, browser):
    if not args:
        return browser._render_current() if browser._current else 'LUNETA <url>'
    sub = args[0].upper()
    if sub in ('B', 'H'):
        sub = 'BACK'
    if sub == 'F':
        sub = 'FOLLOW'
    if sub in ('S', 'J'):
        sub = 'SCROLL'
    if sub == 'K':
        return browser.scroll(-1)
    if sub == 'R':
        sub = 'RELOAD'
    if sub == 'L':
        sub = 'LINKS'
    if sub in ('O', 'U'):
        sub = 'URL'
    if args[0].isdigit():
        return cmd_luneta(['FOLLOW', args[0]], browser)
    if sub.startswith('HTTP') or ('.' in args[0] and sub not in {
        'BACK', 'FORWARD', 'FWD', 'RELOAD', 'LINKS', 'FOLLOW', 'FIND',
        'SCROLL', 'SOURCE', 'BM', 'BOOKMARKS', 'GOTO', 'HISTORY', 'SAVE',
        'DOM', 'URL', 'O', 'U', 'J', 'K', 'H', 'L', 'R', 'B', 'F', 'S'
    }):
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
            return 'LUNETA FOLLOW <numer>'
        try:
            _, msg = browser.follow_link(int(args[1]))
            return msg
        except ValueError:
            return f'Nieprawidłowy numer: {args[1]}'
    if sub == 'FIND':
        if len(args) < 2:
            return 'LUNETA FIND <tekst>'
        return browser.find(' '.join(args[1:]))
    if sub == 'READER':
        if not browser._has_dom or not browser.dom_mapper:
            return "DOMMapper niedostępny."
        try:
            from karmazyn_dom import cmd_dom
            return cmd_dom(["READER"] + args[1:], browser, browser.dom_mapper)
        except ImportError:
            return 'Błąd importu karmazyn_dom.'
    if sub == 'SCROLL':
        return browser.scroll(int(args[1]) if len(args) > 1 else 1)
    if sub == 'SOURCE':
        return browser.show_source(int(args[1]) if len(args) > 1 else 50)
    if sub == 'BM':
        return browser.add_bookmark()
    if sub == 'BOOKMARKS':
        return browser.list_bookmarks()
    if sub == 'GOTO':
        if len(args) < 2:
            return 'LUNETA GOTO <numer>'
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
        label = args[1] if len(args) > 1 else 'www_' + re.sub(r'[^a-z0-9]', '_', browser._current.url.lower().replace('https://', '').replace('http://', ''))[:20]
        try:
            browser.runtime.create_atom(label, browser._current.title[:64], browser._current.url, browser.T_CACHED)
            return f'Zapisano jako atom: {label}'
        except Exception as e:
            return f'Błąd zapisu: {e}'
    if sub == 'DOM':
        if not browser._has_dom or not browser.dom_mapper:
            return 'DOMMapper niedostępny.'
        try:
            from karmazyn_dom import cmd_dom
            return cmd_dom(args[1:], browser, browser.dom_mapper)
        except ImportError:
            return 'Błąd importu karmazyn_dom.'
    if sub == 'JS':
        if not browser._has_js or not browser.js_bridge:
            return 'JSBridge niedostępny.'
        try:
            from karmazyn_js_web import cmd_js_bridge
            return cmd_js_bridge(args[1:], browser.js_bridge)
        except ImportError:
            return 'Błąd importu karmazyn_js_web.'
    if sub in ('RECALL', 'COMMON', 'EXPORT', 'MEMORY', 'PAMIEC'):
        try:
            from karmazyn_recall import cmd_recall
            return cmd_recall([sub] + args[1:], browser)
        except ImportError:
            return 'Błąd importu karmazyn_recall.'
    if sub == 'URL':
        url = browser._current.url if browser._current else ''
        return "URL: " + url + "\n(wpisz LUNETA <adres> aby otworzyc)"
    _, msg = browser.go(args[0])
    return msg