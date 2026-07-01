"""
luneta_image_loader.py — Ładowarka obrazów dla Lunety (E1)
==========================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Rozdział obowiązków, świadomie omijający most async (ten jest jednowątkowy
i napędza VM JS — http_get wykonuje się w nim synchronicznie):

  [wątek roboczy, daemon]  : WYŁĄCZNIE sieć (http_get -> bajty)
  [wątek główny / render]  : poll() dekoduje bajty -> Surface,
                             get() skaluje na żądanie, cache po sha256.

Dekodowanie i skalowanie MUSI być na głównym wątku — convert_alpha() i
transform wymagają kontekstu wyświetlania SDL. Worker dotyka tylko kolejek
(thread-safe), więc reszta stanu jest jednowątkowa i nie wymaga zamków.

Cache E2: oryginały po sha256 z budżetem bajtów (twardy bezpiecznik), ale
GŁÓWNA eksmisja idzie przez substrat — każda tekstura to atom (widoczność =
touch przy rysowaniu, stygnięcie przez decay, reach-GC żnie zimne+nieosiągalne).
Bez wstrzykniętego runtime degraduje się łagodnie do czystego LRU bajtowego (E1).
"""

import io
import queue
import base64
import hashlib
import threading
import urllib.parse

try:
    from karmazyn_gif import gif_aid as _gif_aid
except Exception:
    def _gif_aid(url):
        return "gif:" + hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:16]

try:
    from karmazyn_svg import parse_svg as _parse_svg, svg_aid as _svg_aid
    _HAS_SVG = True
except Exception:
    _parse_svg = _svg_aid = None
    _HAS_SVG = False

# E2: startowa temperatura atomu-tekstury (WARM, spójna z substratem T_INIT).
_TEX_T_INIT = 50.0


def decode_data_uri(uri: str):
    """Zwraca bajty z data: URI albo None. Obsługuje base64 i percent-encoding.

    SVG (data:image/svg+xml) zwróci bajty, ale pygame.image ich nie zdekoduje —
    skończy się stanem 'error' i tekstem alt (świadoma, łagodna degradacja).
    """
    try:
        head, sep, payload = uri.partition(",")
        if not sep or not payload:
            return None
        if ";base64" in head.lower():
            return base64.b64decode(payload)
        return urllib.parse.unquote_to_bytes(payload)
    except Exception:
        return None


class ImageLoader:
    # Stany zwracane na zewnątrz: 'empty' | 'loading' | 'ready' | 'error'
    def __init__(self, fetch_fn=None,
                 max_bytes: int = 64 * 1024 * 1024,
                 max_pixels: int = 4096 * 4096,
                 runtime=None):
        self._fetch = fetch_fn                 # jeśli None -> lazy http_get z browsera
        self._runtime = runtime                # E2: substrat — tekstury jako atomy
        self._in = queue.Queue()               # url do pobrania (main -> worker)
        self._done = queue.Queue()             # (url, kind, payload) (worker -> main)

        # Stan WYŁĄCZNIE głównego wątku (worker go nie dotyka):
        self._status = {}                      # url -> stan
        self._media_kind = {}                  # url -> "gif"|"vector" (detekcja po treści)
        self._media_aid = {}                   # url -> atom_id medium
        self._url_sha = {}                     # url -> sha256(bajty)
        self._orig = {}                        # sha -> Surface (zdekodowana, nieskalowana)
        self._scaled = {}                      # (sha, w, h) -> Surface
        self._sha_bytes = {}                   # sha -> rozmiar bajtów
        self._sha_atom = {}                    # sha -> id atomu-tekstury (E2)
        self._lru = []                         # sha w kolejności użycia (front = najzimniejszy)
        self._bytes_total = 0

        self.max_bytes = max_bytes
        self.max_pixels = max_pixels

        self._stop = False
        self._worker = threading.Thread(target=self._run, name="luneta-img", daemon=True)
        self._worker.start()

    # ── WĄTEK ROBOCZY: tylko sieć ────────────────────────────────────────
    def _run(self):
        while True:
            url = self._in.get()
            if url is None:                    # sentinel = stop
                return
            try:
                fetch = self._fetch
                if fetch is None:
                    from karmazyn_browser import http_get as fetch
                resp = fetch(url)
                body = getattr(resp, "body", None)
                status = getattr(resp, "status", 0)
                if body and 200 <= status < 300:
                    self._done.put((url, "ok", bytes(body)))
                else:
                    self._done.put((url, "error", None))
            except Exception:
                self._done.put((url, "error", None))

    # ── API GŁÓWNEGO WĄTKU ───────────────────────────────────────────────
    def request(self, url: str):
        """Zleca pobranie (idempotentnie). data: kierujemy od razu do dekodu."""
        if not url:
            return
        if self._status.get(url) in ("loading", "ready", "error"):
            return
        if url.startswith("data:"):
            self._status[url] = "loading"
            self._done.put((url, "data", url))     # zdekodujemy w poll() (main)
            return
        if not url.startswith(("http://", "https://")):
            self._status[url] = "error"
            return
        self._status[url] = "loading"
        self._in.put(url)

    def poll(self):
        """Główny wątek, co klatkę: dekoduje świeżo dostarczone bajty do Surface.

        Zwraca zbiór url, których stan się zmienił (do oznaczenia layoutu jako dirty).
        """
        import pygame
        changed = set()
        while True:
            try:
                url, kind, payload = self._done.get_nowait()
            except queue.Empty:
                break
            if kind == "error":
                self._status[url] = "error"
                changed.add(url)
                continue
            try:
                data = decode_data_uri(payload) if kind == "data" else payload
                if not data:
                    raise ValueError("brak danych")
                sha = hashlib.sha256(data).hexdigest()
                # Detekcja medium PO TREŚCI (magia bajtów), niezależna od URL —
                # łapie SVG/GIF podane bez rozszerzenia, z query, data-uri czy .svgz.
                gp = getattr(self._runtime, "gif_pump", None)
                if data[:4] == b"GIF8" and gp is not None:
                    aid = _gif_aid(url)
                    if not gp.has(aid):
                        gp.load_gif(aid, data, label=url)
                    self._media_kind[url] = "gif"; self._media_aid[url] = aid
                    self._status[url] = "ready"
                    changed.add(url)
                    continue
                svg_bytes = data
                if data[:2] == b"\x1f\x8b":            # gzip -> .svgz
                    try:
                        import gzip
                        svg_bytes = gzip.decompress(data)
                    except Exception:
                        svg_bytes = data
                head = svg_bytes[:512].lstrip()
                looks_svg = (head[:5].lower() == b"<?xml" or head[:4].lower() == b"<svg"
                             or b"<svg" in head.lower())
                if (looks_svg and _HAS_SVG
                        and getattr(self._runtime, "media", None) is not None):
                    parsed = _parse_svg(svg_bytes)
                    if parsed["polylines"]:
                        aid = _svg_aid(url)
                        reg = self._runtime.matrix
                        if not reg.has(aid):
                            reg.create(aid, S="media:svg", E=url, T=70.0,
                                       metadata={"polylines": parsed["polylines"],
                                                 "viewbox": parsed["viewbox"]})
                        self._media_kind[url] = "vector"; self._media_aid[url] = aid
                        self._status[url] = "ready"
                    else:
                        self._status[url] = "error"
                    changed.add(url)
                    continue
                if sha not in self._orig:
                    surf = pygame.image.load(io.BytesIO(data))
                    if surf.get_width() * surf.get_height() > self.max_pixels:
                        raise ValueError("obraz przekracza limit pikseli")
                    try:
                        surf = surf.convert_alpha()
                    except Exception:
                        pass                       # brak kontekstu wyświetlania — zostaje raw
                    self._orig[sha] = surf
                    self._sha_bytes[sha] = len(data)
                    self._bytes_total += len(data)
                    self._touch_lru(sha)
                    self._make_atom(sha)           # E2: tekstura wchodzi do substratu
                    self._evict_if_needed()
                self._url_sha[url] = sha
                self._status[url] = "ready"
                changed.add(url)
            except Exception:
                self._status[url] = "error"
                changed.add(url)
        return changed

    def status(self, url: str) -> str:
        return self._status.get(url, "empty")

    def media_kind(self, url: str):
        """Rodzaj medium wykryty po treści ('gif'|'vector') albo None."""
        return self._media_kind.get(url)

    def media_aid(self, url: str):
        """Atom_id medium dla danego url (po detekcji) albo None."""
        return self._media_aid.get(url)

    def intrinsic_size(self, url: str):
        """Realne wymiary (w, h) zdekodowanego obrazu albo None."""
        sha = self._url_sha.get(url)
        orig = self._orig.get(sha) if sha else None
        return orig.get_size() if orig else None

    def get(self, url: str, w: int = 0, h: int = 0):
        """Zwraca (Surface|None, stan). Dla 'ready' skaluje do (w,h) i cache'uje."""
        st = self._status.get(url, "empty")
        if st != "ready":
            return None, st
        sha = self._url_sha.get(url)
        orig = self._orig.get(sha) if sha else None
        if orig is None:
            return None, "error"
        self._touch_lru(sha)
        self._heat_sha(sha)                    # E2: narysowanie = touch (widoczność grzeje)
        if w <= 0 or h <= 0:
            return orig, "ready"
        key = (sha, int(w), int(h))
        surf = self._scaled.get(key)
        if surf is None:
            import pygame
            try:
                surf = pygame.transform.smoothscale(orig, (int(w), int(h)))
            except Exception:
                surf = pygame.transform.scale(orig, (int(w), int(h)))
            self._scaled[key] = surf
        return surf, "ready"

    # ── Substrat E2: tekstury jako atomy ─────────────────────────────────
    @staticmethod
    def _atom_id(sha: str) -> str:
        return "tex:" + sha[:16]

    def _make_atom(self, sha: str) -> None:
        """Rejestruje teksturę jako atom w substracie (start: WARM)."""
        rt = self._runtime
        if rt is None:
            return
        aid = self._atom_id(sha)
        self._sha_atom[sha] = aid
        if not rt.has_atom(aid):
            try:
                rt.create_atom(aid, S="tex", E=sha[:12], T=_TEX_T_INIT)
            except Exception:
                pass

    def _heat_sha(self, sha: str) -> None:
        """Narysowanie tekstury = touch jej atomu (widoczność grzeje)."""
        rt = self._runtime
        if rt is None:
            return
        aid = self._sha_atom.get(sha)
        if not aid:
            return
        atom = rt.get_atom(aid)
        if atom is not None:
            atom.touch(1.0)

    def atom_ids_for_srcs(self, srcs) -> set:
        """Mapuje URL-e obrazów (z bieżącego layoutu) na id atomów-tekstur."""
        out = set()
        for src in srcs:
            sha = self._url_sha.get(src)
            if sha:
                aid = self._sha_atom.get(sha)
                if aid:
                    out.add(aid)
        return out

    def update_reach(self, srcs) -> int:
        """E2: zgłasza substratowi tekstury obecne w bieżącym layoucie (osiągalne)
        i odzyskuje pamięć po teksturach, które reach-GC zżął. Zwraca liczbę
        zwolnionych. No-op bez runtime."""
        rt = self._runtime
        if rt is None:
            return 0
        rt.set_image_reach(self.atom_ids_for_srcs(srcs))
        return self.reap_dropped()

    def reap_dropped(self) -> int:
        """Zwalnia Surface tekstur, których atomy zniknęły z substratu (reach-GC)."""
        rt = self._runtime
        if rt is None:
            return 0
        gone = [sha for sha, aid in self._sha_atom.items() if not rt.has_atom(aid)]
        for sha in gone:
            self._drop_sha(sha)
        return len(gone)

    # ── Budżet pamięci: reach-GC jest główny, LRU bajtowy to twardy bezpiecznik ──
    def _touch_lru(self, sha):
        try:
            self._lru.remove(sha)
        except ValueError:
            pass
        self._lru.append(sha)

    def _drop_sha(self, sha) -> None:
        """Wspólne zwolnienie wszystkich zasobów tekstury (Surface, warianty,
        bajty, wpis LRU, mapowania URL, atom). Używane przez budżet i reach-GC."""
        self._bytes_total -= self._sha_bytes.pop(sha, 0)
        self._orig.pop(sha, None)
        for k in [k for k in self._scaled if k[0] == sha]:
            self._scaled.pop(k, None)
        try:
            self._lru.remove(sha)
        except ValueError:
            pass
        for u in [u for u, s in self._url_sha.items() if s == sha]:
            self._url_sha.pop(u, None)
            self._status[u] = "empty"          # pozwól ponownie pobrać przy następnym widoku
        aid = self._sha_atom.pop(sha, None)
        if aid and self._runtime is not None and self._runtime.has_atom(aid):
            self._runtime.delete_atom(aid)

    def _evict_if_needed(self):
        # Najpierw tniemy warianty skalowane (tanie do odtworzenia).
        if self._bytes_total > self.max_bytes and self._scaled:
            self._scaled.clear()
        # Potem zwalniamy najzimniejsze oryginały, aż zmieścimy się w budżecie.
        while self._bytes_total > self.max_bytes and len(self._lru) > 1:
            self._drop_sha(self._lru[0])

    def shutdown(self):
        self._stop = True
        self._in.put(None)