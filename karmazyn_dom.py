"""
karmazyn_dom.py — DOM → Phi-Space Mapper KarmazynOS v1.4
=========================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Zmiany v1.4:
  - Przebudowa komendy READER: Zamiast debugowego zrzutu, komenda działa
    teraz jako pełnoprawny Reader Mode. Zmieniono domyślny próg temperatury
    na 30 (WARM), wprowadzono zawijanie tekstu i wygłuszenie surowych linków.

Zmiany v1.3:
  - Naprawiono _map_block_bubble: kontenery <div>/<article> nie spłaszczają
    już swoich dzieci do czystego tekstu.
"""

import hashlib
import re
import time
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    from karmazyn_browser import (
        ParsedPage, SemanticNode, NodeType, ANSI_RE,
    )
    HAS_BROWSER = True
except ImportError:
    HAS_BROWSER = False

try:
    from luneta_text import is_readable_text, looks_like_decode_error
    HAS_TEXT_UTILS = True
except ImportError:
    HAS_TEXT_UTILS = False
    def is_readable_text(text, max_bad_ratio=0.15):
        return bool(text and text.strip())
    def looks_like_decode_error(text):
        return False

__all__ = ['DOMMapper', 'cmd_dom']

T_BASE: Dict[str, float] = {
    "heading:h1": 90.0, "heading:h2": 82.0, "heading:h3": 75.0,
    "heading:h4": 68.0, "heading:h5": 62.0, "heading:h6": 55.0,
    "pre":        78.0, "code":       75.0, "table":      70.0,
    "li":         65.0, "link":       62.0,
    "p":          55.0, "blockquote": 58.0, "article":    60.0,
    "footer":     20.0, "nav":        22.0, "aside":      28.0,
    "header":     35.0,
}

T_DEPTH_DECAY = 2.0
T_MIN_PAGE = 10.0
T_TITLE = 95.0

def _semantic_T(role: str, depth: int = 0) -> float:
    base = T_BASE.get(role, 50.0)
    decay = min(depth * T_DEPTH_DECAY, base - T_MIN_PAGE)
    return max(T_MIN_PAGE, base - decay)

@dataclass
class _MapCtx:
    prefix:     str
    seq:        List[int]
    depth:      int = 0
    in_boiler:  bool = False
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

class DOMMapper:
    BOILERPLATE_TAGS = {"footer", "nav", "aside", "header"}
    BUBBLE_TAGS = {"p", "li", "blockquote", "article",
                   "section", "div", "td", "th"}
    MAX_E_LEN = 256

    def __init__(self, runtime):
        self.runtime = runtime
        self._page_atoms:   Dict[str, List[str]] = {}
        self._page_holos:   Dict[str, str]       = {}
        self._page_bubbles: Dict[str, List[str]] = {}
        self._mapped_at:    Dict[str, float]     = {}
        self._mapped_tree:  Dict[str, int]       = {}

    def map_page(self, page: Any) -> Optional[str]:
        url = page.url
        tree = getattr(page, "semantic_tree", None)
        if tree is None: return None

        if getattr(page, "decode_failed", False): return None
        if looks_like_decode_error(getattr(page, "title", "") or ""): return None

        mapped_at = self._mapped_at.get(url, 0)
        # Pomijamy ponowne mapowanie tylko gdy to TO SAMO drzewo (ten sam obiekt).
        # Po re-parsie (nawigacja/odswiezenie) drzewo jest nowe i trzeba je ponownie
        # ostemplowac _atom_refs, inaczej hover-heat nie ma czego grzac.
        if id(tree) == self._mapped_tree.get(url) and time.time() - mapped_at < 300:
            return self._page_holos.get(url)

        page_hash = hashlib.sha1(url.encode()).hexdigest()[:10]
        prefix    = f"dom_{page_hash}"
        ctx       = _MapCtx(prefix=prefix, seq=[0])

        title_id = self._make_atom(
            label = f"{prefix}_title", S = "title", E = page.title[:self.MAX_E_LEN], T = T_TITLE,
        )
        if title_id: ctx.atom_ids.append(title_id)

        self._walk(tree, ctx)
        hid = self._make_page_hologram(page, ctx, prefix)

        self._page_atoms[url]   = ctx.atom_ids
        self._page_bubbles[url] = ctx.bubble_lbls
        self._page_holos[url]   = hid or ""
        self._mapped_at[url]    = time.time()
        self._mapped_tree[url]  = id(tree)

        return hid

    def _walk(self, node: Any, ctx: _MapCtx) -> Optional[str]:
        if node is None: return None

        typ = getattr(node, "typ", -1)
        tag = getattr(node, "tag", "")
        children = getattr(node, "children", [])

        NODE_TEXT = 8; NODE_HEADING = 3; NODE_LINK = 4; NODE_HR = 9; NODE_PRE = 7
        NODE_TABLE = 6; NODE_LIST = 5; NODE_BLOCK = 1; NODE_DOCUMENT = 0

        if typ == NODE_TEXT:
            text = _clean_text(getattr(node, "text", ""))
            if not text: return None
            role  = "footer" if ctx.in_boiler else "p"
            T_base = _semantic_T(role, ctx.depth)
            
            sentences = _split_sentences(text)
            sent_atoms = []
            for i, sent in enumerate(sentences):
                sent = sent.strip()
                if not sent: continue
                T_sent = max(T_MIN_PAGE, T_base - i * 1.5)
                label = ctx.next_id("txt")
                aid = self._make_atom(label, S=f"sent:{role}", E=sent[:self.MAX_E_LEN], T=T_sent)
                if aid:
                    sent_atoms.append(aid)
                    ctx.atom_ids.append(aid)
            
            if sent_atoms:
                setattr(node, "_atom_refs", list(sent_atoms))
            if len(sent_atoms) == 1: return sent_atoms[0]
            elif len(sent_atoms) > 1:
                bubble_label = ctx.next_id("txtb")
                ctx.bubble_lbls.append(bubble_label)
                self._make_bubble(bubble_label, sent_atoms)
                return bubble_label
            return None

        if typ == NODE_HEADING:
            level = tag[1] if len(tag) > 1 and tag[1].isdigit() else "1"
            text  = _plain_text(node)
            if not text: return None
            role  = f"heading:h{level}"
            T     = _semantic_T(role, ctx.depth)
            label = ctx.next_id(f"h{level}")
            aid   = self._make_atom(label, S=role, E=text, T=T)
            if aid:
                ctx.atom_ids.append(aid)
                setattr(node, "_atom_refs", [aid])
            return aid

        if typ == NODE_LINK:
            href = getattr(node, "attrs", {}).get("href", "")
            text = _plain_text(node)
            if not href or not text:
                for c in children: self._walk(c, ctx.child())
                return None
            T     = _semantic_T("link", ctx.depth)
            label = ctx.next_id("lnk")
            aid   = self._make_atom(label, S=f"link:{text[:64]}", E=href, T=T)
            if aid:
                ctx.atom_ids.append(aid)
                setattr(node, "_atom_refs", [aid])
            return aid

        if typ == NODE_PRE:
            text  = _plain_text(node)
            if not text.strip(): return None
            T     = _semantic_T("pre", ctx.depth)
            label = ctx.next_id("pre")
            aid   = self._make_atom(label, S="code", E=text[:self.MAX_E_LEN], T=T)
            if aid:
                ctx.atom_ids.append(aid)
                setattr(node, "_atom_refs", [aid])
            return aid

        if typ == NODE_HR: return None

        if typ == NODE_TABLE: return self._map_table(node, ctx)
        if typ == NODE_LIST: return self._map_list(node, ctx)

        if typ in (NODE_BLOCK, NODE_DOCUMENT):
            is_boiler = tag in self.BOILERPLATE_TAGS
            child_ctx = ctx.child(boilerplate=is_boiler)
            if tag in self.BUBBLE_TAGS:
                return self._map_block_bubble(node, child_ctx, tag)
            for c in children: self._walk(c, child_ctx)
            return None

        for c in children: self._walk(c, ctx.child())
        return None

    def _map_table(self, table_node: Any, ctx: _MapCtx) -> Optional[str]:
        tbl_label = ctx.next_id("tbl")
        rows: List[Tuple[Any, bool]] = []
        def find_rows(n: Any, parent_tag: str = '') -> None:
            tag = getattr(n, 'tag', '')
            if tag == 'tr':
                is_header = (parent_tag == 'thead') or _has_th(n)
                rows.append((n, is_header))
            else:
                for c in getattr(n, 'children', []): find_rows(c, tag)

        find_rows(table_node)
        if not rows: return None

        row_labels = []
        for row_idx, (row_node, is_header) in enumerate(rows):
            row_label   = f"{tbl_label}_row{row_idx}"
            row_T_boost = 10.0 if is_header else 0.0
            row_ctx     = ctx.child()
            cell_atoms = []
            for cell_node in _find_by_tag(row_node, {"td", "th"}):
                text = _plain_text(cell_node).strip()
                if not text: continue
                c_label = row_ctx.next_id("cel")
                c_role  = "th" if is_header else "td"
                c_T     = _semantic_T("p", row_ctx.depth) + row_T_boost
                aid     = self._make_atom(c_label, S=f"cell:{c_role}", E=text[:128], T=c_T)
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

    def _map_list(self, list_node: Any, ctx: _MapCtx) -> Optional[str]:
        lst_label  = ctx.next_id("lst")
        item_atoms = []
        child_ctx  = ctx.child()
        for item_node in _find_by_tag(list_node, {"li"}):
            text  = _plain_text(item_node).strip()
            if not text: continue
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

    def _map_block_bubble(self, block_node: Any, ctx: _MapCtx, tag: str) -> Optional[str]:
        bubble_label = ctx.next_id(tag[:3])
        child_ids = []
        for c in getattr(block_node, "children", []):
            res_id = self._walk(c, ctx)
            if res_id:
                child_ids.append(res_id)

        if child_ids:
            ctx.bubble_lbls.append(bubble_label)
            self._make_bubble(bubble_label, child_ids)
            return bubble_label
        return None

    def _make_page_hologram(self, page: Any, ctx: _MapCtx, prefix: str) -> Optional[str]:
        heading_ids = [aid for aid in ctx.atom_ids if "h1" in aid or "h2" in aid or "h3" in aid]
        try:
            if hasattr(self.runtime, "archive_to_hologram"):
                return self.runtime.archive_to_hologram(
                    topic   = f"page:{page.title[:48]}",
                    atom_ids= heading_ids[:10] or ctx.atom_ids[:10],
                    remove_originals=False,
                )
            else:
                doc_label = f"{prefix}_doc"
                self._make_bubble(doc_label, ctx.atom_ids[:50])
                ctx.bubble_lbls.append(doc_label)
                return doc_label
        except Exception: return None

    def _make_atom(self, label: str, S: str, E: str, T: float) -> Optional[str]:
        check = E
        if isinstance(S, str) and S.startswith("link:"): check = S[5:]
        if check and not is_readable_text(check): return None
        try:
            if self.runtime.matrix.has_atom(label):
                atom = self.runtime.get_atom(label)
                if atom:
                    delta = T - atom.T
                    if delta > 0: atom.heat(delta)
                    elif delta < 0: atom.cool(-delta)
                return label
            else:
                self.runtime.create_atom(label, S, E, T)
                return label
        except Exception: return None

    def _make_bubble(self, label: str, atom_ids: List[str]) -> bool:
        try:
            valid = [aid for aid in atom_ids if self.runtime.matrix.has_atom(aid) or self.runtime.get_bubble(aid) is not None]
            if not valid: return False
            if self.runtime.get_bubble(label) is None:
                if hasattr(self.runtime, "create_bubble"):
                    self.runtime.create_bubble(label, valid)
                else:
                    anchor = valid[0]
                    self.runtime.consolidate(anchor)
                    label = anchor
                    for aid in valid[1:]:
                        try: self.runtime.import_to_bubble(label, aid)
                        except Exception: pass
                    return True
            else:
                for aid in valid:
                    try: self.runtime.import_to_bubble(label, aid)
                    except Exception: pass
            return self.runtime.get_bubble(label) is not None
        except Exception: return False

    def reader_mode(self, url: str, T_threshold: float = 30.0) -> List[Any]:
        atom_ids = self._page_atoms.get(url, [])
        result   = []
        for aid in atom_ids:
            try:
                atom = self.runtime.get_atom(aid)
                if atom and atom.T >= T_threshold: result.append(atom)
            except Exception: pass
        return result

    def find_in_page(self, url: str, query: str) -> List[Tuple[str, str, float]]:
        atom_ids = self._page_atoms.get(url, [])
        q, hits = query.lower(), []
        for aid in atom_ids:
            try:
                atom = self.runtime.get_atom(aid)
                if atom and q in (atom.E or "").lower():
                    hits.append((aid, atom.E[:128], atom.T))
            except Exception: pass
        hits.sort(key=lambda x: -x[2])
        return hits

    def find_cross_page(self, query: str) -> List[Tuple[str, str, str, float]]:
        q, hits = query.lower(), []
        for url, atom_ids in self._page_atoms.items():
            for aid in atom_ids:
                try:
                    atom = self.runtime.get_atom(aid)
                    if atom and q in (atom.E or "").lower():
                        hits.append((url, aid, atom.E[:128], atom.T))
                except Exception: pass
        hits.sort(key=lambda x: -x[3])
        return hits

    def page_outline(self, url: str) -> List[Tuple[str, str, float]]:
        atom_ids = self._page_atoms.get(url, [])
        outline  = []
        for aid in atom_ids:
            try:
                atom = self.runtime.get_atom(aid)
                if atom and atom.S and atom.S.startswith("heading:"):
                    outline.append((aid, atom.E, atom.T))
            except Exception: pass
        outline.sort(key=lambda x: -x[2])
        return outline

    def stats(self, url: str) -> Dict[str, Any]:
        atom_ids   = self._page_atoms.get(url, [])
        bubble_lbs = self._page_bubbles.get(url, [])
        mapped_at  = self._mapped_at.get(url, 0)
        atoms = [self.runtime.get_atom(aid) for aid in atom_ids if self.runtime.get_atom(aid)]
        T_vals = [a.T for a in atoms]
        return {
            "url":        url, "atoms":      len(atoms), "bubbles":    len(bubble_lbs),
            "hologram":   self._page_holos.get(url, ""),
            "T_mean":     sum(T_vals) / len(T_vals) if T_vals else 0,
            "T_max":      max(T_vals) if T_vals else 0,
            "T_min":      min(T_vals) if T_vals else 0,
            "mapped_age": time.time() - mapped_at if mapped_at else None,
            "indexed":    len(self._page_atoms),
        }

def _clean_text(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return re.sub(r"\s+", " ", text).strip()

def _plain_text(node: Any) -> str:
    if hasattr(node, "get_plain_text"): return _clean_text(node.get_plain_text())
    if getattr(node, "typ", -1) == 8: return _clean_text(getattr(node, "text", ""))
    return _clean_text(" ".join(_plain_text(c) for c in getattr(node, "children", [])))

def _find_by_tag(node: Any, tags: set) -> List[Any]:
    result = []
    for c in getattr(node, "children", []):
        if getattr(c, "tag", "") in tags: result.append(c)
        else: result.extend(_find_by_tag(c, tags))
    return result

def _has_th(row_node: Any) -> bool:
    return any(getattr(c, "tag", "") == "th" for c in getattr(row_node, "children", []))

def _split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    return parts if parts else [text]

def _state_for_T(T: float) -> str:
    try:
        from karmazyn_atom import state_for_T
        return state_for_T(T)
    except ImportError:
        if T >= 70: return "HOT"
        if T >= 30: return "WARM"
        if T >= 2.0: return "COLD"
        return "TOMB"

def attach_to_browser(browser: Any, runtime: Any) -> "DOMMapper":
    mapper = DOMMapper(runtime)
    browser.dom_mapper = mapper
    original_load = browser._load_url
    def _patched_load(url, add_to_history=True, force_reload=False):
        ok, content = original_load(url, add_to_history, force_reload)
        if ok and browser._current is not None:
            try: mapper.map_page(browser._current)
            except Exception: pass
        return ok, content
    browser._load_url = _patched_load
    return mapper

def cmd_dom(args, browser: Any, mapper: "DOMMapper") -> str:
    if not args:
        if browser._current is None: return "Brak strony. Najpierw BROWSE <url>"
        s = mapper.stats(browser._current.url)
        if s["atoms"] == 0: return ("Strona nie jest zaindeksowana w phi-space.\nUżyj: DOM MAP")
        lines = [
            f"URL:     {browser._current.url[:60]}",
            f"Atomy:   {s['atoms']}  | Bąble: {s['bubbles']}",
            f"Hologram:{s['hologram'] or 'brak'}",
            f"T śr:    {s['T_mean']:.1f}  max={s['T_max']:.1f}  min={s['T_min']:.1f}",
        ]
        return "\n".join(lines)
    
    sub = args[0].upper()
    
    if sub == "MAP":
        if browser._current is None: return "Brak strony."
        mapper._mapped_at.pop(browser._current.url, None)
        hid = mapper.map_page(browser._current)
        s = mapper.stats(browser._current.url)
        return f"Zmapowano: {s['atoms']} atomów, {s['bubbles']} bąbli, hologram={hid or 'brak'}"
        
    if sub == "OUTLINE":
        if browser._current is None: return "Brak strony."
        outline = mapper.page_outline(browser._current.url)
        if not outline: return "Brak nagłówków w phi-space. DOM MAP najpierw."
        return "\n".join([f"Szkielet ({len(outline)} nagłówków):"] + [f"  [{_state_for_T(T)[0]}] T={T:4.0f}  {text[:60]}" for aid, text, T in outline])
        
    if sub == "READER":
        if browser._current is None:
            return "Brak strony."
        try:
            T_thr = float(args[1]) if len(args) > 1 else 30.0
        except ValueError:
            return f"Nieprawidłowa temperatura: '{args[1]}'. Podaj liczbę, np. DOM READER 30"
            
        atoms = mapper.reader_mode(browser._current.url, T_thr)
        if not atoms:
            return f"Brak atomów z T >= {T_thr}. Użyj DOM MAP."
            
        width = getattr(browser, 'width', 80)
        lines = [f"\n{'═' * width}", 
                 f"  TRYB CZYTNIKA (Filtrowanie szumu: Atomy T >= {T_thr:.0f})", 
                 f"{'═' * width}\n"]
                 
        for atom in atoms:
            if atom.S and atom.S.startswith("link:"):
                continue
                
            text = atom.E if hasattr(atom, "E") else str(atom)
            if not text.strip(): 
                continue
                
            if atom.S and atom.S.startswith("heading:"):
                lines.append(f"\n{text.upper()}")
                lines.append("─" * min(len(text), width))
            else:
                wrapped = textwrap.wrap(text, width=width)
                lines.extend(wrapped)
                lines.append("")
                
        lines.append(f"{'═' * width}")
        return "\n".join(lines)
        
    return "DOM: Użyj MAP | OUTLINE | READER"