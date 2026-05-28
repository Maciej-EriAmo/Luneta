"""
karmazyn_dom.py — DOM → Phi-Space Mapper KarmazynOS v1.2
=========================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Zmiany v1.2:
  - DOM READER: try/except ValueError przy konwersji T_threshold.
    Poprzednio: DOM READER X crashował shell ValueError.

Zmiany v1.1:
  - __all__ = ['DOMMapper', 'cmd_dom'] — eksplicitny publiczny interfejs
  - _map_table: lepsze wykrywanie <thead> — wiersze w thead zawsze header,
    niezależnie od obecności <th>. Stary kod: row_idx==0 or _has_th(row).
    Nowy: find_rows(n, parent_tag) propaguje tag rodzica do każdego <tr>.

DOM nie jest zewnętrzną strukturą — jest phi-space pod inną nazwą.
Ten moduł uznaje tę tożsamość i mapuje SemanticNode na natywne byty:

  Atom:
    Tekstowy węzeł liść, link, nagłówek.
    S = rola semantyczna ("heading:h1", "link", "text:p")
    E = treść (do 256 znaków) lub URL
    T = ważność semantyczna (nagłówki HOT, stopka COLD)

  Bubble:
    Akapit, lista, wiersz tabeli — zbiór powiązanych atomów.
    Grupuje atomy które razem tworzą sens.

  Bubble zagnieżdżony:
    Tabela → bąbel wierszy → bąble komórek.
    Wielowymiarowe struktury danych w naturalnej hierarchii phi-space.

  Hologram:
    Cały dokument. Prototyp = atom tytułu (najbardziej reprezentatywny).
    Generatory = atomy nagłówków (szkielet strony).
    Sieć linków = relacje między hologramami stron.

Korzyści natywnej reprezentacji:
  Reader Mode:
    Render tylko atomów z T >= prog. Nawigacja i stopka mają niskie T
    → naturalny filtr boilerplate bez heurystyk readability.js.

  DOM Diff na reload:
    Stare atomy stygną. Nowe są HOT. Diff = które atomy zmieniły T.
    Nie porównujesz HTML — porównujesz temperatury.

  Cross-page resonance:
    Ten sam termin na różnych stronach = atomy które rezonują.
    BROWSE PHI "łuki kompozytowe" szuka po całej historii przeglądania.

  Persistencja między sesjami:
    Soul store zapisuje atomy DOM → offline browsing naturalnie.

  Phi-space queries zamiast text grep:
    BROWSE FIND przeszukuje atomy bieżącej strony.
    BROWSE PHI przeszukuje wszystkie strony w phi-space.

Temperatury semantyczne:
  h1             = 90    (centrum dokumentu)
  h2-h3          = 82/75 (sekcje)
  h4-h6          = 68/62/55
  <pre>/<code>   = 78    (kod = cenna informacja)
  <table>        = 70    (dane strukturalne)
  <li>           = 65    (wyliczenia)
  <a>            = 62    (nawigacja)
  <p>/<div>      = 55    (treść)
  <footer>/<nav> = 25    (boilerplate — naturalnie wypada w reader mode)
"""

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Import typów z parsera — opcjonalne (fallback na duck typing)
try:
    from karmazyn_browser import (
        ParsedPage, SemanticNode, NodeType, ANSI_RE,
    )
    HAS_BROWSER = True
except ImportError:
    HAS_BROWSER = False
    # Duck typing — mapper działa z dowolnym obiektem mającym .semantic_tree

__all__ = ['DOMMapper', 'cmd_dom']

# ─── Temperatura semantyczna ─────────────────────────────────────────────────

# Temperatura bazowa dla danej roli semantycznej
T_BASE: Dict[str, float] = {
    # Nagłówki — im wyższy poziom, tym gorętszy
    "heading:h1": 90.0,
    "heading:h2": 82.0,
    "heading:h3": 75.0,
    "heading:h4": 68.0,
    "heading:h5": 62.0,
    "heading:h6": 55.0,
    # Treść strukturalna
    "pre":        78.0,
    "code":       75.0,
    "table":      70.0,
    "li":         65.0,
    "link":       62.0,
    # Treść ogólna
    "p":          55.0,
    "blockquote": 58.0,
    "article":    60.0,
    # Boilerplate — naturalnie wypada w reader mode
    "footer":     20.0,
    "nav":        22.0,
    "aside":      28.0,
    "header":     35.0,
}

# Zanikanie T z głębokością zagnieżdżenia (każdy poziom -2.0)
T_DEPTH_DECAY = 2.0

# Minimum T dla atomów (nie spadaj do zera — TOMB to za wcześnie przy sesji)
T_MIN_PAGE = 10.0

# T dla atomu tytułu strony (hologram prototyp)
T_TITLE = 95.0


def _semantic_T(role: str, depth: int = 0) -> float:
    """Oblicza temperaturę atomu na podstawie roli semantycznej i głębokości."""
    base = T_BASE.get(role, 50.0)
    decay = min(depth * T_DEPTH_DECAY, base - T_MIN_PAGE)
    return max(T_MIN_PAGE, base - decay)


# ─── Kontekst mapowania ───────────────────────────────────────────────────────

@dataclass
class _MapCtx:
    """Kontekst przekazywany podczas chodzenia po drzewie DOM."""
    prefix:     str               # prefiks ID atomów tej strony
    seq:        List[int]         # [0] — globalny licznik atomów
    depth:      int = 0           # głębokość zagnieżdżenia
    in_boiler:  bool = False      # jesteśmy w footer/nav/aside
    atom_ids:   List[str] = field(default_factory=list)
    bubble_lbls: List[str] = field(default_factory=list)

    def next_id(self, prefix_local: str = "n") -> str:
        self.seq[0] += 1
        return f"{self.prefix}_{prefix_local}{self.seq[0]}"

    def child(self, boilerplate: bool = False) -> "_MapCtx":
        return _MapCtx(
            prefix      = self.prefix,
            seq         = self.seq,
            depth       = self.depth + 1,
            in_boiler   = self.in_boiler or boilerplate,
            atom_ids    = self.atom_ids,
            bubble_lbls = self.bubble_lbls,
        )


# ─── Główna klasa mappera ─────────────────────────────────────────────────────

class DOMMapper:
    """
    Mapuje drzewo DOM (SemanticNode) na natywne byty KarmazynOS.

    Nie duplikuje parsowania — działa na gotowym semantic_tree
    z ParsedPage. Może być wywołany po go() lub lazily przy BROWSE DOM.

    Idempotentny: ponowne mapowanie tej samej strony aktualizuje
    temperatury istniejących atomów (diff przez T) zamiast tworzyć duplikaty.
    """

    # Tagi boilerplate — niskie T
    BOILERPLATE_TAGS = {"footer", "nav", "aside", "header"}

    # Tagi które stają się bąblami (kontenery atomów)
    BUBBLE_TAGS = {"p", "li", "blockquote", "article",
                   "section", "div", "td", "th"}

    # Maksymalna długość E atomu tekstowego
    MAX_E_LEN = 256

    def __init__(self, runtime):
        self.runtime = runtime
        # url → lista atom_ids (strona → jej atomy)
        self._page_atoms:   Dict[str, List[str]] = {}
        # url → hologram_id
        self._page_holos:   Dict[str, str]       = {}
        # url → bubble_labels
        self._page_bubbles: Dict[str, List[str]] = {}
        # url → timestamp mapowania
        self._mapped_at:    Dict[str, float]     = {}

    # ── Mapowanie strony ──────────────────────────────────────────────────────

    def map_page(self, page: Any) -> Optional[str]:
        """
        Mapuje ParsedPage na phi-space.
        Zwraca hologram_id lub None przy błędzie.

        Idempotentne: jeśli strona była już mapowana < 300s temu, pomija.
        Przy reload (force=True) aktualizuje T istniejących atomów.
        """
        url = page.url
        tree = getattr(page, "semantic_tree", None)
        if tree is None:
            return None

        # Idempotencja — nie mapuj ponownie świeżej strony
        mapped_at = self._mapped_at.get(url, 0)
        if time.time() - mapped_at < 300:
            return self._page_holos.get(url)

        page_hash = hashlib.sha1(url.encode()).hexdigest()[:10]
        prefix    = f"dom_{page_hash}"
        ctx       = _MapCtx(prefix=prefix, seq=[0])

        # Atom tytułu — prototyp hologramu
        title_id = self._make_atom(
            label = f"{prefix}_title",
            S     = "title",
            E     = page.title[:self.MAX_E_LEN],
            T     = T_TITLE,
        )
        if title_id:
            ctx.atom_ids.append(title_id)

        # Chód po drzewie
        self._walk(tree, ctx)

        # Hologram strony
        hid = self._make_page_hologram(page, ctx, prefix)

        # Zapisz stan
        self._page_atoms[url]   = ctx.atom_ids
        self._page_bubbles[url] = ctx.bubble_lbls
        self._page_holos[url]   = hid or ""
        self._mapped_at[url]    = time.time()

        return hid

    # ── Chód po drzewie ───────────────────────────────────────────────────────

    def _walk(self, node: Any, ctx: _MapCtx) -> Optional[str]:
        """
        Rekurencyjnie chodzi po SemanticNode i tworzy atomy/bąble.
        Zwraca atom_id liścia lub bubble_label kontenera.
        """
        if node is None:
            return None

        typ = getattr(node, "typ", -1)
        tag = getattr(node, "tag", "")
        children = getattr(node, "children", [])

        # NodeType wartości z karmazyn_browser
        NODE_TEXT     = 8
        NODE_HEADING  = 3
        NODE_LINK     = 4
        NODE_HR       = 9
        NODE_PRE      = 7
        NODE_TABLE    = 6
        NODE_LIST     = 5
        NODE_BLOCK    = 1
        NODE_DOCUMENT = 0

        # ── Liść tekstowy ───────────────────────────────────────────────
        if typ == NODE_TEXT:
            text = _clean_text(getattr(node, "text", ""))
            if not text:
                return None
            role  = "footer" if ctx.in_boiler else "p"
            T     = _semantic_T(role, ctx.depth)
            label = ctx.next_id("txt")
            aid   = self._make_atom(label, S=f"text:{role}", E=text, T=T)
            if aid:
                ctx.atom_ids.append(aid)
            return aid

        # ── Nagłówek ────────────────────────────────────────────────────
        if typ == NODE_HEADING:
            level = tag[1] if len(tag) > 1 and tag[1].isdigit() else "1"
            text  = _plain_text(node)
            if not text:
                return None
            role  = f"heading:h{level}"
            T     = _semantic_T(role, ctx.depth)
            label = ctx.next_id(f"h{level}")
            aid   = self._make_atom(label, S=role, E=text, T=T)
            if aid:
                ctx.atom_ids.append(aid)
            return aid

        # ── Link ────────────────────────────────────────────────────────
        if typ == NODE_LINK:
            href = getattr(node, "attrs", {}).get("href", "")
            text = _plain_text(node)
            if not href or not text:
                # Nadal chód po dzieciach
                for c in children:
                    self._walk(c, ctx.child())
                return None
            T     = _semantic_T("link", ctx.depth)
            label = ctx.next_id("lnk")
            # S = tekst linku (co znaczy), E = URL (gdzie prowadzi)
            aid   = self._make_atom(label, S=f"link:{text[:64]}", E=href, T=T)
            if aid:
                ctx.atom_ids.append(aid)
            return aid

        # ── Pre/Code — zachowaj jako całość ────────────────────────────
        if typ == NODE_PRE:
            text  = _plain_text(node)
            if not text.strip():
                return None
            T     = _semantic_T("pre", ctx.depth)
            label = ctx.next_id("pre")
            # Kod to cenny atom — E przechowuje pierwsze 256 znaków
            aid   = self._make_atom(label, S="code", E=text[:self.MAX_E_LEN], T=T)
            if aid:
                ctx.atom_ids.append(aid)
            return aid

        # ── HR ──────────────────────────────────────────────────────────
        if typ == NODE_HR:
            return None   # separator — nie tworzy atomu

        # ── Tabela — wielowymiarowa struktura → zagnieżdżone bąble ─────
        if typ == NODE_TABLE:
            return self._map_table(node, ctx)

        # ── Lista → bąbel elementów ─────────────────────────────────────
        if typ == NODE_LIST:
            return self._map_list(node, ctx)

        # ── Blok kontenera ──────────────────────────────────────────────
        if typ in (NODE_BLOCK, NODE_DOCUMENT):
            is_boiler = tag in self.BOILERPLATE_TAGS
            child_ctx = ctx.child(boilerplate=is_boiler)

            # Akapit/artykuł → bąbel grupujący atomy
            if tag in self.BUBBLE_TAGS:
                return self._map_block_bubble(node, child_ctx, tag)

            # Inne kontenery — przejdź przez dzieci
            for c in children:
                self._walk(c, child_ctx)
            return None

        # ── INLINE — przejdź przez dzieci ──────────────────────────────
        for c in children:
            self._walk(c, ctx.child())
        return None

    # ── Mapowanie tabeli ──────────────────────────────────────────────────────

    def _map_table(self, table_node: Any, ctx: _MapCtx) -> Optional[str]:
        """
        Tabela → zagnieżdżone bąble:
          table_bubble
            row_bubble_0  (nagłówkowy — T wyższe)
              cell_atom_0_0
              cell_atom_0_1
            row_bubble_1
              cell_atom_1_0

        v1.1: find_rows propaguje parent_tag — wiersze w <thead> są zawsze
        nagłówkowe, nawet jeśli używają <td> zamiast <th>.
        Stara logika: row_idx == 0 or _has_th(row) — myliła tbody row_0
        z nagłówkiem w tabelach bez thead.
        """
        tbl_label = ctx.next_id("tbl")

        # Zbierz wiersze z informacją o rodzicu (thead/tbody/tfoot)
        rows: List[Tuple[Any, bool]] = []  # (node_tr, is_header)

        def find_rows(n: Any, parent_tag: str = '') -> None:
            tag = getattr(n, 'tag', '')
            if tag == 'tr':
                # Wiersz jest nagłówkowy jeśli:
                #   a) należy do <thead>, lub
                #   b) zawiera komórki <th>
                is_header = (parent_tag == 'thead') or _has_th(n)
                rows.append((n, is_header))
            else:
                for c in getattr(n, 'children', []):
                    find_rows(c, tag)

        find_rows(table_node)
        if not rows:
            return None

        row_labels = []
        for row_idx, (row_node, is_header) in enumerate(rows):
            row_label   = f"{tbl_label}_row{row_idx}"
            row_T_boost = 10.0 if is_header else 0.0
            row_ctx     = ctx.child()

            cell_atoms = []
            for cell_node in _find_by_tag(row_node, {"td", "th"}):
                text = _plain_text(cell_node).strip()
                if not text:
                    continue
                c_label = row_ctx.next_id("cel")
                c_role  = "th" if is_header else "td"
                c_T     = _semantic_T("p", row_ctx.depth) + row_T_boost
                aid     = self._make_atom(c_label,
                                          S=f"cell:{c_role}",
                                          E=text[:128],
                                          T=c_T)
                if aid:
                    cell_atoms.append(aid)
                    ctx.atom_ids.append(aid)

            if cell_atoms:
                row_labels.append(row_label)
                ctx.bubble_lbls.append(row_label)
                self._make_bubble(row_label, cell_atoms)

        if row_labels:
            ctx.bubble_lbls.append(tbl_label)
            self._make_bubble(tbl_label, row_labels)

        return tbl_label

    # ── Mapowanie listy ───────────────────────────────────────────────────────

    def _map_list(self, list_node: Any, ctx: _MapCtx) -> Optional[str]:
        """
        Lista → bąbel elementów:
          list_bubble
            item_atom_0  (S="li", E=tekst)
            item_atom_1
        """
        lst_label  = ctx.next_id("lst")
        item_atoms = []
        child_ctx  = ctx.child()

        for item_node in _find_by_tag(list_node, {"li"}):
            text  = _plain_text(item_node).strip()
            if not text:
                continue
            label = child_ctx.next_id("li")
            T     = _semantic_T("li", child_ctx.depth)
            aid   = self._make_atom(label, S="li", E=text[:self.MAX_E_LEN], T=T)
            if aid:
                item_atoms.append(aid)
                ctx.atom_ids.append(aid)

        if item_atoms:
            ctx.bubble_lbls.append(lst_label)
            self._make_bubble(lst_label, item_atoms)
            return lst_label
        return None

    # ── Mapowanie bloku jako bąbla ────────────────────────────────────────────

    def _map_block_bubble(self, block_node: Any, ctx: _MapCtx,
                          tag: str) -> Optional[str]:
        """
        Blok (p, article, section) → bąbel atomów zdań.
        Każde zdanie to osobny atom — bąbel je grupuje.
        """
        text = _plain_text(block_node).strip()
        if not text:
            # Brak tekstu — może ma zagnieżdżone bloki
            for c in getattr(block_node, "children", []):
                self._walk(c, ctx)
            return None

        bubble_label = ctx.next_id(tag[:3])

        # Podziel na zdania i stwórz atomy
        sentences = _split_sentences(text)
        sent_atoms = []
        T_base     = _semantic_T(tag if tag in T_BASE else "p", ctx.depth)

        for i, sent in enumerate(sentences):
            sent = sent.strip()
            if not sent:
                continue
            s_label = ctx.next_id("s")
            # Pierwsze zdanie w akapicie ważniejsze — gradient T
            T_sent = max(T_MIN_PAGE, T_base - i * 1.5)
            aid    = self._make_atom(s_label,
                                     S=f"sent:{tag}",
                                     E=sent[:self.MAX_E_LEN],
                                     T=T_sent)
            if aid:
                sent_atoms.append(aid)
                ctx.atom_ids.append(aid)

        if sent_atoms:
            ctx.bubble_lbls.append(bubble_label)
            self._make_bubble(bubble_label, sent_atoms)
            return bubble_label
        return None

    # ── Hologram dokumentu ────────────────────────────────────────────────────

    def _make_page_hologram(self, page: Any, ctx: _MapCtx,
                             prefix: str) -> Optional[str]:
        """
        Tworzy hologram całego dokumentu.
        Prototyp = atom tytułu (najbardziej reprezentatywny).
        Generatory = atomy nagłówków (szkielet strony).
        """
        # Znajdź atomy nagłówków jako generatory
        heading_ids = [aid for aid in ctx.atom_ids
                       if "h1" in aid or "h2" in aid or "h3" in aid]

        # Prototyp = atom tytułu (pierwsza pozycja w atom_ids)
        prototype_id = ctx.atom_ids[0] if ctx.atom_ids else None

        try:
            if hasattr(self.runtime, "archive_to_hologram"):
                # Używaj natywnego archive_to_hologram
                hid = self.runtime.archive_to_hologram(
                    topic   = f"page:{page.title[:48]}",
                    atom_ids= heading_ids[:10] or ctx.atom_ids[:10],
                    remove_originals=False,
                )
                return hid
            else:
                # Fallback: zapisz jako bąbel dokumentu
                doc_label = f"{prefix}_doc"
                self._make_bubble(doc_label, ctx.atom_ids[:50])
                ctx.bubble_lbls.append(doc_label)
                return doc_label
        except Exception:
            return None

    # ── Tworzenie bytów phi-space ─────────────────────────────────────────────

    def _make_atom(self, label: str, S: str, E: str, T: float) -> Optional[str]:
        """
        Tworzy lub aktualizuje atom w phi-space.
        Idempotentne: jeśli atom istnieje, aktualizuje T.
        """
        try:
            if self.runtime.matrix.has_atom(label):
                atom = self.runtime.get_atom(label)
                if atom:
                    # DOM diff przez T: aktualizacja = przegrzanie
                    atom.T     = T
                    atom.state = _state_for_T(T)
                return label
            else:
                self.runtime.create_atom(label, S, E, T)
                return label
        except Exception:
            return None

    def _make_bubble(self, label: str, atom_ids: List[str]) -> bool:
        """Konsoliduje atomy do bąbla."""
        try:
            if self.runtime.get_bubble(label) is None:
                # Konsoliduj pierwszy atom jako kotew bąbla
                if atom_ids and self.runtime.matrix.has_atom(atom_ids[0]):
                    self.runtime.consolidate(atom_ids[0])
            bubble = self.runtime.get_bubble(label)
            if bubble is None:
                return False
            # Importuj pozostałe atomy do bąbla
            for aid in atom_ids[1:]:
                if self.runtime.matrix.has_atom(aid):
                    try:
                        self.runtime.import_to_bubble(label, aid)
                    except Exception:
                        pass
            return True
        except Exception:
            return False

    # ── Zapytania phi-space ───────────────────────────────────────────────────

    def reader_mode(self, url: str,
                    T_threshold: float = 60.0) -> List[Any]:
        """
        Zwraca atomy strony z T >= T_threshold.
        Reader mode: boilerplate (footer/nav T=20) naturalnie odpada.
        """
        atom_ids = self._page_atoms.get(url, [])
        result   = []
        for aid in atom_ids:
            try:
                atom = self.runtime.get_atom(aid)
                if atom and atom.T >= T_threshold:
                    result.append(atom)
            except Exception:
                pass
        return result

    def find_in_page(self, url: str, query: str) -> List[Tuple[str, str, float]]:
        """
        Szuka query w atomach strony.
        Zwraca [(atom_id, E_fragment, T), ...] posortowane wg T desc.
        """
        atom_ids = self._page_atoms.get(url, [])
        q        = query.lower()
        hits     = []
        for aid in atom_ids:
            try:
                atom = self.runtime.get_atom(aid)
                if atom and q in (atom.E or "").lower():
                    hits.append((aid, atom.E[:128], atom.T))
            except Exception:
                pass
        hits.sort(key=lambda x: -x[2])
        return hits

    def find_cross_page(self, query: str) -> List[Tuple[str, str, str, float]]:
        """
        Szuka query we wszystkich zaindeksowanych stronach.
        Zwraca [(url, atom_id, fragment, T), ...] posortowane wg T desc.
        """
        q    = query.lower()
        hits = []
        for url, atom_ids in self._page_atoms.items():
            for aid in atom_ids:
                try:
                    atom = self.runtime.get_atom(aid)
                    if atom and q in (atom.E or "").lower():
                        hits.append((url, aid, atom.E[:128], atom.T))
                except Exception:
                    pass
        hits.sort(key=lambda x: -x[3])
        return hits

    def page_outline(self, url: str) -> List[Tuple[str, str, float]]:
        """
        Szkielet strony: tylko atomy nagłówków posortowane wg T.
        Zwraca [(atom_id, tekst, T), ...].
        """
        atom_ids = self._page_atoms.get(url, [])
        outline  = []
        for aid in atom_ids:
            try:
                atom = self.runtime.get_atom(aid)
                if atom and atom.S and atom.S.startswith("heading:"):
                    outline.append((aid, atom.E, atom.T))
            except Exception:
                pass
        outline.sort(key=lambda x: -x[2])
        return outline

    def stats(self, url: str) -> Dict[str, Any]:
        """Statystyki mapowania strony."""
        atom_ids   = self._page_atoms.get(url, [])
        bubble_lbs = self._page_bubbles.get(url, [])
        mapped_at  = self._mapped_at.get(url, 0)
        atoms = []
        for aid in atom_ids:
            try:
                a = self.runtime.get_atom(aid)
                if a:
                    atoms.append(a)
            except Exception:
                pass
        T_vals = [a.T for a in atoms]
        return {
            "url":        url,
            "atoms":      len(atoms),
            "bubbles":    len(bubble_lbs),
            "hologram":   self._page_holos.get(url, ""),
            "T_mean":     sum(T_vals) / len(T_vals) if T_vals else 0,
            "T_max":      max(T_vals) if T_vals else 0,
            "T_min":      min(T_vals) if T_vals else 0,
            "mapped_age": time.time() - mapped_at if mapped_at else None,
            "indexed":    len(self._page_atoms),
        }


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _clean_text(text: str) -> str:
    """Normalizuje whitespace, usuwa ANSI."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)   # usuń ANSI
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _plain_text(node: Any) -> str:
    """Rekurencyjnie zbiera tekst z węzła (jak get_plain_text)."""
    if hasattr(node, "get_plain_text"):
        return _clean_text(node.get_plain_text())
    typ = getattr(node, "typ", -1)
    if typ == 8:  # NODE_TEXT
        return _clean_text(getattr(node, "text", ""))
    parts = []
    for c in getattr(node, "children", []):
        parts.append(_plain_text(c))
    return _clean_text(" ".join(parts))


def _find_by_tag(node: Any, tags: set) -> List[Any]:
    """Płytkie wyszukanie bezpośrednich dzieci o danych tagach."""
    result = []
    for c in getattr(node, "children", []):
        if getattr(c, "tag", "") in tags:
            result.append(c)
        else:
            # Szukaj głębiej (thead, tbody)
            result.extend(_find_by_tag(c, tags))
    return result


def _has_th(row_node: Any) -> bool:
    """Sprawdza czy wiersz zawiera komórki <th>."""
    return any(getattr(c, "tag", "") == "th"
               for c in getattr(row_node, "children", []))


def _split_sentences(text: str) -> List[str]:
    """Dzieli tekst na zdania po ., !, ? — każde zdanie = atom w bąblu."""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    return parts if parts else [text]


def _state_for_T(T: float) -> str:
    if T >= 70: return "HOT"
    if T >= 30: return "WARM"
    if T >= 10: return "COLD"
    return "TOMB"


# ─── Integracja z karmazyn_browser ───────────────────────────────────────────

def attach_to_browser(browser: Any, runtime: Any) -> "DOMMapper":
    """
    Dołącza DOMMapper do KarmazynBrowser.
    Po każdym go() browser.dom_mapper.map_page(page) jest wywoływany.
    Zwraca mapper (browser.dom_mapper).
    """
    mapper = DOMMapper(runtime)
    browser.dom_mapper = mapper

    # Monkey-patch _load_url żeby mapował po załadowaniu
    original_load = browser._load_url

    def _patched_load(url, add_to_history=True, force_reload=False):
        ok, content = original_load(url, add_to_history, force_reload)
        if ok and browser._current is not None:
            try:
                mapper.map_page(browser._current)
            except Exception:
                pass
        return ok, content

    browser._load_url = _patched_load
    return mapper


# ─── Komenda shella ───────────────────────────────────────────────────────────

def cmd_dom(args, browser: Any, mapper: "DOMMapper") -> str:
    """
    DOM                     — statystyki phi-space aktualnej strony
    DOM MAP                 — (re)mapuj aktualną stronę
    DOM OUTLINE             — szkielet strony (nagłówki jako atomy)
    DOM READER [T]          — atomy powyżej progu T (domyślnie 60)
    DOM FIND <query>        — szukaj w atomach aktualnej strony
    DOM PHI <query>         — szukaj we wszystkich zaindeksowanych stronach
    DOM STATS               — szczegółowe statystyki mappera
    """
    if not args:
        if browser._current is None:
            return "Brak strony. Najpierw BROWSE <url>"
        s = mapper.stats(browser._current.url)
        if s["atoms"] == 0:
            return ("Strona nie jest zaindeksowana w phi-space.\n"
                    "Użyj: DOM MAP")
        lines = [
            f"URL:     {browser._current.url[:60]}",
            f"Atomy:   {s['atoms']}",
            f"Bąble:   {s['bubbles']}",
            f"Hologram:{s['hologram'] or 'brak'}",
            f"T śr:    {s['T_mean']:.1f}  max={s['T_max']:.1f}  min={s['T_min']:.1f}",
            f"Wiek:    {s['mapped_age']:.0f}s" if s['mapped_age'] else "Wiek: nieznany",
            f"Stron:   {s['indexed']} zaindeksowanych",
        ]
        return "\n".join(lines)

    sub = args[0].upper()

    if sub == "MAP":
        if browser._current is None:
            return "Brak strony."
        # Force remap — wyczyść cache timestampu
        url = browser._current.url
        mapper._mapped_at.pop(url, None)
        hid = mapper.map_page(browser._current)
        s   = mapper.stats(url)
        return (f"Zmapowano: {s['atoms']} atomów, "
                f"{s['bubbles']} bąbli, hologram={hid or 'brak'}")

    if sub == "OUTLINE":
        if browser._current is None:
            return "Brak strony."
        outline = mapper.page_outline(browser._current.url)
        if not outline:
            return "Brak nagłówków w phi-space. DOM MAP najpierw."
        lines = [f"Szkielet ({len(outline)} nagłówków):"]
        for aid, text, T in outline:
            state = _state_for_T(T)
            lines.append(f"  [{state[0]}] T={T:4.0f}  {text[:60]}")
        return "\n".join(lines)

    if sub == "READER":
        if browser._current is None:
            return "Brak strony."
        try:
            T_thr = float(args[1]) if len(args) > 1 else 60.0
        except ValueError:
            return f"Nieprawidłowa temperatura: '{args[1]}'. Podaj liczbę, np. DOM READER 60"
        atoms = mapper.reader_mode(browser._current.url, T_thr)
        if not atoms:
            return f"Brak atomów z T >= {T_thr}. DOM MAP najpierw."
        lines = [f"Reader mode (T >= {T_thr:.0f}), {len(atoms)} atomów:\n",
                 "─" * 60]
        for atom in atoms:
            lines.append(atom.E[:80] if hasattr(atom, "E") else str(atom))
        return "\n".join(lines)

    if sub == "FIND":
        if browser._current is None:
            return "Brak strony."
        if len(args) < 2:
            return "DOM FIND <query>"
        query = " ".join(args[1:])
        hits  = mapper.find_in_page(browser._current.url, query)
        if not hits:
            return f"Nie znaleziono '{query}' w phi-space. DOM MAP najpierw."
        lines = [f"'{query}' — {len(hits)} atomów (wg T):"]
        for aid, fragment, T in hits[:15]:
            lines.append(f"  T={T:4.0f}  {fragment[:70]}")
        if len(hits) > 15:
            lines.append(f"  ... i {len(hits)-15} więcej")
        return "\n".join(lines)

    if sub == "PHI":
        if len(args) < 2:
            return "DOM PHI <query>"
        query = " ".join(args[1:])
        hits  = mapper.find_cross_page(query)
        if not hits:
            return f"Nie znaleziono '{query}' w żadnej zaindeksowanej stronie."
        lines = [f"Phi-space '{query}' — {len(hits)} trafień:"]
        seen_urls = set()
        for url, aid, fragment, T in hits[:20]:
            domain = url.split("/")[2] if "/" in url else url
            if domain not in seen_urls:
                seen_urls.add(domain)
                lines.append(f"\n  {domain}:")
            lines.append(f"    T={T:4.0f}  {fragment[:65]}")
        return "\n".join(lines)

    if sub == "STATS":
        indexed = list(mapper._page_atoms.keys())
        if not indexed:
            return "Brak zaindeksowanych stron."
        lines = [f"DOMMapper — {len(indexed)} stron:"]
        for url in indexed[-10:]:
            s = mapper.stats(url)
            age = f"{s['mapped_age']:.0f}s" if s['mapped_age'] else "?"
            lines.append(
                f"  {s['atoms']:3}at {s['bubbles']:2}bb  "
                f"T̄={s['T_mean']:4.0f}  [{age}]  {url[:45]}"
            )
        return "\n".join(lines)

    return cmd_dom([], browser, mapper)