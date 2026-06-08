"""
luneta_async_bridge.py — Integracja async engine z silnikiem Lunety v1.0
=========================================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Podpina ThermalLoop + AsyncAtom + fetch pod istniejący JSBridge.
Nieinwazyjne: monkey-patch instancji JSBridge (wzorzec attach_to_browser).

Co dodaje do każdej strony:
  - ThermalLoop per strona (tworzony przy attach, czyszczony przy detach)
  - setTimeout / setInterval / clearTimeout / queueMicrotask / Promise
  - fetch(url) → AsyncAtom napędzany http_get z karmazyn_browser
  - response.json() / response.text() → AsyncAtom (resolved)
  - napędzanie pętli: auto-settle po run_scripts + pump na każdym tick

Cykl życia:
  bridge.attach(page)      → nowy VM + nowy loop + async API + fetch
  bridge.run_scripts(page) → skrypty + auto-settle (drenaż micro/promise)
  bridge.tick()            → pump pętli 1 obrót + vm.tick() (phi GC)
  bridge.pump(n)           → ręczne n obrotów pętli (dla REPL)
  bridge.detach()          → loop usunięty

Użycie w luneta.py:
    from luneta_async_bridge import install_async_engine
    browser = LunetaBrowser(runtime)
    install_async_engine(browser.js_bridge)   # jedna linia

Lub przez browser (instaluje na js_bridge jeśli jest):
    install_async_engine_on_browser(browser)
"""

import json as _json
from typing import Any, Callable, Dict, List, Optional

from karmazyn_async import (
    ThermalLoop, AsyncAtom,
    install_async_runtime,
    TICK_MS,
)

# http_get z przeglądarki — realne I/O dla fetch
try:
    from karmazyn_browser import http_get, _resolve_url
    _HAS_HTTP = True
except ImportError:
    http_get = None
    _resolve_url = None
    _HAS_HTTP = False


# ─── FetchResponse — odpowiednik Response z fetch API ─────────────────────────

class FetchResponse:
    """
    Odpowiednik obiektu Response z fetch().

    Atrybuty (JS property):
      ok       — bool, 200..299
      status   — int kod HTTP
      url      — finalny URL
    Metody (zwracają AsyncAtom, jak w przeglądarce):
      text()   — treść jako string
      json()   — sparsowany JSON (reject przy błędzie parsowania)
    """

    __slots__ = ("_loop", "status", "ok", "url", "_text", "_headers")

    def __init__(self, loop: ThermalLoop, status: int, body_text: str,
                 url: str = "", headers: Optional[dict] = None):
        self._loop    = loop
        self.status   = status
        self.ok       = 200 <= status < 300
        self.url      = url
        self._text    = body_text
        self._headers = headers or {}

    def text(self) -> AsyncAtom:
        aa = self._loop.create_async()
        aa.resolve(self._text)
        return aa

    def json(self) -> AsyncAtom:
        aa = self._loop.create_async()
        try:
            aa.resolve(_json.loads(self._text))
        except Exception as e:
            aa.reject(f"JSON parse error: {e}")
        return aa

    def __repr__(self) -> str:
        return f"FetchResponse({self.status}, ok={self.ok}, url={self.url!r})"


# ─── fetch ────────────────────────────────────────────────────────────────────

def make_fetch(loop: ThermalLoop, base_url: str = "",
               timeout: float = 15.0) -> Callable:
    """
    Buduje funkcję fetch(url, options) dla danej pętli.

    fetch zwraca AsyncAtom NATYCHMIAST (PENDING/COLD).
    Realne żądanie HTTP wykonuje się jako macrotask przy następnym pump —
    host robi I/O, potem ogrzewa atom (resolve) lub zabija (reject).

    To dokładnie model V8: fetch() zwraca Promise od ręki, a żądanie
    realizowane jest "pod spodem" przez hosta. Tu host = http_get.
    """
    def _fetch(url, options=None):
        aa = loop.create_async()

        if not _HAS_HTTP or http_get is None:
            aa.reject("fetch: http_get niedostępny (brak karmazyn_browser)")
            return aa

        full_url = url
        if _resolve_url is not None and base_url:
            resolved = _resolve_url(url, base_url)
            full_url = resolved or url
        if not str(full_url).startswith(("http://", "https://")):
            full_url = "https://" + str(full_url)

        # Realne żądanie odracza się do pętli — host robi I/O później
        def _do_request():
            try:
                resp = http_get(full_url, timeout=timeout)
                if resp is None:
                    aa.reject(f"fetch: brak odpowiedzi z {full_url}")
                    return
                if resp.status == 0:
                    aa.reject(f"fetch: błąd sieci ({resp.body.decode('utf-8', 'ignore')[:80]})")
                    return
                response = FetchResponse(
                    loop, resp.status, resp.text,
                    url=full_url, headers=resp.headers,
                )
                aa.resolve(response)
            except Exception as e:
                aa.reject(f"fetch: {e}")

        loop.macro(_do_request)
        return aa

    return _fetch


# ─── Integracja z JSBridge ────────────────────────────────────────────────────

def install_async_engine(bridge,
                         settle_ticks: int = 64,
                         ticks_per_pump: int = 1,
                         tick_ms: float = TICK_MS) -> None:
    """
    Podpina async engine pod instancję JSBridge (monkey-patch).

    settle_ticks    — ile obrotów pętli po run_scripts (drenaż micro/promise
                      + krótkie timery). Cap chroni przed setInterval w pętli.
    ticks_per_pump  — ile obrotów pętli na jeden bridge.tick().
    tick_ms         — ms na tick (mapowanie setTimeout(ms) → ticki).

    Idempotentne: ponowne wywołanie nie nakłada patcha drugi raz.
    """
    if getattr(bridge, "_async_installed", False):
        return
    bridge._async_installed = True
    bridge._async_loop: Optional[ThermalLoop] = None
    bridge._async_errors: List[str] = []

    _orig_attach      = bridge.attach
    _orig_detach      = bridge.detach
    _orig_run_scripts = bridge.run_scripts
    _orig_tick        = bridge.tick

    # ── attach: po stworzeniu VM podpinamy loop + async API + fetch ──────────
    def _attach(page):
        _orig_attach(page)
        vm = bridge._vm
        if vm is None:
            return
        loop = ThermalLoop()
        loop.set_vm(vm)
        loop.on_error(lambda e: bridge._async_errors.append(str(e)))
        # setTimeout / setInterval / clearTimeout / queueMicrotask / Promise
        install_async_runtime(vm, loop, tick_ms=tick_ms)
        # fetch związany z URL strony (dla względnych adresów)
        page_url = getattr(page, "url", "")
        vm.global_scope.vars["fetch"] = make_fetch(loop, base_url=page_url)
        bridge._async_loop = loop

    # ── detach: usuwamy loop ──────────────────────────────────────────────────
    def _detach():
        bridge._async_loop = None
        _orig_detach()

    # ── run_scripts: po skryptach auto-settle pętli ──────────────────────────
    def _run_scripts(page):
        result = _orig_run_scripts(page)
        loop = bridge._async_loop
        if loop is not None:
            # Drenaż TYLKO pracy jednorazowej (micro/promise/setTimeout).
            # NIE goni za setInterval — inaczej strona z zegarem/karuzelą
            # zawieszałaby ładowanie na settle_ticks obrotów.
            settled = loop.settle(max_ticks=settle_ticks)
            if isinstance(result, dict):
                result["async_settled"] = settled
        return result

    # ── tick: pump pętli + oryginalny vm.tick() ──────────────────────────────
    def _tick():
        loop = bridge._async_loop
        pumped = {}
        if loop is not None:
            for _ in range(ticks_per_pump):
                if not loop._has_work():
                    break
                pumped = loop.tick()
        vm_stats = _orig_tick()
        if isinstance(vm_stats, dict) and pumped:
            vm_stats["async"] = pumped
        return vm_stats

    bridge.attach      = _attach
    bridge.detach      = _detach
    bridge.run_scripts = _run_scripts
    bridge.tick        = _tick

    # ── Nowe metody na instancji ──────────────────────────────────────────────
    def _pump(n: int = 1) -> Dict[str, Any]:
        """Ręcznie przesuwa pętlę o n obrotów (dla REPL: 'js pump 10')."""
        loop = bridge._async_loop
        if loop is None:
            return {"ok": False, "reason": "brak aktywnej pętli"}
        ran = fired = 0
        for _ in range(max(1, n)):
            if not loop._has_work():
                break
            st = loop.tick()
            ran   += st["ran"]
            fired += st["fired"]
        return {"ok": True, "ran": ran, "fired": fired, "stats": loop.stats()}

    def _run_loop(max_ticks: int = 1000) -> Dict[str, Any]:
        """Napędza pętlę aż do bezczynności lub max_ticks (dla REPL: 'js run')."""
        loop = bridge._async_loop
        if loop is None:
            return {"ok": False, "reason": "brak aktywnej pętli"}
        summary = loop.run(max_ticks=max_ticks)
        summary["ok"] = True
        return summary

    def _loop_stats() -> Dict[str, Any]:
        loop = bridge._async_loop
        if loop is None:
            return {"active": False}
        s = loop.stats()
        s["active"] = True
        s["errors"] = list(bridge._async_errors[-5:])
        return s

    bridge.pump       = _pump
    bridge.run_loop   = _run_loop
    bridge.loop_stats = _loop_stats


def install_async_engine_on_browser(browser, **kwargs) -> bool:
    """
    Wygodny wrapper: instaluje async engine na browser.js_bridge jeśli istnieje.
    Zwraca True jeśli się udało.
    """
    bridge = getattr(browser, "js_bridge", None)
    has_js = getattr(browser, "_has_js", False)
    if not has_js or bridge is None:
        return False
    install_async_engine(bridge, **kwargs)
    return True


# ─── Rozszerzenie komendy JS dla REPL ─────────────────────────────────────────

def cmd_async(args: list, bridge) -> str:
    """
    Komendy async dla REPL Lunety (rozszerzenie 'js'):
      js pump [n]   — przesuń pętlę o n obrotów (domyślnie 1)
      js run [max]  — napędź pętlę aż do bezczynności (cap max)
      js loop       — statystyki pętli termodynamicznej
      js errors     — ostatnie błędy async

    Zwraca None jeśli komenda nie jest async (caller obsłuży standardowo).
    """
    if not args:
        return None
    sub = args[0].upper()

    if not getattr(bridge, "_async_installed", False):
        if sub in ("PUMP", "RUN", "LOOP", "ERRORS"):
            return "Async engine niezainstalowany. Wywołaj install_async_engine(bridge)."
        return None

    if sub == "PUMP":
        n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
        r = bridge.pump(n)
        if not r.get("ok"):
            return r.get("reason", "błąd")
        s = r["stats"]
        return (f"Pump x{n}: wykonano {r['ran']} tasków, "
                f"dopalono {r['fired']} timerów\n"
                f"Pozostało: ready={s['ready']} timers={s['timers']} "
                f"async={s['async_pending']}p")

    if sub == "RUN":
        max_t = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1000
        r = bridge.run_loop(max_t)
        if not r.get("ok"):
            return r.get("reason", "błąd")
        return (f"Loop run: {r['ticks']} ticków, "
                f"{r['ran_total']} tasków, {r['fired_total']} timerów dopalonych")

    if sub == "LOOP":
        s = bridge.loop_stats()
        if not s.get("active"):
            return "Pętla nieaktywna (brak załadowanej strony)."
        lines = [
            f"Tick:          {s['tick']}",
            f"Ready:         {s['ready']} tasków",
            f"Timery:        {s['timers']} lontów",
            f"Async:         {s['async_total']} ({s['async_pending']} pending)",
            f"Wykonano:      {s['ran_total']} tasków",
            f"Dopalono:      {s['fired_total']} timerów",
        ]
        if s.get("errors"):
            lines.append(f"Błędy:         {len(s['errors'])}")
        return "\n".join(lines)

    if sub == "ERRORS":
        errs = getattr(bridge, "_async_errors", [])
        if not errs:
            return "Brak błędów async."
        return "Ostatnie błędy async:\n" + "\n".join(f"  {e}" for e in errs[-10:])

    return None


# ─── Self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 64)
    print("luneta_async_bridge.py — testy integracji")
    print("=" * 64)

    passed = failed = 0
    def chk(name, ok, detail=""):
        global passed, failed
        print(f"  {'OK ' if ok else 'XX '} {name}")
        if detail and not ok: print(f"       {detail}")
        if ok: passed += 1
        else:  failed += 1

    from luneta_runtime import LunetaRuntime
    from karmazyn_browser import LunetaBrowser, parse_html

    # ── 1. Instalacja na browser ──────────────────────────────────────────────
    print("\n[1] Instalacja na browser")
    runtime = LunetaRuntime()
    browser = LunetaBrowser(runtime)
    ok = install_async_engine_on_browser(browser)
    chk("install_async_engine_on_browser", ok and browser._has_js)
    chk("bridge ma _async_installed",
        getattr(browser.js_bridge, "_async_installed", False))

    # ── 2. attach tworzy loop ──────────────────────────────────────────────────
    print("\n[2] attach tworzy pętlę")
    html = """<html><head><title>Async Test</title></head><body>
    <h1>Test</h1>
    <script>setTimeout(function(){ }, 50);</script>
    </body></html>"""
    page = parse_html(html, url="http://test.local", width=60)
    browser.js_bridge.attach(page)
    chk("loop utworzony po attach", browser.js_bridge._async_loop is not None)
    vm = browser.js_bridge._vm
    chk("setTimeout w scope", "setTimeout" in vm.global_scope.vars)
    chk("Promise w scope", "Promise" in vm.global_scope.vars)
    chk("fetch w scope", "fetch" in vm.global_scope.vars)

    # ── 3. setTimeout odpala przez bridge ──────────────────────────────────────
    print("\n[3] setTimeout przez bridge")
    from karmazyn_js_parser import parse_js
    fired = []
    vm.global_scope.vars["mark"] = lambda: fired.append(1)
    vm.run(parse_js("setTimeout(function(){ mark(); }, 50);"))
    browser.js_bridge.run_loop(100)
    chk("setTimeout odpalił przez bridge", fired == [1], f"fired={fired}")

    # ── 4. Promise przez bridge ─────────────────────────────────────────────────
    print("\n[4] Promise przez bridge")
    pvals = []
    vm.global_scope.vars["collect"] = lambda v: pvals.append(v)
    vm.run(parse_js("Promise(function(res){ res(123); }).then(function(v){ collect(v); });"))
    browser.js_bridge.run_loop(50)
    chk("Promise rozwiązany", pvals == [123], f"pvals={pvals}")

    # ── 5. pump krokowo ─────────────────────────────────────────────────────────
    print("\n[5] pump krokowo")
    steps = []
    vm.global_scope.vars["step"] = lambda: steps.append(browser.js_bridge._async_loop._tick_n)
    vm.run(parse_js("setTimeout(function(){ step(); }, 80);"))
    r = browser.js_bridge.pump(2)
    chk("pump 2 nie odpalił jeszcze (delay 80ms=5t)", len(steps) == 0, f"steps={steps}")
    browser.js_bridge.pump(10)
    chk("po pump 10 odpalił", len(steps) == 1, f"steps={steps}")

    # ── 6. cmd_async LOOP ───────────────────────────────────────────────────────
    print("\n[6] cmd_async LOOP")
    out = cmd_async(["LOOP"], browser.js_bridge)
    chk("LOOP zwraca statystyki", out and "Tick:" in out, out)

    # ── 7. cmd_async PUMP ───────────────────────────────────────────────────────
    print("\n[7] cmd_async PUMP")
    out = cmd_async(["PUMP", "3"], browser.js_bridge)
    chk("PUMP zwraca wynik", out and "Pump" in out, out)

    # ── 8. detach czyści loop ─────────────────────────────────────────────────
    print("\n[8] detach czyści pętlę")
    browser.js_bridge.detach()
    chk("loop usunięty po detach", browser.js_bridge._async_loop is None)

    # ── 9. Idempotencja ─────────────────────────────────────────────────────────
    print("\n[9] Idempotencja instalacji")
    rt2 = LunetaRuntime()
    br2 = LunetaBrowser(rt2)
    install_async_engine_on_browser(br2)
    attach_before = br2.js_bridge.attach
    install_async_engine_on_browser(br2)   # drugi raz — nie powinien re-patchować
    chk("drugi install nie re-patchuje", br2.js_bridge.attach is attach_before)

    print(f"\n{'=' * 64}")
    print(f"Wyniki: {passed}/{passed + failed}")
    print("PASS — integracja operacyjna" if failed == 0
          else f"FAIL — {failed} testów nie przeszło")
    print("=" * 64)