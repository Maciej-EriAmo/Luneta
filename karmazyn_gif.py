"""
karmazyn_gif.py — Termiczny Oscylator GIF dla silnika Karmin
=============================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

GIF = JEDEN atom-oscylator (S="media:gif"). Klatki przesuwają się TYLKO gdy
atom jest gorący; gdy stygnie poniżej COLD (T < FREEZE_T) — zamarza na bieżącej
klatce. Niewidoczny GIF nie kosztuje procesora.

DWIE korekty względem pierwotnego szkicu, obie konieczne dla spójności:

  • CIEPŁO Z WIDOCZNOŚCI, NIE Z POMPY. Pierwotnie pump() robił touch_write()
    przy każdej zmianie klatki — to SAMO-grzanie: atom nigdy by nie wystygł,
    więc nigdy by nie zamarł, co przeczy całej tezie. Tu pompa tylko KONSUMUJE
    temperaturę (przesuwa, gdy gorąco; zamarza, gdy zimno). Energię dostarcza
    renderer przez note_visible() przy rysowaniu — dokładnie jak tekstury w E2.

  • E ATOMU TO STRING. Klatki nie trafiają do atom.E (E jest używane jako tekst:
    bramka czytelności, _readable, hot_content, etykieta heatmapy). Surowe klatki
    żyją w pompie; atom niesie wyłącznie sterowanie cieplne.

Klatki są konwertowane na pygame.Surface JEDNORAZOWO przy ładowaniu (sprite
sheet w RAM), więc rysowanie to czysty blit bez konwersji per-klatka.

Łagodna degradacja: bez PIL/pygame moduł importuje się, AVAILABLE=False,
a load_gif zwraca False.
"""

import io
import time

from karmazyn_atom import AtomRegistry, T_HOT

try:
    import pygame
    from PIL import Image
    AVAILABLE = True
except Exception:
    pygame = None
    Image = None
    AVAILABLE = False

# Poniżej tej temperatury oscylator zamarza (= T_WARM substratu).
FREEZE_T = 30.0


class ThermalGifPump:
    """Pompa klatek napędzana temperaturą atomów. Trzyma sprite-sheet (listę
    Surface) per GIF; przesuwa indeks tylko dla gorących atomów."""

    def __init__(self, registry: AtomRegistry):
        self.reg = registry
        self._gifs = {}     # atom_id -> {surfaces, delays, idx, last_swap, label}

    # ── Ładowanie ────────────────────────────────────────────────────────────
    def load_gif(self, atom_id: str, source, label: str = None) -> bool:
        """Dekoduje GIF (ścieżka lub bajty) na listę pygame.Surface i rejestruje
        atom-oscylator (start: HOT). Zwraca True przy powodzeniu."""
        if not AVAILABLE:
            return False
        try:
            src = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
            img = Image.open(src)
            surfaces, delays = [], []
            while True:
                # convert('RGBA') komponuje klatkę (disposal/przezroczystość GIF-a);
                # samo .copy() zostawiłoby częściową klatkę w trybie 'P'.
                frame = img.convert("RGBA")
                surf = pygame.image.fromstring(frame.tobytes(), frame.size, "RGBA")
                try:
                    surf = surf.convert_alpha()
                except Exception:
                    pass                       # brak zainicjalizowanego display — zostaw surowy
                surfaces.append(surf)
                # GIF duration w ms; 0/None -> ~100 ms; dolny limit chroni przed busy-loop
                delays.append(max(0.02, img.info.get("duration", 100) / 1000.0))
                img.seek(img.tell() + 1)
        except EOFError:
            pass
        except Exception as e:
            print(f"[GIF] nie udało się załadować: {e}")
            return False

        if not surfaces:
            return False
        self._gifs[atom_id] = {
            "surfaces": surfaces,
            "delays": delays,
            "idx": 0,
            "last_swap": time.time(),
            "label": label or "gif",
        }
        if not self.reg.has(atom_id):
            self.reg.create(atom_id, S="media:gif", E=(label or "gif"), T=T_HOT)
        return True

    # ── Krok oscylatora (pętla renderu, NIE 1-sekundowy tick substratu) ───────
    def pump(self) -> int:
        """Przesuwa klatkę gorących GIF-ów wg ich delaya. Zimne (T<FREEZE_T)
        zamarzają — pomijane bez kosztu. Zwraca liczbę przesuniętych GIF-ów."""
        now = time.time()
        advanced = 0
        for aid, d in self._gifs.items():
            if not self.reg.has(aid):
                continue                       # atom zżęty przez reach-GC — pomiń
            atom = self.reg.get(aid)
            if atom is None or atom.T < FREEZE_T:
                continue                       # ZAMARZA: zero pracy dla niewidocznych
            n = len(d["surfaces"])
            if n <= 1:
                continue
            if now - d["last_swap"] >= d["delays"][d["idx"]]:
                d["idx"] = (d["idx"] + 1) % n
                d["last_swap"] = now
                advanced += 1
        return advanced

    def cleanup(self) -> int:
        """Wyrzuca z RAM klatki GIF-ów, których atomy zostały zżęte. Zwraca
        liczbę zwolnionych."""
        dead = [aid for aid in self._gifs if not self.reg.has(aid)]
        for aid in dead:
            del self._gifs[aid]
        return len(dead)

    # ── Most do renderera ────────────────────────────────────────────────────
    def note_visible(self, atom_id: str, weight: float = 1.0) -> None:
        """WIDOCZNOŚĆ = CIEPŁO. Renderer woła to przy rysowaniu pudełka GIF-a —
        to jedyne źródło energii oscylatora. Bez tego GIF stygnie i zamarza."""
        if self.reg.has(atom_id):
            atom = self.reg.get(atom_id)
            if atom is not None:
                atom.touch(weight)

    def current_surface(self, atom_id):
        """Bieżąca klatka jako pygame.Surface (do blitu) albo None."""
        d = self._gifs.get(atom_id)
        if not d or not d["surfaces"]:
            return None
        return d["surfaces"][d["idx"]]

    def current_index(self, atom_id: str) -> int:
        d = self._gifs.get(atom_id)
        return d["idx"] if d else -1

    def frame_count(self, atom_id: str) -> int:
        d = self._gifs.get(atom_id)
        return len(d["surfaces"]) if d else 0

    def has(self, atom_id: str) -> bool:
        return atom_id in self._gifs
