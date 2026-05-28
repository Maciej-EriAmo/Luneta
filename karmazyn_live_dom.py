"""
karmazyn_live_dom.py — Dynamiczny DOM dla KarmazynJS v1.2
=========================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Każdy węzeł DOM = atom phi-space.
Każdy kontener DOM = bąbel phi-space.
Mutacja DOM = zmiana temperatury atomów.

Integracja z karmazyn_js_core.py (v1.0+):
  - vm.set_global("document", live_dom)
  - prop handler w Core obsługuje Python objects przez getattr
  - JS Function callbacks w addEventListener wywołane przez vm._call()

Zmiany v1.2 (port na nowe API):
  - Używa karmazyn_js_core.KarmazynJSCore / KarmazynJSPhi
  - bind_live_dom: vm.set_global() zamiast vm.global_bubble.set_local()
  - LiveDOM przechowuje referencję do VM dla wywołań JS Function callbacks
  - addEventListener: JS Function obiekty wrapowane przez vm._call()
  - Pełne zarządzanie bąblami (appendChild, removeChild, insertBefore)
  - querySelector / getElementsByTagName / getElementById
  - dispatchEvent z bubbling
  - Buforowanie aktualizacji bąbla (flush po zakończeniu skryptu)
  - on_mutate() callback dla renderera Lunety

Architektura:
  LiveDOM  — odpowiednik `document` w JS
  LiveNode — węzeł DOM + atom phi-space

  JS wywołuje:
    document.createElement("div")         → LiveNode
    div.setAttribute("id", "header")      → sync atom
    div.appendChild(child)                → sync bąbel
    div.addEventListener("click", fn)     → fn = JS Function lub Python callable
    div.dispatchEvent("click")            → vm._call(fn, [event])
"""

import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set

# NodeType zgodne z karmazyn_browser
NODE_BLOCK = 1
NODE_TEXT  = 8

SUPPORTED_EVENTS = {"click", "keydown", "keyup", "load", "change", "input"}

# Importy silnika — opcjonalne (duck typing dla testów)
try:
    from karmazyn_js_core import Function as JSFunction
    HAS_CORE = True
except ImportError:
    HAS_CORE = False
    JSFunction = type(None)


# ─── LiveNode ─────────────────────────────────────────────────────────────────

class LiveNode:
    """
    Węzeł DOM zintegrowany z phi-space.
    Każdy węzeł = atom w RUNTIME.matrix.
    Każdy kontener z dziećmi = bąbel w RUNTIME.

    Temperatura atomu rośnie przy każdej interakcji JS.
    Usunięty węzeł stygnię do TOMB (naturalny GC).
    """

    __slots__ = (
        "document", "typ", "tag", "text", "attrs",
        "children", "parent", "_listeners",
        "phi_id", "_dirty",
    )

    def __init__(self, document: "LiveDOM",
                 typ: int, tag: str = "", text: str = ""):
        self.document  = document
        self.typ       = typ
        self.tag       = tag.lower()
        self.text      = text
        self.attrs:    Dict[str, str]           = {}
        self.children: List["LiveNode"]         = []
        self.parent:   Optional["LiveNode"]     = None
        self._listeners: Dict[str, List[Any]]   = {}
        self._dirty    = True

        # Unikalny ID atomu w phi-space
        self.phi_id = f"live_{document.prefix}_{uuid.uuid4().hex[:8]}"
        self._sync_atom()

    # ── Właściwości (dostępne z JS przez prop handler) ────────────────────────

    @property
    def textContent(self) -> str:
        if self.typ == NODE_TEXT:
            return self.text
        return "".join(c.textContent for c in self.children)

    @textContent.setter
    def textContent(self, value: str) -> None:
        self.setTextContent(value)

    @property
    def innerHTML(self) -> str:
        return self._to_html()

    @property
    def id(self) -> str:
        return self.attrs.get("id", "")

    @property
    def className(self) -> str:
        return self.attrs.get("class", "")

    @property
    def tagName(self) -> str:
        return self.tag.upper()

    @property
    def nodeType(self) -> int:
        return 1 if self.typ == NODE_BLOCK else 3

    @property
    def style(self) -> "_StyleProxy":
        return _StyleProxy(self)

    def _to_html(self) -> str:
        if self.typ == NODE_TEXT:
            return self.text
        attrs = " ".join(f'{k}="{v}"' for k, v in self.attrs.items())
        inner = "".join(c._to_html() for c in self.children)
        return f"<{self.tag} {attrs}>{inner}</{self.tag}>"

    # ── DOM mutations ─────────────────────────────────────────────────────────

    def appendChild(self, child: "LiveNode") -> "LiveNode":
        self._touch()
        child.parent = self
        self.children.append(child)
        self._mark_dirty()
        self.document._queue_bubble(self)
        self.document._emit_mutation()
        return child

    def removeChild(self, child: "LiveNode") -> "LiveNode":
        self._touch()
        if child in self.children:
            self.children.remove(child)
            child.parent = None
            child._kill_atom()
            self._mark_dirty()
            self.document._queue_bubble(self)
            self.document._emit_mutation()
        return child

    def insertBefore(self, new_child: "LiveNode",
                     ref: Optional["LiveNode"]) -> "LiveNode":
        self._touch()
        if ref is None:
            return self.appendChild(new_child)
        if ref not in self.children:
            raise ValueError("ref_child nie jest dzieckiem tego węzła")
        idx = self.children.index(ref)
        new_child.parent = self
        self.children.insert(idx, new_child)
        self._mark_dirty()
        self.document._queue_bubble(self)
        self.document._emit_mutation()
        return new_child

    def replaceChild(self, new_child: "LiveNode",
                     old_child: "LiveNode") -> "LiveNode":
        self._touch()
        if old_child not in self.children:
            raise ValueError("old_child nie jest dzieckiem tego węzła")
        idx = self.children.index(old_child)
        old_child.parent = None
        old_child._kill_atom()
        new_child.parent = self
        self.children[idx] = new_child
        self._mark_dirty()
        self.document._queue_bubble(self)
        self.document._emit_mutation()
        return old_child

    def remove(self) -> None:
        if self.parent:
            self.parent.removeChild(self)

    def cloneNode(self, deep: bool = False) -> "LiveNode":
        clone = LiveNode(self.document, self.typ, self.tag, self.text)
        clone.attrs = dict(self.attrs)
        if deep:
            for child in self.children:
                clone.appendChild(child.cloneNode(deep=True))
        return clone

    # ── Atrybuty ──────────────────────────────────────────────────────────────

    def setAttribute(self, name: str, value: str) -> None:
        self._touch()
        old = self.attrs.get(name)
        self.attrs[name] = str(value)
        if name == "id":
            if old:
                self.document._unregister_id(old)
            self.document._register_id(str(value), self)
        self._sync_atom()
        self.document._emit_mutation()

    def getAttribute(self, name: str) -> Optional[str]:
        self._touch()
        return self.attrs.get(name)

    def removeAttribute(self, name: str) -> None:
        self._touch()
        if name == "id" and name in self.attrs:
            self.document._unregister_id(self.attrs["id"])
        self.attrs.pop(name, None)
        self._sync_atom()
        self.document._emit_mutation()

    def hasAttribute(self, name: str) -> bool:
        return name in self.attrs

    def setTextContent(self, text: str) -> None:
        """Ustawia textContent — usuwa dzieci i wstawia węzeł tekstowy."""
        self._touch()
        for child in list(self.children):
            self.removeChild(child)
        if text:
            txt = self.document.createTextNode(str(text))
            self.appendChild(txt)
        self.document._emit_mutation()

    # ── Selektory ─────────────────────────────────────────────────────────────

    def querySelector(self, selector: str) -> Optional["LiveNode"]:
        self._touch()
        selector = selector.strip()
        if selector.startswith("#"):
            return self.document.getElementById(selector[1:])
        if selector.startswith("."):
            return self._find_by_class(selector[1:])
        return self._find_by_tag(selector)

    def querySelectorAll(self, selector: str) -> List["LiveNode"]:
        self._touch()
        selector = selector.strip()
        results: List[LiveNode] = []
        if selector.startswith("#"):
            node = self.document.getElementById(selector[1:])
            return [node] if node else []
        if selector.startswith("."):
            self._collect_by_class(selector[1:], results)
        else:
            self._collect_by_tag(selector, results)
        return results

    def getElementsByTagName(self, tag: str) -> List["LiveNode"]:
        self._touch()
        results: List[LiveNode] = []
        self._collect_by_tag(tag.lower(), results)
        return results

    def getElementsByClassName(self, cls: str) -> List["LiveNode"]:
        self._touch()
        results: List[LiveNode] = []
        self._collect_by_class(cls, results)
        return results

    def _find_by_tag(self, tag: str) -> Optional["LiveNode"]:
        if self.tag == tag.lower():
            return self
        for c in self.children:
            r = c._find_by_tag(tag)
            if r: return r
        return None

    def _find_by_class(self, cls: str) -> Optional["LiveNode"]:
        if cls in self.attrs.get("class", "").split():
            return self
        for c in self.children:
            r = c._find_by_class(cls)
            if r: return r
        return None

    def _collect_by_tag(self, tag: str, out: list) -> None:
        if self.tag == tag.lower():
            out.append(self)
        for c in self.children:
            c._collect_by_tag(tag, out)

    def _collect_by_class(self, cls: str, out: list) -> None:
        if cls in self.attrs.get("class", "").split():
            out.append(self)
        for c in self.children:
            c._collect_by_class(cls, out)

    # ── Zdarzenia ─────────────────────────────────────────────────────────────

    def addEventListener(self, event_type: str,
                         callback: Any,
                         options: Any = None) -> None:
        """
        Rejestruje listener. callback może być:
          - JS Function (z Core) — wywoływany przez vm._call()
          - Python callable — wywoływany bezpośrednio
        """
        if event_type not in SUPPORTED_EVENTS:
            return
        self._touch()
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        # Wrap JS Function → callable przez VM
        wrapped = self.document._wrap_callback(callback)
        self._listeners[event_type].append(wrapped)

    def removeEventListener(self, event_type: str,
                            callback: Any) -> None:
        if event_type in self._listeners:
            self._touch()
            # Usuń pierwszą pasującą — uproszczone (bez identyczności wrappera)
            if self._listeners[event_type]:
                self._listeners[event_type].pop(0)

    def dispatchEvent(self, event_type: str,
                      detail: Any = None) -> bool:
        """
        Wywołuje zdarzenie. Propagacja w górę (bubbling).
        Zwraca True jeśli listener istniał.
        """
        if event_type not in SUPPORTED_EVENTS:
            return False
        self._touch()
        fired = False
        for cb in list(self._listeners.get(event_type, [])):
            try:
                cb({"type": event_type, "target": self,
                    "detail": detail, "bubbles": True})
                fired = True
            except Exception:
                pass
        # Bubbling w górę
        if self.parent and event_type != "load":
            self.parent.dispatchEvent(event_type, detail)
        return fired

    # ── Phi-space ─────────────────────────────────────────────────────────────

    def _touch(self) -> None:
        """Ogrzewa atom przy interakcji JS."""
        try:
            atom = self.document.runtime.get_atom(self.phi_id)
            if atom:
                atom.T = min(100.0, atom.T + 10.0)
                atom.state = ("HOT"  if atom.T >= 70 else
                              "WARM" if atom.T >= 30 else "COLD")
        except Exception:
            pass

    def _sync_atom(self) -> None:
        """Tworzy lub aktualizuje atom w phi-space."""
        try:
            rt      = self.document.runtime
            role    = (f"live:{self.tag}" if self.typ == NODE_BLOCK
                       else "live:text")
            content = (self.text if self.typ == NODE_TEXT
                       else str(self.attrs))[:256]
            if rt.matrix.has_atom(self.phi_id):
                atom = rt.get_atom(self.phi_id)
                if atom:
                    atom.E = content
                    self._touch()
            else:
                rt.create_atom(self.phi_id, role, content, 50.0)
        except Exception:
            pass

    def _kill_atom(self) -> None:
        """Ochładza atom po usunięciu węzła (TOMB → GC)."""
        try:
            atom = self.document.runtime.get_atom(self.phi_id)
            if atom:
                atom.T     = 1.0
                atom.state = "TOMB"
        except Exception:
            pass

    def _mark_dirty(self) -> None:
        self._dirty = True
        if self.parent:
            self.parent._mark_dirty()

    def __repr__(self) -> str:
        if self.typ == NODE_TEXT:
            return f'Text("{self.text[:20]}")'
        attrs = " ".join(f'{k}="{v}"' for k, v in self.attrs.items())
        return f'<{self.tag} {attrs}>'


# ─── Style proxy ──────────────────────────────────────────────────────────────

class _StyleProxy:
    """Proxy dla node.style.property = value."""
    def __init__(self, node: LiveNode):
        object.__setattr__(self, "_node", node)
        object.__setattr__(self, "_styles", {})

    def __setattr__(self, name: str, value: str) -> None:
        styles = object.__getattribute__(self, "_styles")
        styles[name] = value
        node = object.__getattribute__(self, "_node")
        # Aktualizuj atrybut style w węźle
        css = "; ".join(f"{k}: {v}" for k, v in styles.items())
        node.attrs["style"] = css

    def __getattr__(self, name: str) -> str:
        styles = object.__getattribute__(self, "_styles")
        return styles.get(name, "")


# ─── LiveDOM ──────────────────────────────────────────────────────────────────

class LiveDOM:
    """
    Odpowiednik `document` w JS.
    Wstrzykiwany do globalnego scope VM przez bind_live_dom().

    Zarządza:
      - tworzeniem węzłów (createElement, createTextNode)
      - rejestrem id (getElementById)
      - synchronizacją bąbli phi-space
      - callbackami mutacji (dla renderera Lunety)
    """

    def __init__(self, runtime, prefix: str = "doc",
                 vm=None):
        self.runtime  = runtime
        self.prefix   = prefix
        self._vm      = vm          # referencja do JS VM (dla callbacks)
        self._by_id:  Dict[str, LiveNode]  = {}
        self._bubble_queue: Set[LiveNode]  = set()
        self._mutation_cbs: List[Callable] = []

        # Korzeń dokumentu
        self.body  = self.createElement("body")
        self.head  = self.createElement("head")
        self.title_node: Optional[LiveNode] = None

    # ── document.* API ────────────────────────────────────────────────────────

    def createElement(self, tag: str) -> LiveNode:
        return LiveNode(document=self, typ=NODE_BLOCK, tag=tag)

    def createTextNode(self, text: str) -> LiveNode:
        return LiveNode(document=self, typ=NODE_TEXT, text=text)

    def createDocumentFragment(self) -> LiveNode:
        return LiveNode(document=self, typ=NODE_BLOCK, tag="#fragment")

    def getElementById(self, id_val: str) -> Optional[LiveNode]:
        node = self._by_id.get(str(id_val))
        if node:
            node._touch()
        return node

    def getElementsByTagName(self, tag: str) -> List[LiveNode]:
        return self.body.getElementsByTagName(tag)

    def getElementsByClassName(self, cls: str) -> List[LiveNode]:
        return self.body.getElementsByClassName(cls)

    def querySelector(self, selector: str) -> Optional[LiveNode]:
        return self.body.querySelector(selector)

    def querySelectorAll(self, selector: str) -> List[LiveNode]:
        return self.body.querySelectorAll(selector)

    @property
    def title(self) -> str:
        if self.title_node:
            return self.title_node.textContent
        return ""

    @title.setter
    def title(self, value: str) -> None:
        if self.title_node is None:
            self.title_node = self.createTextNode(value)
        else:
            self.title_node.text = value

    # ── Rejestr ID ────────────────────────────────────────────────────────────

    def _register_id(self, id_val: str, node: LiveNode) -> None:
        self._by_id[id_val] = node

    def _unregister_id(self, id_val: str) -> None:
        self._by_id.pop(id_val, None)

    # ── Synchronizacja bąbli ──────────────────────────────────────────────────

    def _queue_bubble(self, node: LiveNode) -> None:
        """Odroczona aktualizacja bąbla — flush po skrypcie."""
        self._bubble_queue.add(node)

    def flush(self) -> None:
        """
        Flush odroczone aktualizacje bąbli.
        Wywoływane po zakończeniu skryptu JS lub przez scheduler.
        """
        for node in set(self._bubble_queue):
            if node._dirty:
                self._sync_bubble(node)
                node._dirty = False
        self._bubble_queue.clear()

    def _sync_bubble(self, parent: LiveNode) -> None:
        """Synchronizuje dzieci węzła jako bąbel w phi-space."""
        if not parent.children:
            return
        label = f"bbl_{parent.phi_id}"
        try:
            rt = self.runtime
            first_id = parent.children[0].phi_id
            if rt.get_bubble(label) is None:
                if rt.matrix.has_atom(first_id):
                    rt.consolidate(first_id)
            for child in parent.children[1:]:
                if rt.matrix.has_atom(child.phi_id):
                    try:
                        rt.import_to_bubble(label, child.phi_id)
                    except Exception:
                        pass
        except Exception:
            pass

    # ── Callbacki dla renderera ────────────────────────────────────────────────

    def on_mutate(self, callback: Callable) -> None:
        """Rejestruje callback wywoływany przy każdej mutacji DOM."""
        self._mutation_cbs.append(callback)

    def _emit_mutation(self) -> None:
        for cb in self._mutation_cbs:
            try:
                cb()
            except Exception:
                pass

    # ── Wrapping JS Function callbacks ───────────────────────────────────────

    def _wrap_callback(self, callback: Any) -> Callable:
        """
        Zamienia JS Function na Python callable przez vm._call().
        Kluczowe: Core nie wie jak wywołać Function z zewnątrz —
        LiveDOM ma referencję do VM i opakowuje to transparentnie.
        """
        if self._vm is None or not HAS_CORE:
            return callback if callable(callback) else lambda *a: None
        if isinstance(callback, JSFunction):
            vm = self._vm
            fn = callback
            return lambda *args: vm._call(fn, list(args))
        if callable(callback):
            return callback
        return lambda *a: None

    # ── Serializer dla debugowania ────────────────────────────────────────────

    def render_text(self, node: Optional[LiveNode] = None,
                    indent: int = 0) -> str:
        """Renderuje drzewo DOM jako tekst (debug/terminal)."""
        node = node or self.body
        pad  = "  " * indent
        if node.typ == NODE_TEXT:
            return f'{pad}"{node.text}"' if node.text.strip() else ""
        attrs = " ".join(f'{k}="{v}"' for k, v in node.attrs.items())
        header = f"{pad}<{node.tag}{' ' + attrs if attrs else ''}>"
        lines  = [header]
        for child in node.children:
            r = self.render_text(child, indent + 1)
            if r:
                lines.append(r)
        lines.append(f"{pad}</{node.tag}>")
        return "\n".join(lines)


# ─── Integracja ───────────────────────────────────────────────────────────────

def bind_live_dom(vm,
                  runtime,
                  prefix: str = "doc",
                  mutation_callback: Optional[Callable] = None) -> LiveDOM:
    """
    Tworzy LiveDOM i wstrzykuje go jako `document` do VM.

    Używa vm.set_global() (API karmazyn_js_core v1.0).
    VM dostaje referencję przez LiveDOM._vm — potrzebne do
    wywołania JS Function callbacks w addEventListener.
    """
    live_dom = LiveDOM(runtime, prefix=prefix, vm=vm)

    if mutation_callback:
        live_dom.on_mutate(mutation_callback)

    # Wstrzyknij `document` do globalnego scope JS
    vm.set_global("document", live_dom)

    # Wstrzyknij `window` jako alias global scope
    vm.set_global("window", {
        "document": live_dom,
        "location": {"href": "", "pathname": "", "search": ""},
        "history":  {"pushState": lambda *a: None,
                     "replaceState": lambda *a: None},
        "setTimeout":  lambda fn, ms=0: None,  # uproszczone
        "clearTimeout": lambda id=None: None,
    })

    return live_dom