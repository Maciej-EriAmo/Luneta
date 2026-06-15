"""
karmazyn_js_web.py — JS Web Bridge dla Lunety v1.3 (Diagnostyka)
================================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Most między silnikiem JS a przeglądarką Luneta.

Zmiany v1.3:
  - DIAGNOSTYKA: Wstrzyknięcie globalnego API `console` (log, warn, error).
  - DIAGNOSTYKA: Agregacja błędów parsowania i wykonania w `_js_errors`.
  - CLI: Dodano komendy `JS LOGS` i `JS ERRORS` do wglądu w zrzuty i awarie.

Zmiany v1.2:
  - USUNIĘTO destruktywny cache plików JS w atomach.
  - Skrypty zewnętrzne używają `http_get`.
"""

import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

from karmazyn_js_phi import KarmazynJSPhi, cmd_js
from karmazyn_live_dom import LiveDOM, LiveNode, bind_live_dom
from karmazyn_live_dom import NODE_TEXT
from karmazyn_browser import http_get

try:
    from karmazyn_browser import NodeType, SemanticNode, TextChunk
    HAS_BROWSER = True
except ImportError:
    HAS_BROWSER = False

# ─── Konwersja SemanticNode → LiveDOM ────────────────────────────────────────

def semantic_to_live(node: Any, doc: LiveDOM, depth: int = 0, max_depth: int = 50) -> Optional[LiveNode]:
    if node is None or depth > max_depth:
        return None

    typ = getattr(node, "typ", -1)
    tag = getattr(node, "tag", "")

    if typ == NODE_TEXT or typ == 8:
        text = getattr(node, "text", "")
        if not text or not text.strip():
            return None
        return doc.createTextNode(text)

    if typ == 0 or tag == "document":
        for child in getattr(node, "children", []):
            _populate_body(child, doc, depth + 1, max_depth)
        return doc.body

    live = doc.createElement(tag or "div")

    for k, v in getattr(node, "attrs", {}).items():
        live.setAttribute(k, str(v))

    for child in getattr(node, "children", []):
        live_child = semantic_to_live(child, doc, depth + 1, max_depth)
        if live_child is not None:
            live.appendChild(live_child)

    return live


def _populate_body(node: Any, doc: LiveDOM, depth: int, max_depth: int) -> None:
    tag = getattr(node, "tag", "")
    if tag in ("html", "body", "document"):
        for child in getattr(node, "children", []):
            _populate_body(child, doc, depth + 1, max_depth)
        return
    live = semantic_to_live(node, doc, depth, max_depth)
    if live is not None and live is not doc.body:
        doc.body.appendChild(live)


def build_live_dom(page: Any, runtime: Any, vm: KarmazynJSPhi) -> LiveDOM:
    doc = bind_live_dom(vm, runtime, prefix=_url_prefix(page.url))
    doc.title = page.title

    if getattr(page, "semantic_tree", None) is not None:
        _populate_body(page.semantic_tree, doc, 0, 50)

    doc.flush()
    return doc


# ─── Ekstrakcja skryptów ──────────────────────────────────────────────────────

def extract_scripts(html: str) -> List[Tuple[str, str]]:
    scripts = []
    pattern = re.compile(
        r'<script([^>]*)>(.*?)</script>',
        re.DOTALL | re.IGNORECASE,
    )
    src_pattern = re.compile(r'src=["\']([^"\']+)["\']', re.IGNORECASE)
    type_pattern = re.compile(r'type=["\']([^"\']+)["\']', re.IGNORECASE)

    for m in pattern.finditer(html):
        attrs_raw = m.group(1)
        code      = m.group(2).strip()

        type_m = type_pattern.search(attrs_raw)
        if type_m:
            t = type_m.group(1).lower()
            if t not in ("text/javascript", "application/javascript", "module", "application/ecmascript", ""):
                continue

        src_m = src_pattern.search(attrs_raw)
        if src_m:
            scripts.append(("external", src_m.group(1)))
        elif code:
            scripts.append(("inline", code))

    return scripts


# ─── JSBridge — główna klasa ──────────────────────────────────────────────────

class JSBridge:
    def __init__(self, runtime):
        self.runtime = runtime
        self._vm:   Optional[KarmazynJSPhi] = None
        self._doc:  Optional[LiveDOM]       = None
        self._page_url: str = ""
        self._scripts_run   = 0
        self._mutations     = 0
        self._active        = False
        
        # Narzędzia diagnostyczne
        self._console_logs: List[str] = []
        self._js_errors:    List[str] = []

    def attach(self, page: Any) -> None:
        self._console_logs.clear()
        self._js_errors.clear()
        
        self._vm = KarmazynJSPhi(
            runtime=self.runtime,
            name=_url_prefix(getattr(page, "url", "page")),
        )
        self._inject_browser_api(page)
        self._doc = build_live_dom(page, self.runtime, self._vm)
        self._doc.on_mutate(lambda: self._on_mutation())
        self._page_url    = getattr(page, "url", "")
        self._scripts_run = 0
        self._mutations   = 0
        self._active      = True

    def detach(self) -> None:
        self._vm     = None
        self._doc    = None
        self._active = False

    def run_scripts(self, page: Any) -> Dict[str, Any]:
        if not self._active or self._vm is None:
            return {"ok": False, "reason": "bridge not attached"}

        scripts = extract_scripts(getattr(page, "raw_html", ""))
        results = {"ok": True, "inline": 0, "external": 0,
                   "errors": [], "skipped": 0}

        for typ, content in scripts:
            if typ == "inline":
                ok, err = self._run_inline(content)
                if ok:
                    results["inline"] += 1
                    self._scripts_run += 1
                else:
                    results["skipped"] += 1
                    results["errors"].append(err)
            else:
                ok, err = self._run_external(content)
                if ok:
                    results["external"] += 1
                    self._scripts_run += 1
                else:
                    results["skipped"] += 1
                    results["errors"].append(f"external:{content}: {err}")

        if self._doc:
            self._doc.flush()

        return results

    def _run_external(self, url: str) -> Tuple[bool, str]:
        full_url = urllib.parse.urljoin(self._page_url, url)

        try:
            resp = http_get(full_url)
            if not resp or not resp.ok():
                err_msg = f"fetch failed: status {resp.status if resp else 0} dla {full_url}"
                self._js_errors.append(err_msg)
                return False, err_msg
            code = resp.text
        except Exception as e:
            err_msg = f"fetch error ({full_url}): {e}"
            self._js_errors.append(err_msg)
            return False, err_msg

        if not code or not code.strip():
            return False, "empty response"

        return self._run_inline(code, source_name=full_url)

    def _run_inline(self, source: str, source_name: str = "inline_script") -> Tuple[bool, str]:
        try:
            from karmazyn_js_parser import parse_js
            prog = parse_js(source)
            if not prog:
                return True, "empty AST"
            self._vm.run(prog)
            if self._doc:
                self._doc.flush()
            return True, ""
        except Exception as e:
            err_msg = f"[{source_name}] Error: {str(e)[:300]}"
            self._js_errors.append(err_msg)
            return False, err_msg

    def run_ast(self, program: list) -> Tuple[bool, Any]:
        if not self._active or self._vm is None:
            return False, "bridge not attached"
        try:
            result = self._vm.run(program)
            if self._doc:
                self._doc.flush()
            return True, result
        except Exception as e:
            err_msg = f"[AST Run] Error: {str(e)}"
            self._js_errors.append(err_msg)
            return False, str(e)

    def live_chunks(self) -> List[Any]:
        if not self._active or self._doc is None:
            return []
        return _live_to_chunks(self._doc.body)

    def has_mutations(self) -> bool:
        return self._mutations > 0

    def reset_mutations(self) -> None:
        self._mutations = 0

    def tick(self) -> Dict[str, Any]:
        if self._vm is None:
            return {}
        return self._vm.tick()

    def status(self) -> Dict[str, Any]:
        if not self._active or self._vm is None:
            return {"active": False}
        s = self._vm.phi_stats()
        s["active"]       = True
        s["mutations"]    = self._mutations
        s["scripts_run"]  = self._scripts_run
        s["dom_nodes"]    = self._count_nodes(self._doc.body) if self._doc else 0
        s["url"]          = self._page_url
        s["log_count"]    = len(self._console_logs)
        s["err_count"]    = len(self._js_errors)
        return s

    def _inject_browser_api(self, page: Any) -> None:
        url  = getattr(page, "url", "")
        vm   = self._vm

        def make_logger(level: str):
            def logger(*args):
                msg = " ".join(str(a) for a in args)
                # Ograniczamy rozrost logów na ekstremalnie głośnych stronach
                if len(self._console_logs) < 1000:
                    self._console_logs.append(f"[{level}] {msg}")
            return logger

        vm.set_global("console", {
            "log":   make_logger("LOG"),
            "error": make_logger("ERR"),
            "warn":  make_logger("WARN"),
            "info":  make_logger("INFO"),
            "debug": make_logger("DEBUG"),
        })

        vm.set_global("navigator", {
            "userAgent":  "KarmazynBrowser/4.4 KarmazynOS",
            "language":   "pl-PL",
            "onLine":     True,
            "platform":   "KarmazynOS",
        })
        vm.set_global("location", {
            "href":     url,
            "pathname": _url_pathname(url),
            "search":   _url_search(url),
            "hash":     "",
            "hostname": _url_hostname(url),
            "reload":   lambda: None,
            "assign":   lambda u: None,
        })
        vm.set_global("history", {
            "pushState":    lambda *a: None,
            "replaceState": lambda *a: None,
            "back":         lambda: None,
            "forward":      lambda: None,
            "length":       1,
        })
        vm.set_global("screen",  {"width": 80, "height": 24})
        vm.set_global("JSON", {
            "parse":     __import__("json").loads,
            "stringify": __import__("json").dumps,
        })
        vm.set_global("encodeURIComponent",
                      __import__("urllib.parse", fromlist=["quote"]).quote)
        vm.set_global("decodeURIComponent",
                      __import__("urllib.parse", fromlist=["unquote"]).unquote)

    def _on_mutation(self) -> None:
        self._mutations += 1

    def _count_nodes(self, node: LiveNode) -> int:
        if node is None:
            return 0
        return 1 + sum(self._count_nodes(c) for c in node.children)


def _live_to_chunks(node: LiveNode) -> List[Any]:
    chunks = []
    _node_to_chunks(node, chunks)
    return chunks

def _node_to_chunks(node: LiveNode, out: list, depth: int = 0) -> None:
    if node is None:
        return

    try:
        from karmazyn_browser import TextChunk
    except ImportError:
        class TextChunk:
            def __init__(self, text, preformatted=False):
                self.text = text
                self.preformatted = preformatted

    tag = node.tag

    if node.typ == NODE_TEXT:
        if node.text and node.text.strip():
            out.append(TextChunk(text=node.text))
        return

    if tag == "hr":
        out.append(TextChunk(text="\n" + "─" * 78 + "\n"))
        return

    block_tags = {"p","div","article","section","main","header",
                  "footer","nav","aside","li","tr","td","th"}
    is_block = tag in block_tags

    if is_block:
        out.append(TextChunk(text="\n"))

    if tag in ("h1","h2","h3","h4","h5","h6"):
        level  = int(tag[1])
        text   = "".join(c.text for c in node.children
                         if c.typ == NODE_TEXT and c.text)
        if text:
            prefix = "#" * level + " "
            line   = prefix + text
            sep    = ("═" if level == 1 else "─") * min(len(line), 78)
            out.append(TextChunk(text=f"\n{line}\n{sep}\n"))
        return

    if tag in ("pre", "code"):
        text = "".join(c.text for c in node.children if c.typ == NODE_TEXT)
        if text:
            out.append(TextChunk(text=text, preformatted=True))
        return

    for child in node.children:
        _node_to_chunks(child, out, depth + 1)

    if is_block:
        out.append(TextChunk(text="\n"))


def _url_prefix(url: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]", "_",
                  url.lower().replace("https://","").replace("http://",""))[:16]

def _url_pathname(url: str) -> str:
    try:
        import urllib.parse
        return urllib.parse.urlparse(url).path or "/"
    except Exception:
        return "/"

def _url_search(url: str) -> str:
    try:
        import urllib.parse
        q = urllib.parse.urlparse(url).query
        return f"?{q}" if q else ""
    except Exception:
        return ""

def _url_hostname(url: str) -> str:
    try:
        import urllib.parse
        return urllib.parse.urlparse(url).hostname or ""
    except Exception:
        return ""


def cmd_js_bridge(args: list, bridge: JSBridge) -> str:
    if not bridge._active:
        return "JS Bridge nieaktywny. Najpierw otwórz stronę: LUNETA <url>"

    if not args or args[0].upper() == "STATUS":
        s = bridge.status()
        lines = [
            f"Aktywny:     {s['active']}",
            f"URL:         {s.get('url','?')[:50]}",
            f"Węzły DOM:   {s.get('dom_nodes',0)}",
            f"Mutacje:     {s.get('mutations',0)}",
            f"Skrypty:     {s.get('scripts_run',0)}",
            f"Logi/Błędy:  {s.get('log_count',0)} logów / {s.get('err_count',0)} awarii",
            f"Atomy JS:    {s.get('atoms',0)}  HOT:{s.get('hot',0)}",
            f"Operacje:    {s.get('op_count',0)}/{s.get('max_ops',0)}",
        ]
        return "\n".join(lines)

    sub = args[0].upper()

    if sub == "LOGS":
        if not bridge._console_logs:
            return "Pusty log konsoli JS."
        # Wypisujemy ostatnie 40 wpisów, by nie zalać terminala
        return "Logi konsoli JS (ostatnie 40):\n" + "\n".join(f"  {l}" for l in bridge._console_logs[-40:])

    if sub == "ERRORS":
        if not bridge._js_errors:
            return "Brak awarii JS w bieżącej sesji."
        return "Błędy parsowania/wykonania JS (ostatnie 40):\n" + "\n".join(f"  - {e}" for e in bridge._js_errors[-40:])

    if sub == "THERMAL" and bridge._vm:
        return cmd_js(["THERMAL"], bridge._vm)

    if sub == "TICK" and bridge._vm:
        r = bridge.tick()
        return f"Tick #{r.get('tick','?')}: GC={r.get('collected',0)} anomalia={r.get('anomaly',0):.2f}"

    if sub == "DOM" and bridge._doc:
        return bridge._doc.render_text()

    if sub == "RUN" and len(args) > 1:
        expr_str = " ".join(args[1:])
        try:
            from karmazyn_js_parser import parse_js
            ast_prog = parse_js(expr_str)
            ok, result = bridge.run_ast(ast_prog)
            return f"{'OK' if ok else 'ERR'}: {result}"
        except Exception as e:
            return f"Błąd parsowania wyrażenia: {e}"

    return cmd_js_bridge(["STATUS"], bridge)