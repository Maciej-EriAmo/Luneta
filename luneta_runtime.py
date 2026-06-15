"""
luneta_runtime.py — Lightweight Runtime dla Lunety v1.0
========================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Lekki adapter runtime wystarczający do uruchomienia przeglądarki Luneta
bez pełnego SanctuaryRuntime i jego zależności (numpy, HSSKarmazynMatrix,
PhiPhysics, phi_space, karmazyn_ui).

Oparty na karmazyn_atom.py (unified model).
AtomRegistry jako matrix, proste Bubble/Hologram jako kontenery.

Interfejs kompatybilny z tym czego używają:
  karmazyn_browser.py  — create_atom, get_atom, matrix.has_atom
  karmazyn_dom.py      — create_atom, get_atom, get_bubble, consolidate,
                         import_to_bubble, archive_to_hologram, matrix.has_atom
  karmazyn_js_web.py   — peek_atom, create_atom

Użycie:
    from luneta_runtime import LunetaRuntime
    from karmazyn_browser import KarmazynBrowser

    runtime = LunetaRuntime()
    browser = KarmazynBrowser(runtime)
    ok, msg = browser.go("https://example.com")
    print(msg)
"""

import time
from typing import Any, Dict, List, Optional

from karmazyn_atom import (
    Atom, AtomRegistry,
    T_INIT, T_MAX, T_HOT, T_WARM, T_TOMB,
    DECAY_DEFAULT,
)
from karmazyn_substrate import Store as KarmazynEngine


# ─── Bubble — lekka wersja ───────────────────────────────────────────────────

class LunetaBubble:
    """
    Lekki bąbel — kontener atomów bez geometrii sferycznej.
    Wystarczający dla DOMMapper i JSBridge.
    """

    def __init__(self, label: str, content: str = "", immortal: bool = False):
        self.label    = label
        self.content  = content
        self.immortal = immortal
        self.atom_ids: List[str] = []
        self._born    = time.monotonic()

    def absorb(self, atom: Atom) -> None:
        """Wchłania atom do bąbla."""
        if atom.id not in self.atom_ids:
            self.atom_ids.append(atom.id)
        self.content = f"{self.content} {atom.S} {atom.E}".strip()

    def liveliness(self, runtime: "LunetaRuntime") -> float:
        """Żywotność bąbla — średnia T atomów / T_MAX."""
        if not self.atom_ids:
            return 0.0
        total_T = 0.0
        count   = 0
        for aid in self.atom_ids:
            atom = runtime.get_atom(aid)
            if atom and not atom.is_dead():
                total_T += atom.T
                count   += 1
        if count == 0:
            return 0.0
        return total_T / (count * T_MAX)

    def __repr__(self) -> str:
        return f"LunetaBubble({self.label!r}, atoms={len(self.atom_ids)})"


# ─── LunetaRuntime ───────────────────────────────────────────────────────────

class LunetaRuntime:
    """
    Lekki runtime dla Lunety.

    Zapewnia interfejs kompatybilny z SanctuaryRuntime
    bez zależności od numpy, HSSKarmazynMatrix, PhiPhysics.

    Pola:
      matrix  — AtomRegistry (kompatybilne z matrix.has_atom, matrix.atoms)
    """

    def __init__(self):
        self._bubbles:  Dict[str, LunetaBubble] = {}
        self._holograms: Dict[str, Dict[str, Any]] = {}
        # Silnik: kanoniczny substrat z reach-GC, nie goły AtomRegistry.
        # Osiągalność Lunety = atomy trzymane przez bąble-kontenery (płaskie
        # członkostwo). Atom w bąblu przeżywa stygnięcie; sierota ginie.
        self.engine = KarmazynEngine(
            thermal=True,
            extra_reach=lambda: {aid for b in self._bubbles.values() for aid in b.atom_ids},
        )
        # alias: konsumenci (browser/dom/js_web) wołają matrix.has_atom/atoms/get/stats
        self.matrix = self.engine.reg

    # ── Atomy ─────────────────────────────────────────────────────────────────

    def create_atom(self, id: str, S: str, E: str, T: float,
                    decay_rate: float = 0.0, **kwargs) -> Atom:
        """Tworzy atom w rejestrze. Kompatybilne z SanctuaryRuntime."""
        if self.matrix.has(id):
            raise ValueError(f"Atom '{id}' już istnieje")
        atom = self.matrix.create(id, S=S, E=E, T=T, **kwargs)
        return atom

    def get_atom(self, id: str) -> Optional[Atom]:
        """Zwraca atom lub None."""
        return self.matrix.get(id)

    def peek_atom(self, id: str) -> Optional[Atom]:
        """Zwraca atom bez ogrzewania (peek). Alias get_atom dla Lunety."""
        return self.matrix.get(id)

    def has_atom(self, id: str) -> bool:
        return self.matrix.has(id)

    def delete_atom(self, id: str) -> Optional[Atom]:
        atom = self.matrix.get(id)
        if atom:
            atom.kill()
            self.matrix.delete(id)               # usuń z rejestru — nie czekaj na GC
            for b in self._bubbles.values():      # i z bąbli, które go trzymały
                if id in b.atom_ids:
                    b.atom_ids.remove(id)
        return atom

    def list_atoms(self, layer: str = None,
                   visible_only: bool = True) -> List[Atom]:
        atoms = self.matrix.atoms()
        if layer:
            atoms = [a for a in atoms if a.state == layer]
        if visible_only:
            atoms = [a for a in atoms if a.state != "TOMB"]
        return atoms

    # ── Bąble ─────────────────────────────────────────────────────────────────

    def consolidate(self, label: str, metadata: dict = None) -> str:
        """Tworzy bąbel z atomu. Zwraca bubble_label."""
        if label in self._bubbles:
            return f"bubble_{label}"
        atom = self.matrix.get(label)
        if atom is None:
            raise ValueError(f"consolidate: atom '{label}' nie istnieje")
        bubble = LunetaBubble(
            label=label,
            content=f"{atom.S} {atom.E}".strip(),
        )
        bubble.atom_ids.append(label)
        self._bubbles[label] = bubble
        return f"bubble_{label}"

    def get_bubble(self, label: str) -> Optional[LunetaBubble]:
        return self._bubbles.get(label)

    def create_bubble(self, label: str, atom_ids: List[str]) -> Optional[str]:
        """
        Tworzy NAZWANY bąbel-kontener pod etykietą `label` i wchłania członków.

        Członkami mogą być atomy LUB inne bąble (zagnieżdżenie) — tabela to
        bąbel-wierszy, wiersz to bąbel-komórek. Wcześniej create_bubble
        filtrował tylko po has_atom, więc bąbel tabeli (członkowie = bąble
        wierszy) nie powstawał.

        Różni się od consolidate(): consolidate(atom_id) tworzy bąbel nazwany
        ID atomu. create_bubble(label, ids) tworzy bąbel nazwany `label`
        zawierający wielu członków (atomy i/lub zagnieżdżone bąble).
        """
        members = []
        for aid in atom_ids:
            if self.matrix.has(aid):
                members.append(("atom", aid))
            elif aid in self._bubbles:
                members.append(("bubble", aid))
        if not members:
            return None
        bubble = self._bubbles.get(label)
        if bubble is None:
            bubble = LunetaBubble(label=label)
            self._bubbles[label] = bubble
        for kind, aid in members:
            if kind == "atom":
                atom = self.matrix.get(aid)
                if atom is not None:
                    bubble.absorb(atom)
            else:  # zagnieżdżony bąbel — referencja bez wchłaniania treści
                if aid not in bubble.atom_ids:
                    bubble.atom_ids.append(aid)
        return label

    def import_to_bubble(self, bubble_label: str, atom_id: str) -> bool:
        """Importuje atom do istniejącego bąbla."""
        bubble = self._bubbles.get(bubble_label)
        if bubble is None:
            return False
        atom = self.matrix.get(atom_id)
        if atom is None:
            return False
        bubble.absorb(atom)
        return True

    # ── Hologramy ─────────────────────────────────────────────────────────────

    def archive_to_hologram(self, topic: str, atom_ids: List[str],
                            remove_originals: bool = False,
                            n_components: int = 5) -> str:
        """
        Lekka wersja hologramu — dict z metadanymi, bez PCA/eigenvectors.
        Wystarczający dla DOMMapper (potrzebuje tylko hid).
        """
        import hashlib
        valid_ids = [aid for aid in atom_ids if self.matrix.has(aid)]
        if not valid_ids:
            raise ValueError(f"archive_to_hologram '{topic}': brak atomów")

        hid = f"idea_{topic}_{int(time.monotonic())}_{hashlib.md5(topic.encode()).hexdigest()[:6]}"
        self._holograms[hid] = {
            "id":          hid,
            "topic":       topic,
            "atom_labels": valid_ids,
            "created_at":  time.monotonic(),
        }

        if remove_originals:
            for aid in valid_ids:
                self._bubbles.pop(aid, None)

        return hid

    # ── Termodynamika ─────────────────────────────────────────────────────────

    def step(self, n: int = 1) -> Dict[str, Any]:
        """Tick termodynamiczny — decay + BEZPIECZNY reach-GC (silnik substratu).

        Atom trzymany przez bąbel jest osiągalny → przeżywa stygnięcie (archiwum).
        Tylko atom-sierota (w żadnym bąblu) ginie, gdy ostygnie. Naprawia bug,
        w którym GC po samej temperaturze kasował treść strony spod bąbla.
        """
        for _ in range(n):
            self.engine.tick()
        return self.status_summary()

    def status_summary(self) -> Dict[str, int]:
        return self.matrix.stats()

    # ── Reprezentacja ─────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        s = self.matrix.stats()
        return (f"LunetaRuntime(atoms={s['total']}, "
                f"HOT={s['HOT']}, WARM={s['WARM']}, "
                f"COLD={s['COLD']}, bubbles={len(self._bubbles)})")
