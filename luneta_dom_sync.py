"""
luneta_dom_sync.py — Szew A: write-back LiveDOM → semantic_tree
================================================================
KarmazynOS / Luneta — Maciej Mazur

JS wykonuje się na LiveDOM (kopii drzewa), ale renderer Lunety czyta wyłącznie
page.semantic_tree — więc mutacje JS są niewidoczne na ekranie. Ten moduł
konwertuje aktualny stan LiveDOM z powrotem na drzewo SemanticNode, żeby JS
faktycznie wpływał na to, co Luneta rysuje.

Mirror LiveDOM jest PEŁNY: limit w build_live_dom to głębokość 50, nie liczba
węzłów (żadna realna strona nie ma 50 poziomów zagnieżdżenia). Dlatego
przebudowa jest wierna i nie gubi treści.

LiveNode kolapsuje wszystko do BLOCK/TEXT, więc właściwy NodeType odtwarzamy
z TAGU — replika klasyfikacji parsera (karmazyn_browser), żeby nagłówki, linki,
listy i pre renderowały się tak samo jak przy zwykłym parsowaniu.
"""

from karmazyn_browser import SemanticNode, NodeType

NODE_TEXT = 8   # zgodne z karmazyn_live_dom.NODE_TEXT

_BLOCK = {"p", "div", "article", "section", "main", "header", "footer", "nav",
          "aside", "br", "li", "tr", "td", "th", "tbody", "thead", "noscript"}
_HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}
_LIST = {"ul", "ol"}
_PRE = {"pre", "code"}
_TRANSPARENT = {"#fragment", "body", "html", "document", ""}


def _tag_to_nodetype(tag: str) -> int:
    t = (tag or "").lower()
    if t in _HEADING:
        return NodeType.HEADING
    if t == "a":
        return NodeType.LINK
    if t in _LIST:
        return NodeType.LIST
    if t == "table":
        return NodeType.TABLE
    if t in _PRE:
        return NodeType.PRE
    if t == "hr":
        return NodeType.HR
    if t in _BLOCK:
        return NodeType.BLOCK
    return NodeType.INLINE


def _convert(live, out_children):
    """Konwertuje pojedynczy LiveNode i dokłada wynik do out_children.

    Węzły przezroczyste (#fragment/body) nie tworzą własnego SemanticNode —
    ich dzieci wpływają w miejscu do rodzica.
    """
    typ = getattr(live, "typ", None)
    if typ == NODE_TEXT:
        txt = getattr(live, "text", "") or ""
        if txt:
            out_children.append(SemanticNode(NodeType.TEXT, text=txt))
        return

    tag = (getattr(live, "tag", "") or "").lower()
    kids = getattr(live, "children", []) or []

    if tag in _TRANSPARENT:
        for c in kids:
            _convert(c, out_children)
        return

    sn = SemanticNode(_tag_to_nodetype(tag), tag,
                      dict(getattr(live, "attrs", {}) or {}))
    for c in kids:
        _convert(c, sn.children)
    out_children.append(sn)


def live_to_semantic(doc):
    """Buduje drzewo SemanticNode (DOCUMENT) z aktualnego LiveDOM.body."""
    root = SemanticNode(NodeType.DOCUMENT, "document")
    body = getattr(doc, "body", None)
    if body is None:
        return root
    for child in getattr(body, "children", []) or []:
        _convert(child, root.children)
    return root