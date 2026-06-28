"""
karmazyn_atom.py — Unified Atom Model KarmazynOS v1.2
======================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Wersja 1.2: Dodano zliczanie cykli (ticków) na poziomie Rejestru.
Pasek narzędzi HUD wreszcie otrzyma bieżącą statystykę postępu czasu.
"""

import time
from typing import Any, Callable, Dict, List, Optional


# ─── Progi termodynamiczne (jeden zestaw dla całego systemu) ──────────────────

T_INIT    = 50.0    # temperatura startowa (WARM)
T_MAX     = 100.0   # temperatura maksymalna
T_HOT     = 70.0    # próg HOT
T_WARM    = 30.0    # próg WARM (poniżej = COLD)
T_TOMB    = 2.0     # próg TOMB / przekazanie do Vacuum

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
        self.vector   = vector        
        self.metadata = metadata or {}
        self._born    = time.monotonic()
        self._reads   = 0
        self._writes  = 0
        self._on_state_change: Optional[Callable] = None

    # ── Interfejs termodynamiczny ──────────────────────────────────────────────

    def touch(self, weight: float = 1.0) -> None:
        self._reads += 1
        self.T = min(T_MAX, self.T + HEAT_READ * weight)
        self._update_state()

    def touch_write(self) -> None:
        self._writes += 1
        self.T = min(T_MAX, self.T + HEAT_WRITE)
        self._update_state()

    def heat(self, amount: float) -> None:
        self.T = min(T_MAX, self.T + amount)
        self._update_state()

    def cool(self, amount: float) -> None:
        self.T = max(0.0, self.T - amount)
        self._update_state()

    def decay(self, rate: float = DECAY_DEFAULT) -> None:
        self.T *= rate
        self._update_state()

    def kill(self) -> None:
        self.T = T_TOMB * 0.5
        self._update_state()

    def is_dead(self) -> bool:
        return self.T < T_TOMB

    def age(self) -> float:
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
    Posiada strefę _vacuum do zarządzania cyklem rozpadu oraz
    zlicza instancje wywołania tick() na użytek telemetrii.
    """

    def __init__(self):
        self._atoms: Dict[str, Atom] = {}
        self._vacuum: Dict[str, Atom] = {} 
        self._generation: int = 0
        self._tick_count: int = 0

    def create(self, id: str, S: str = "", E: str = "",
               T: float = T_INIT, **kwargs) -> Atom:
        a = Atom(id, S, E, T, **kwargs)
        self._atoms[id] = a
        self._generation += 1
        return a

    def get(self, id: str) -> Optional[Atom]:
        return self._atoms.get(id)

    def has(self, id: str) -> bool:
        return id in self._atoms

    def delete(self, id: str) -> bool:
        if id in self._atoms:
            del self._atoms[id]
            self._generation += 1
            return True
        return False

    @property
    def atoms_wrapper(self) -> "AtomsWrapper":
        return AtomsWrapper(self)

    def atoms(self) -> List[Atom]:
        return list(self._atoms.values())

    def tick(self, rate: float = DECAY_DEFAULT) -> List[str]:
        self._tick_count += 1
        dead = []
        
        for id, atom in list(self._atoms.items()):
            atom.decay(rate)
            if atom.is_dead():
                dead.append(id)
                
        if dead:
            self.gc(dead)
            
        vacuum_dust = []
        for id, atom in list(self._vacuum.items()):
            atom.decay(rate)
            if atom.T < 0.1:
                vacuum_dust.append(id)
                
        for id in vacuum_dust:
            del self._vacuum[id]

        return dead

    def gc(self, dead_ids: List[str]) -> int:
        removed = 0
        for id in dead_ids:
            atom = self._atoms.pop(id, None)
            if atom is not None:
                self._vacuum[id] = atom
                removed += 1
        if removed > 0:
            self._generation += 1
        return removed

    def hot_atoms(self) -> List[Atom]:
        return [a for a in self._atoms.values() if a.is_hot]

    def by_state(self, state: str) -> List[Atom]:
        return [a for a in self._atoms.values() if a.state == state]

    def stats(self) -> Dict[str, int]:
        atoms = list(self._atoms.values())
        return {
            "total": len(atoms) + len(self._vacuum),
            "HOT":   sum(1 for a in atoms if a.is_hot),
            "WARM":  sum(1 for a in atoms if a.is_warm),
            "COLD":  sum(1 for a in atoms if a.is_cold),
            "TOMB":  len(self._vacuum),
            "tick":  self._tick_count,
        }

    def __len__(self) -> int:
        return len(self._atoms)

    def __contains__(self, id: str) -> bool:
        return id in self._atoms

    def has_atom(self, id: str) -> bool:
        return self.has(id)


class AtomsWrapper:
    """
    Wrapper AtomRegistry wspierający dwa style dostępu.
    """

    def __init__(self, registry: "AtomRegistry"):
        self._reg   = registry
        self._cache: Optional[List["Atom"]] = None
        self._cache_gen = -1

    def _get_list(self) -> List["Atom"]:
        current_gen = self._reg._generation
        if self._cache is None or self._cache_gen != current_gen:
            self._cache     = list(self._reg._atoms.values())
            self._cache_gen = current_gen
        return self._cache

    def __call__(self) -> List["Atom"]:
        return self._get_list()

    def __iter__(self):
        return iter(self._reg._atoms.values())

    def __len__(self) -> int:
        return len(self._reg._atoms)

    def __getitem__(self, index: int) -> "Atom":
        return self._get_list()[index]