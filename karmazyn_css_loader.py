"""
karmazyn_css_loader.py — Asynchroniczne pobieranie zewnętrznych arkuszy CSS
============================================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Ten sam wzorzec co ImageLoader: kolejka + wątek roboczy + http_get. Pobiera
`<link rel="stylesheet">`, parsuje utwardzonym czytnikiem (parse_stylesheet) i
udostępnia zagregowane reguły. Wołane co klatkę przez poll(); gdy dojdą nowe
reguły, zwraca True, żeby viewer przeliczył layout (sprite'y się doresolwują).

Reguły z <style> strona ma od razu (parse_html); ten moduł dokłada zewnętrzne.
"""

import queue
import threading

try:
    from karmazyn_browser import http_get
    _HAS_HTTP = True
except Exception:
    http_get = None
    _HAS_HTTP = False

from karmazyn_css import parse_stylesheet


class CssLoader:
    def __init__(self, runtime=None):
        self._runtime = runtime
        self._in = queue.Queue()
        self._done = queue.Queue()
        self._status = {}          # url -> 'loading'|'ready'|'error'
        self._rules = {}           # url -> [(selektory, deklaracje), ...]
        self._started = False
        self._worker = None

    # ── Wątek roboczy ────────────────────────────────────────────────────────
    def _ensure_worker(self):
        if self._started:
            return
        self._started = True
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self):
        while True:
            url = self._in.get()
            if url is None:
                break
            try:
                if http_get is None:
                    raise RuntimeError("brak http_get")
                resp = http_get(url)
                body = getattr(resp, "body", b"") or b""
                status = getattr(resp, "status", 0)
                if status and status >= 400:
                    raise RuntimeError(f"HTTP {status}")
                text = body.decode("utf-8", "replace") if isinstance(body, (bytes, bytearray)) else str(body)
                self._done.put((url, "ok", text))
            except Exception as e:
                self._done.put((url, "err", str(e)))

    # ── API głównego wątku ───────────────────────────────────────────────────
    def request(self, url: str):
        """Zleć pobranie arkusza (idempotentne)."""
        if not url or url in self._status:
            return
        self._status[url] = "loading"
        self._ensure_worker()
        self._in.put(url)

    def request_many(self, urls):
        for u in urls or ():
            self.request(u)

    def poll(self) -> bool:
        """Odbierz gotowe arkusze, sparsuj. Zwraca True, jeśli doszły nowe reguły
        (viewer powinien wtedy przeliczyć layout)."""
        changed = False
        while True:
            try:
                url, kind, payload = self._done.get_nowait()
            except queue.Empty:
                break
            if kind == "ok":
                try:
                    self._rules[url] = parse_stylesheet(payload)
                    self._status[url] = "ready"
                except Exception:
                    self._rules[url] = []
                    self._status[url] = "error"
            else:
                self._rules[url] = []
                self._status[url] = "error"
            changed = True
        return changed

    def rules(self):
        """Zagregowane reguły ze wszystkich pobranych arkuszy (kolejność zgłoszeń)."""
        out = []
        for r in self._rules.values():
            out.extend(r)
        return out

    def status(self, url: str) -> str:
        return self._status.get(url, "empty")

    def pending(self) -> bool:
        return any(s == "loading" for s in self._status.values())

    def reset(self):
        """Nowa strona — czyścimy stan (stary wątek dokończy w tle nieszkodliwie)."""
        self._status.clear()
        self._rules.clear()
