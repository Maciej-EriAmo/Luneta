"""
karmazyn_js_phi.py — KarmazynJS Phi Layer v1.0
================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Warstwa phi-space nad czystym interpreterem JS.
Dodaje termodynamikę, GC, sandbox i anomaly detection
bez dotykania interpretera.

Jedna zasada: KarmazynJSCore nie wie że istnieje phi-space.
KarmazynJSPhi nie wie jak interpretować JS.

Warstwa phi-space:
  PhiScope  — Scope z temperaturą atomów
  PhiAtom   — wartość JS jako atom phi-space
  KarmazynJSPhi — Core + phi-space

Bezpieczeństwo przez strukturę:
  sandbox() → izolowany phi-space bez referencji do parent
  "Ucieczka" z bąbla → pusty phi-space (próżnia)
  Nie polityka — ontologia.

Anomaly detection (darmowe z termodynamiki):
  Kod który za dużo czyta bez produkcji → podejrzany
  Bąbel który rośnie bez ograniczeń → leak
  Nieskończona pętla → op_count limit w Core
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from karmazyn_js_core import (
    KarmazynJSCore, Scope, Function,
    _Return, _Break, _Continue, _Throw,
)


# ─── PhiAtom ─────────────────────────────────────────────────────────────────

class PhiAtom:
    """
    Wartość JS jako atom phi-space.

    T (temperatura) = częstotliwość dostępu.
    Gorący atom = używany często = trzymaj w RAM.
    Zimny atom = zapomniany = kandydat GC.

    To jest JIT profiling za darmo — silnik wie które zmienne
    są hot path bez osobnego profilera.
    """

    T_INIT  = 50.0    # temperatura startowa (WARM)
    T_MAX   = 100.0
    T_HEAT  = 15.0    # przyrost przy dostępie
    T_DECAY = 0.92    # mnożnik przy tick
    T_TOMB  = 2.0     # próg GC

    __slots__ = ("value", "name", "T", "state", "_reads", "_writes", "_born")

    def __init__(self, value: Any, name: str = ""):
        self.value   = value
        self.name    = name
        self.T       = self.T_INIT
        self.state   = "WARM"
        self._reads  = 0
        self._writes = 0
        self._born   = time.monotonic()

    def touch_read(self) -> None:
        self._reads += 1
        self.T = min(self.T_MAX, self.T + self.T_HEAT)
        self._sync_state()

    def touch_write(self) -> None:
        self._writes += 1
        self.T = min(self.T_MAX, self.T + self.T_HEAT * 0.5)
        self._sync_state()

    def decay(self) -> None:
        self.T *= self.T_DECAY
        self._sync_state()

    def _sync_state(self) -> None:
        if   self.T >= 70: self.state = "HOT"
        elif self.T >= 30: self.state = "WARM"
        elif self.T >= self.T_TOMB: self.state = "COLD"
        else: self.state = "TOMB"

    def is_dead(self) -> bool:
        return self.T < self.T_TOMB

    def age(self) -> float:
        return time.monotonic() - self._born

    def __repr__(self) -> str:
        return (f"PhiAtom({self.name!r}={self.value!r}, "
                f"T={self.T:.1f}, {self.state})")


# ─── PhiScope ─────────────────────────────────────────────────────────────────

class PhiScope(Scope):
    """
    Scope z termodynamiką.
    Każda zmienna to PhiAtom — dostęp ogrzewa, brak dostępu stygnie.

    Rozszerza Scope z Core — Core nie wie o temperaturach.
    """

    def __init__(self, parent: Optional["PhiScope"] = None):
        super().__init__(parent)
        self._atoms: Dict[str, PhiAtom] = {}
        self.children: List["PhiScope"] = []
        if parent is not None and isinstance(parent, PhiScope):
            parent.children.append(self)

    def get(self, name: str) -> Any:
        if name in self._atoms:
            atom = self._atoms[name]
            atom.touch_read()
            return atom.value
        if name in self.vars:
            return self.vars[name]
        if self.parent is not None:
            return self.parent.get(name)
        raise NameError(f"'{name}' is not defined")

    def set(self, name: str, value: Any) -> None:
        """Tworzy zmienną w bieżącym scope jako PhiAtom."""
        if name in self._atoms:
            atom = self._atoms[name]
            atom.value = value
            atom.touch_write()
        else:
            self._atoms[name] = PhiAtom(value, name)

    def assign(self, name: str, value: Any) -> None:
        """Przypisanie do istniejącej zmiennej przez scope chain."""
        if name in self._atoms:
            atom = self._atoms[name]
            atom.value = value
            atom.touch_write()
            return
        if name in self.vars:
            self.vars[name] = value
            return
        if self.parent is not None:
            self.parent.assign(name, value)
            return
        raise NameError(f"'{name}' is not defined")

    def child(self, name: str = "") -> "PhiScope":
        """Zwraca nowy PhiScope zagnieżdżony w bieżącym."""
        return PhiScope(parent=self)

    def detach_child(self, child_scope: "PhiScope") -> None:
        """Odłącza dziecko z listy children."""
        if child_scope in self.children:
            self.children.remove(child_scope)

    def tick(self) -> List[str]:
        """
        Decay wszystkich atomów w tym scope. Zwraca nazwy martwych.
        Nie chodzi rekurencyjnie — _tick_scope w VM robi to.
        """
        dead = []
        for name, atom in list(self._atoms.items()):
            atom.decay()
            if atom.is_dead():
                dead.append(name)
        return dead

    def gc(self, dead_names: List[str]) -> int:
        """Usuwa martwe atomy. Zwraca liczbę usuniętych."""
        count = 0
        for name in dead_names:
            if name in self._atoms:
                del self._atoms[name]
                count += 1
        return count

    def atom_count(self) -> int:
        return len(self._atoms)

    def hot_atoms(self) -> List[PhiAtom]:
        return [a for a in self._atoms.values() if a.state == "HOT"]

    def cold_atoms(self) -> List[PhiAtom]:
        return [a for a in self._atoms.values() if a.state == "COLD"]

    def thermal_map(self) -> Dict[str, Dict[str, Any]]:
        """Mapa temperatur zmiennych w tym scope."""
        result = {}
        for name, atom in self._atoms.items():
            result[name] = {
                "T":      round(atom.T, 1),
                "state":  atom.state,
                "reads":  atom._reads,
                "writes": atom._writes,
                "age":    round(atom.age(), 1),
            }
        return result


# ─── KarmazynJSPhi ───────────────────────────────────────────────────────────

class KarmazynJSPhi(KarmazynJSCore):
    """
    Interpreter JS z phi-space.
    Dziedziczy Core i zastępuje jedną rzecz: Scope → PhiScope.

    Core nie wie że istnieje PhiScope.
    PhiScope nie wie jak interpretować JS.
    Separation of concerns jest formalny.
    """

    def __init__(self, runtime=None, context: str = "global",
                 name: str = None):
        super().__init__()
        # Zamień global_scope na PhiScope
        old_vars = self.global_scope.vars.copy()
        self.global_scope = PhiScope()
        # Przenieś builtiny do nowego scope (jako zwykłe vars, nie atoms)
        self.global_scope.vars.update(old_vars)

        self._runtime = runtime
        self._context = name or context  # name = alias kompatybilności z js_web
        self._tick_n  = 0
        self._total_gc = 0

    # ── Tick — stygnięcie + GC ────────────────────────────────────────────────

    def tick(self) -> Dict[str, Any]:
        """
        Jeden tick termodynamiczny: decay + GC w całym drzewie scope.
        Wywoływany przez scheduler lub ręcznie (JS TICK).
        """
        self._tick_n += 1
        collected = self._tick_scope(self.global_scope)
        self._total_gc += collected
        return {
            "tick":      self._tick_n,
            "collected": collected,
            "total_gc":  self._total_gc,
        }

    def _tick_scope(self, scope) -> int:
        """Rekurencyjny tick przez drzewo scope."""
        if not isinstance(scope, PhiScope):
            return 0
        dead = scope.tick()
        collected = scope.gc(dead)
        for child in list(scope.children):
            collected += self._tick_scope(child)
        return collected

    # ── Profiling ─────────────────────────────────────────────────────────────

    def thermal_map(self) -> Dict[str, Dict[str, Any]]:
        """Pełna mapa temperatur w global scope."""
        if isinstance(self.global_scope, PhiScope):
            return self.global_scope.thermal_map()
        return {}

    def anomaly_score(self) -> float:
        """
        Detekcja anomalii — darmowa z termodynamiki.

        Wysoki score = podejrzany kod:
          - Dużo odczytów, mało zapisów → szpieg
          - Wszystkie atomy HOT → infinite loop candidate
          - Dużo martwych atomów → memory leak
        """
        if not isinstance(self.global_scope, PhiScope):
            return 0.0

        atoms = list(self.global_scope._atoms.values())
        if not atoms:
            return 0.0

        total_reads  = sum(a._reads for a in atoms)
        total_writes = sum(a._writes for a in atoms)
        hot_ratio    = sum(1 for a in atoms if a.state == "HOT") / len(atoms)

        # Read/write imbalance
        rw_imbalance = 0.0
        if total_writes > 0:
            rw_imbalance = max(0.0, (total_reads / total_writes - 5.0) / 10.0)
        elif total_reads > 10:
            rw_imbalance = 1.0

        # All-hot anomaly (infinite loop?)
        hot_anomaly = max(0.0, hot_ratio - 0.8) * 5.0

        # GC pressure
        gc_pressure = min(1.0, self._total_gc / max(1, len(atoms) * 2))

        return min(1.0, rw_imbalance * 0.4 + hot_anomaly * 0.4 + gc_pressure * 0.2)

    def phi_stats(self) -> Dict[str, Any]:
        """Statystyki phi-space kontekstu JS."""
        atoms = list(self.global_scope._atoms.values()) if isinstance(
            self.global_scope, PhiScope) else []
        return {
            "context":   self._context,
            "atoms":     len(atoms),
            "hot":       sum(1 for a in atoms if a.state == "HOT"),
            "warm":      sum(1 for a in atoms if a.state == "WARM"),
            "cold":      sum(1 for a in atoms if a.state == "COLD"),
            "tomb":      sum(1 for a in atoms if a.state == "TOMB"),
            "tick_n":    self._tick_n,
            "total_gc":  self._total_gc,
            "op_count":  self._op_count,
            "max_ops":   self.MAX_OPS,
            "anomaly":   self.anomaly_score(),
            "reads":     sum(a._reads for a in atoms),
            "writes":    sum(a._writes for a in atoms),
        }

    # ── Sandbox ───────────────────────────────────────────────────────────────

    def sandbox(self, context: str = "sandbox") -> "KarmazynJSPhi":
        """
        Tworzy izolowany interpreter bez referencji do rodzica.
        Parent == None to próżnia — brak referencji do phi-space systemu.
        """
        return KarmazynJSPhi(runtime=None, context=context)

    # ── Runtime integration ───────────────────────────────────────────────────

    def expose_atom(self, js_name: str, atom_id: str) -> bool:
        """
        Eksponuje atom KarmazynOS jako zmienną JS.
        Live binding — przy każdym dostępie odczytuje atom.E z runtime.
        """
        if self._runtime is None:
            return False
        try:
            atom = self._runtime.get_atom(atom_id)
            if atom is None:
                return False
            self.global_scope.vars[js_name] = atom.E
            return True
        except Exception:
            return False

    def expose_bubble(self, js_name: str, bubble_label: str) -> bool:
        """
        Eksponuje bąbel KarmazynOS jako obiekt JS.
        Atomy bąbla stają się właściwościami obiektu.
        """
        if self._runtime is None:
            return False
        try:
            bubble = self._runtime.get_bubble(bubble_label)
            if bubble is None:
                return False
            obj = {}
            for atom in getattr(bubble, "atoms", []):
                obj[getattr(atom, "id", str(atom))] = getattr(atom, "E", None)
            self.global_scope.vars[js_name] = obj
            return True
        except Exception:
            return False

    # ── Override run ──────────────────────────────────────────────────────────

    def run(self, program: list) -> Any:
        """Uruchamia program w global scope z resetem op_count."""
        self._op_count = 0
        try:
            return self.exec(program, self.global_scope)
        except _Return as r:
            return r.value
        except _Throw as t:
            raise RuntimeError(f"Uncaught: {t.value}")


# ─── Komenda shella ───────────────────────────────────────────────────────────

def cmd_js(args, vm: KarmazynJSPhi) -> str:
    """
    JS STATUS         — statystyki phi-space kontekstu
    JS THERMAL        — mapa temperatur zmiennych
    JS TICK           — ręczny tick GC
    JS SANDBOX <name> — stwórz izolowany kontekst
    JS STATS          — szczegółowe statystyki
    """
    if not args or args[0].upper() == "STATUS":
        s = vm.phi_stats()
        lines = [
            f"Kontekst: {s['context']}",
            f"Atomy:    {s['atoms']}  HOT:{s['hot']}  COLD:{s['cold']}",
            f"Ticki:    {s['tick_n']}",
            f"Operacje: {s['op_count']}/{s['max_ops']}",
            f"Anomalia: {s['anomaly']:.2f}",
            f"Odczyty:  {s['reads']}  Zapisy: {s['writes']}",
        ]
        return "\n".join(lines)

    sub = args[0].upper()

    if sub == "THERMAL":
        tmap = vm.thermal_map()
        if not tmap:
            return "Brak atomów JS w phi-space."
        lines = ["Mapa temperatur JS:"]
        for name, info in sorted(tmap.items(), key=lambda x: -x[1]["T"]):
            lines.append(
                f"  [{info['state'][0]}] T={info['T']:5.1f}  "
                f"R:{info['reads']:3}  W:{info['writes']:3}  "
                f"{info['age']:5.1f}s  {name}"
            )
        return "\n".join(lines)

    if sub == "TICK":
        n = int(args[1]) if len(args) > 1 else 1
        total_collected = 0
        for _ in range(n):
            result = vm.tick()
            total_collected += result["collected"]
        return (f"Tick x{n}: zebrano {total_collected} atomów, "
                f"łącznie GC: {vm._total_gc}")

    if sub == "SANDBOX":
        name = args[1] if len(args) > 1 else "sandbox"
        sb = vm.sandbox(context=name)
        return f"Sandbox '{name}' utworzony (izolowany phi-space, 0 atomów)"

    if sub == "STATS":
        s = vm.phi_stats()
        lines = [
            f"=== KarmazynJS Phi Stats ===",
            f"Kontekst:  {s['context']}",
            f"Atomy:     {s['atoms']} (HOT:{s['hot']} WARM:{s['warm']} "
            f"COLD:{s['cold']} TOMB:{s['tomb']})",
            f"Ticki:     {s['tick_n']}",
            f"GC total:  {s['total_gc']}",
            f"Operacje:  {s['op_count']}/{s['max_ops']}",
            f"Anomalia:  {s['anomaly']:.3f}",
            f"R/W:       {s['reads']}/{s['writes']}",
        ]
        return "\n".join(lines)

    return f"Nieznana subkomenda JS: {args[0]}. Dostępne: STATUS THERMAL TICK SANDBOX STATS"
