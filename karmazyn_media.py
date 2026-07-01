"""
karmazyn_media.py — Wspólny szew renderu mediów termicznych
============================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

JEDEN wzorzec dla wszystkich mediów-atomów (obraz, GIF, wideo, wektor). Renderer
i pętla dotykają mediów w jednym miejscu, niezależnie od typu:

  • widoczność = ciepło   — note_visible(kind, aid): rysowanie grzeje atom,
  • draw(ctx, kind, aid, rect) — dyspozytor do providera danego typu,
  • pump()               — krok mediów czasowych (GIF/AVC), z pętli renderu,
  • update_reach(pairs)  — media w layoucie osiągalne; reszta stygnie i ginie.

Provider implementuje draw() i opcjonalnie pump()/note_visible()/reach_ids().
Atom-medium niesie tylko sterowanie cieplne (S='media:*'); ciężki ładunek
(klatki/punkty/tekstury) żyje w providerze. Spójne z E2 (tekstury) — obrazy
mogą zostać zmigrowane do tej samej warstwy bez zmiany kontraktu.
"""

try:
    import pygame
except Exception:
    pygame = None

from karmazyn_vector import ThermalVector


# ─── Providery ────────────────────────────────────────────────────────────────

class GifProvider:
    """Most do ThermalGifPump: rysuje bieżącą klatkę, pompuje czas, grzeje."""
    KIND = "gif"

    def __init__(self, gif_pump):
        self.pump_ = gif_pump

    def note_visible(self, aid, weight=1.0):
        if self.pump_ is not None:
            self.pump_.note_visible(aid, weight)

    def draw(self, ctx, aid, rect):
        if self.pump_ is None:
            return False
        surf = self.pump_.current_surface(aid)
        if surf is None:
            return False
        ctx.blit(surf, rect)
        return True

    def pump(self):
        if self.pump_ is None:
            return 0
        n = self.pump_.pump()
        self.pump_.cleanup()
        return n

    def reach_ids(self, aids):
        if self.pump_ is None:
            return set()
        return {a for a in aids if self.pump_.has(a)}


class VectorProvider:
    """Rysuje łamaną o szczegółowości zależnej od temperatury (ThermalVector)."""
    KIND = "vector"

    def __init__(self, runtime, stroke=(200, 205, 215)):
        self.runtime = runtime
        self.stroke = stroke

    def note_visible(self, aid, weight=1.0):
        atom = self.runtime.get_atom(aid)
        if atom is not None:
            atom.touch(weight)

    def draw(self, ctx, aid, rect):
        atom = self.runtime.get_atom(aid)
        if atom is None:
            return False
        warm = max(0.0, min(1.0, atom.T / 100.0))
        col = tuple(int(c * (0.5 + 0.5 * warm)) for c in self.stroke)
        meta = getattr(atom, "metadata", None) or {}
        polys = meta.get("polylines")
        if polys:
            allpts = [p for poly in polys for p in poly]
            if len(allpts) < 2:
                return False
            tr = self._fit_transform(allpts, rect)
            drew = False
            for poly in polys:
                d = ThermalVector.decimate(poly, atom.T)
                if len(d) < 2:
                    continue
                screen = self._apply(d, tr)
                for i in range(len(screen) - 1):
                    ctx.line(screen[i], screen[i + 1], col, 1)
                drew = True
            return drew
        # pojedyncza łamana (metadata["points"])
        pts = ThermalVector.get_drawable_points_rdp(atom)
        if len(pts) < 2:
            return False
        screen = self._apply(pts, self._fit_transform(pts, rect))
        for i in range(len(screen) - 1):
            ctx.line(screen[i], screen[i + 1], col, 1)
        return True

    @staticmethod
    def _fit_transform(pts, rect):
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        minx, maxx = min(xs), max(xs); miny, maxy = min(ys), max(ys)
        sw = (maxx - minx) or 1.0; sh = (maxy - miny) or 1.0
        s = min(rect.w / sw, rect.h / sh)
        ox = rect.x + (rect.w - sw * s) / 2.0
        oy = rect.y + (rect.h - sh * s) / 2.0
        return (minx, miny, s, ox, oy)

    @staticmethod
    def _apply(pts, tr):
        minx, miny, s, ox, oy = tr
        return [(ox + (x - minx) * s, oy + (y - miny) * s) for x, y in pts]

    def pump(self):
        return 0

    def reach_ids(self, aids):
        return {a for a in aids if self.runtime.has_atom(a)}


# ─── Warstwa ──────────────────────────────────────────────────────────────────

class SpriteProvider:
    """Wycinek arkusza sprite'ów (atlasu) jako medium. Atom niesie źródło i
    prostokąt wycinka (metadata: src, crop=(sx,sy,sw,sh)); provider pobiera pełny
    atlas z ładowarki obrazów i blituje tylko wskazany kawałek. Ciepło z
    widoczności, jak każde medium."""
    KIND = "sprite"

    def __init__(self, runtime):
        self.runtime = runtime
        self.loader = None                 # ustawiany przez viewer (ma ładowarkę)

    def set_loader(self, loader):
        self.loader = loader

    def note_visible(self, aid, weight=1.0):
        atom = self.runtime.get_atom(aid)
        if atom is not None:
            atom.touch(weight)

    def draw(self, ctx, aid, rect):
        atom = self.runtime.get_atom(aid)
        if atom is None or self.loader is None:
            return False
        meta = getattr(atom, "metadata", None) or {}
        src = meta.get("src"); crop = meta.get("crop")
        if not src or not crop:
            return False
        surf, _status = self.loader.get(src, 0, 0)     # pełny atlas (nieskalowany)
        if surf is None:
            return False
        sx, sy, sw, sh = crop
        aw, ah = surf.get_width(), surf.get_height()
        # background-size (retina): CSS podaje pozycję/rozmiar w px logicznych, a
        # atlas bywa większy (np. _hr = 2x). Skalujemy crop do pikseli atlasu.
        size = meta.get("size")
        if size and size[0] > 0 and size[1] > 0:
            fx, fy = aw / size[0], ah / size[1]
            sx, sy, sw, sh = sx * fx, sy * fy, sw * fx, sh * fy
        sx, sy, sw, sh = int(round(sx)), int(round(sy)), int(round(sw)), int(round(sh))
        if sx >= aw or sy >= ah or sw <= 0 or sh <= 0:
            return False
        sw = min(sw, aw - sx); sh = min(sh, ah - sy)     # przytnij do atlasu
        ctx.blit_crop(surf, (sx, sy, sw, sh), rect)
        return True

    def pump(self):
        return 0

    def reach_ids(self, aids):
        return {a for a in aids if self.runtime.has_atom(a)}


class MediaLayer:
    def __init__(self, runtime):
        self.runtime = runtime
        self._providers = {}                  # kind -> provider

    def register(self, provider):
        self._providers[provider.KIND] = provider
        return self

    def provider_for(self, kind):
        return self._providers.get(kind)

    def note_visible(self, kind, aid, weight=1.0):
        p = self._providers.get(kind)
        if p is not None:
            p.note_visible(aid, weight)

    def draw(self, ctx, kind, aid, rect):
        p = self._providers.get(kind)
        return bool(p and p.draw(ctx, aid, rect))

    def pump(self):
        """Krok wszystkich mediów czasowych. Z pętli renderu (co klatkę)."""
        n = 0
        for p in self._providers.values():
            try:
                n += p.pump() or 0
            except Exception:
                pass
        return n

    def update_reach(self, pairs):
        """pairs: iterowalne (kind, aid) mediów obecnych w bieżącym layoucie.
        Ustawia osiągalność 'media' w substracie — reszta stygnie i jest żęta."""
        ids = set()
        for kind, aid in pairs:
            p = self._providers.get(kind)
            if p is not None:
                ids |= p.reach_ids([aid])
        self.runtime.set_reach("media", ids)