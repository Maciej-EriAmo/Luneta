"""
karmazyn_vector.py — Termiczna Geometria dla Lunety
====================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Szczegółowość geometrii (LOD) degradowana temperaturą atomu: gorący kształt
rysowany w pełnej rozdzielczości, zimny — zgrubnie, poniżej TOMB znika.
Wektorowy odpowiednik E2 (tekstury) i oscylatora GIF.

ZAKRES: to jest LOD nad GOTOWĄ łamaną [(x,y), ...], NIE parser SVG. Punkty
muszą być wcześniej wyekstrahowane (parser ścieżek / rasteryzacja). Geometria
żyje w atom.metadata["points"]; atom.E pozostaje stringiem-etykietą (E jest
w systemie tekstem — bramka czytelności, hot_content, heatmapa).

Dwa tryby:
  • get_drawable_points  — stride termiczny (wierny pierwotnemu szkicowi, tani,
    ale gubi punkty niezależnie od ich znaczenia geometrycznego).
  • get_drawable_points_rdp — Douglas-Peucker z progiem epsilon sterowanym
    temperaturą: usuwa punkty wnoszące najmniej do sylwetki, więc przy tym
    samym budżecie kształt wygląda lepiej (perceptual decimation).
"""

import math
from karmazyn_atom import Atom

T_TOMB = 2.0      # poniżej — kształt znika
T_FULL = 80.0     # od tej T pełna szczegółowość


class ThermalVector:

    # ── Wspólne: wydobycie punktów (z metadata, nie z E) ─────────────────────
    @staticmethod
    def _points_of(atom: Atom) -> list:
        meta = getattr(atom, "metadata", None) or {}
        pts = meta.get("points")
        # Zgodność wsteczna ze szkicem, gdyby ktoś trzymał punkty w E:
        if pts is None and isinstance(getattr(atom, "E", None), (list, tuple)):
            pts = atom.E
        return list(pts) if pts else []

    @staticmethod
    def _ensure_ends(decimated: list, base: list) -> list:
        """Zawsze zachowaj pierwszy i ostatni punkt oryginału (sylwetka/zamknięcie)."""
        if not decimated:
            return decimated
        if decimated[0] != base[0]:
            decimated.insert(0, base[0])
        if decimated[-1] != base[-1]:
            decimated.append(base[-1])
        return decimated

    # ── Tryb 1: stride termiczny (wierny szkicowi) ───────────────────────────
    @staticmethod
    def lod_step(T: float, n: int) -> int:
        if T >= T_FULL:   return 1               # 100% szczegółowości
        if T >= 50.0:     return 2               # 50%
        if T >= 20.0:     return max(3, n // 6)  # low-poly
        return max(4, n // 4)                    # ostatnie tchnienie

    @staticmethod
    def get_drawable_points(atom: Atom) -> list:
        """Termicznie zdegenerowana lista punktów (stride). [] gdy zbyt zimne."""
        if atom.T < T_TOMB:
            return []
        base = ThermalVector._points_of(atom)
        n = len(base)
        if n <= 2:
            return list(base)
        step = ThermalVector.lod_step(atom.T, n)
        return ThermalVector._ensure_ends(base[::step], base)

    # ── Tryb 2: Douglas-Peucker z epsilon sterowanym temperaturą ─────────────
    @staticmethod
    def _bbox_diag(pts: list) -> float:
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        return math.hypot(max(xs) - min(xs), max(ys) - min(ys)) or 1.0

    @staticmethod
    def _perp_dist(p, a, b) -> float:
        (px, py), (ax, ay), (bx, by) = p, a, b
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        # odległość punktu od odcinka a–b
        t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
        t = max(0.0, min(1.0, t))
        cx, cy = ax + t * dx, ay + t * dy
        return math.hypot(px - cx, py - cy)

    @staticmethod
    def _rdp(points: list, eps: float) -> list:
        if len(points) < 3:
            return list(points)
        a, b = points[0], points[-1]
        dmax, idx = 0.0, 0
        for i in range(1, len(points) - 1):
            d = ThermalVector._perp_dist(points[i], a, b)
            if d > dmax:
                dmax, idx = d, i
        if dmax > eps:
            left = ThermalVector._rdp(points[:idx + 1], eps)
            right = ThermalVector._rdp(points[idx:], eps)
            return left[:-1] + right
        return [a, b]

    @staticmethod
    def thermal_epsilon(T: float, diag: float) -> float:
        """Gorące -> eps≈0 (pełny detal); zimne -> eps duże (agresywne uproszczenie)."""
        warmth = max(0.0, min(1.0, T / T_FULL))
        return (1.0 - warmth) * 0.06 * diag

    @staticmethod
    def get_drawable_points_rdp(atom: Atom) -> list:
        """LOD zachowujący sylwetkę: Douglas-Peucker z progiem od temperatury."""
        if atom.T < T_TOMB:
            return []
        base = ThermalVector._points_of(atom)
        if len(base) <= 2:
            return list(base)
        eps = ThermalVector.thermal_epsilon(atom.T, ThermalVector._bbox_diag(base))
        if eps <= 0.0:
            return list(base)
        return ThermalVector._rdp(base, eps)
