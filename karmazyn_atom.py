"""
karmazyn_atom.py — Unified Atom Model KarmazynOS v1.0
======================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Jeden model atomu zamiast czterech rozproszonych implementacji.
Zastępuje:
  PhiAtom   (karmazyn_js_phi.py)    — Python variable jako atom JS
  HRRAtom   (karmazyn_hrr.py)       — atom z wektorem HRR
  ręczne T  (karmazyn_dom.py)       — atom.T = ...; atom.state = ...
  ręczne T  (karmazyn_browser.py)   — atom.T = temp; atom.state = ...

Interfejs kompatybilny z istniejącym runtime.py:
  atom.id, atom.S, atom.E, atom.T, atom.state
  atom.touch(), atom.decay(), atom.is_dead(), atom.age()

Nowe możliwości:
  atom.heat(amount)     — bezpośrednie ogrzanie o kwotę
  atom.vector           — opcjonalny wektor HRR (numpy)
  atom.metadata         — dict dla dodatkowych danych
  Atom.from_state(T)    — klasyfikacja stanu z T
  AtomEvent             — zdarzenia emitowane przy zmianie stanu

Użycie:
    from karmazyn_atom import Atom, T_HOT, T_WARM, T_COLD, T_TOMB

    a = Atom("my_atom", S="text:p", E="treść", T=60.0)
    a.touch()          # ogrzej przy dostępie
    a.decay()          # ostudź (wywołuj przez scheduler)
    a.heat(20.0)       # ogrzej o konkretną kwotę
    print(a.state)     # "HOT" / "WARM" / "COLD" / "TOMB"
"""

import time
from typing import Any, Callable, Dict, List, Optional


# ─── Progi termodynamiczne (jeden zestaw dla całego systemu) ──────────────────

T_INIT    = 50.0    # temperatura startowa (WARM)
T_MAX     = 100.0   # temperatura maksymalna
T_HOT     = 70.0    # próg HOT
T_WARM    = 30.0    # próg WARM (poniżej = COLD)
T_TOMB    = 2.0     # próg TOMB / GC

DECAY_DEFAULT = 0.92   # mnożnik przy tick (stygnięcie)
HEAT_READ     = 10.0   # przyrost przy odczycie
HEAT_WRITE    = 5.0    # przyrost przy zapisie (mniejszy niż read)


# ─── Klasyfikacja stanu ───────────────────────────────────────────────────────

def state_for_T(T: float) -> str:
    """Jeden punkt prawdy dla klasyfikacji stanu termodynamicznego."""
    if   T >= T_HOT:  return "HOT"
    elif T >= T_WARM: return "WARM"
    elif T >= T_TOMB: return "COLD"
    return "TOMB"


# ─── Atom ─────────────────────────────────────────────────────────────────────

class Atom:
    """
    Universalny atom KarmazynOS.

    Pola zgodne z istniejącym runtime.py (kompatybilność wsteczna):
      id, S, E, T, state

    Dodatkowe:
      vector   — opcjonalny wektor HRR (numpy ndarray)
      metadata — dict dla specyficznych danych warstwy
      age      — wiek atomu w sekundach
    """

    __slots__ = (
        "id", "S", "E", "T", "state",
        "vector", "metadata",
        "_born", "_reads", "_writes",
        "_on_state_change",
    )

    def __init__(self,
                 id:      str,
                 S:       str   = "",
                 E:       str   = "",
                 T:       float = T_INIT,
                 vector         = None,
                 metadata: Dict[str, Any] = None):
        self.id       = id
        self.S        = S
        self.E        = E
        self.T        = float(T)
        self.state    = state_for_T(self.T)
        self.vector   = vector        # numpy array lub None
        self.metadata = metadata or {}
        self._born    = time.monotonic()
        self._reads   = 0
        self._writes  = 0
        self._on_state_change: Optional[Callable] = None

    # ── Interfejs termodynamiczny ──────────────────────────────────────────────

    def touch(self, weight: float = 1.0) -> None:
        """Ogrzej przy dostępie (odczyt). weight > 1 = intensywniejszy dostęp."""
        self._reads += 1
        self.T = min(T_MAX, self.T + HEAT_READ * weight)
        self._update_state()

    def touch_write(self) -> None:
        """Ogrzej przy zapisie (mniejszy przyrost niż read)."""
        self._writes += 1
        self.T = min(T_MAX, self.T + HEAT_WRITE)
        self._update_state()

    def heat(self, amount: float) -> None:
        """Bezpośrednie ogrzanie o konkretną kwotę."""
        self.T = min(T_MAX, self.T + amount)
        self._update_state()

    def cool(self, amount: float) -> None:
        """Bezpośrednie ochłodzenie o kwotę."""
        self.T = max(0.0, self.T - amount)
        self._update_state()

    def decay(self, rate: float = DECAY_DEFAULT) -> None:
        """Stygnięcie — wywoływane przez scheduler co tick."""
        self.T *= rate
        self._update_state()

    def kill(self) -> None:
        """Natychmiastowe przejście do TOMB (usunięcie węzła DOM itp.)."""
        self.T = T_TOMB * 0.5
        self._update_state()

    def is_dead(self) -> bool:
        return self.T < T_TOMB

    def age(self) -> float:
        """Wiek atomu w sekundach."""
        return time.monotonic() - self._born

    def _update_state(self) -> None:
        new_state = state_for_T(self.T)
        if new_state != self.state:
            self.state = new_state
            if self._on_state_change is not None:
                try:
                    self._on_state_change(self)
                except Exception:
                    pass

    def on_state_change(self, callback: Callable) -> None:
        """Rejestruje callback wywoływany przy zmianie stanu."""
        self._on_state_change = callback

    # ── Właściwości ───────────────────────────────────────────────────────────

    @property
    def is_hot(self)  -> bool: return self.T >= T_HOT
    @property
    def is_warm(self) -> bool: return T_WARM <= self.T < T_HOT
    @property
    def is_cold(self) -> bool: return T_TOMB <= self.T < T_WARM
    @property
    def is_tomb(self) -> bool: return self.T < T_TOMB

    @property
    def has_vector(self) -> bool:
        return self.vector is not None

    # ── Reprezentacja ─────────────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id":    self.id,
            "S":     self.S,
            "E":     self.E,
            "T":     round(self.T, 2),
            "state": self.state,
            "age":   round(self.age(), 1),
        }

    def __repr__(self) -> str:
        return f"Atom({self.id!r}, T={self.T:.1f}, {self.state})"

    def __eq__(self, other) -> bool:
        return isinstance(other, Atom) and self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)


# ─── AtomRegistry — prosty rejestr ───────────────────────────────────────────

class AtomRegistry:
    """
    Minimalny rejestr atomów.
    Używany przez PhiSpace, JSPhi, DOMMapper.
    Nie duplikuje logiki — tylko przechowuje i udostępnia.
    """

    def __init__(self):
        self._atoms: Dict[str, Atom] = {}

    def create(self, id: str, S: str = "", E: str = "",
               T: float = T_INIT, **kwargs) -> Atom:
        a = Atom(id, S, E, T, **kwargs)
        self._atoms[id] = a
        return a

    def get(self, id: str) -> Optional[Atom]:
        return self._atoms.get(id)

    def has(self, id: str) -> bool:
        return id in self._atoms

    def delete(self, id: str) -> bool:
        if id in self._atoms:
            del self._atoms[id]
            return True
        return False

    @property
    def atoms_wrapper(self) -> "AtomsWrapper":
        """Zwraca AtomsWrapper — wspiera atoms() i iterację."""
        return AtomsWrapper(self)

    def atoms(self) -> List[Atom]:
        """Zwraca listę wszystkich żywych atomów."""
        return list(self._atoms.values())

    def tick(self, rate: float = DECAY_DEFAULT) -> List[str]:
        """
        Decay wszystkich atomów. Zwraca id martwych (do GC).
        Wywoływany przez ThermalScheduler.
        """
        dead = []
        for id, atom in list(self._atoms.items()):
            atom.decay(rate)
            if atom.is_dead():
                dead.append(id)
        return dead

    def gc(self, dead_ids: List[str]) -> int:
        """Usuń martwe atomy. Zwraca liczbę usuniętych."""
        for id in dead_ids:
            self._atoms.pop(id, None)
        return len(dead_ids)

    def hot_atoms(self) -> List[Atom]:
        return [a for a in self._atoms.values() if a.is_hot]

    def by_state(self, state: str) -> List[Atom]:
        return [a for a in self._atoms.values() if a.state == state]

    def stats(self) -> Dict[str, int]:
        atoms = list(self._atoms.values())
        return {
            "total": len(atoms),
            "HOT":   sum(1 for a in atoms if a.is_hot),
            "WARM":  sum(1 for a in atoms if a.is_warm),
            "COLD":  sum(1 for a in atoms if a.is_cold),
            "TOMB":  sum(1 for a in atoms if a.is_tomb),
        }

    def __len__(self) -> int:
        return len(self._atoms)

    def __contains__(self, id: str) -> bool:
        return id in self._atoms

    # Kompatybilność z runtime.py: matrix.has_atom(), matrix.atoms()
    def has_atom(self, id: str) -> bool:
        return self.has(id)


class AtomsWrapper:
    """
    Wrapper AtomRegistry wspierający dwa style dostępu:
      matrix.atoms()  — wywołanie jako funkcja (oryginalny API)
      matrix.atoms    — użycie jako property/iterowalny obiekt

    Rozwiązuje niekompatybilność między modułami które używają
    różnych konwencji dostępu do listy atomów.

    Używany przez PhiBuffer, draw_phi_map, zewnętrzne narzędzia.
    """

    def __init__(self, registry: "AtomRegistry"):
        self._reg   = registry
        self._cache: Optional[List["Atom"]] = None
        self._cache_size = -1

    def _get_list(self) -> List["Atom"]:
        """Zwraca listę atomów — z cache jeśli rozmiar się nie zmienił."""
        current_size = len(self._reg._atoms)
        if self._cache is None or self._cache_size != current_size:
            self._cache      = list(self._reg._atoms.values())
            self._cache_size = current_size
        return self._cache

    def __call__(self) -> List["Atom"]:
        """Pozwala wywołać atoms() jako funkcję."""
        return self._get_list()

    def __iter__(self):
        """Pozwala iterować bezpośrednio: for atom in matrix.atoms."""
        return iter(self._reg._atoms.values())

    def __len__(self) -> int:
        return len(self._reg._atoms)

    def __getitem__(self, index: int) -> "Atom":
        return self._get_list()[index]