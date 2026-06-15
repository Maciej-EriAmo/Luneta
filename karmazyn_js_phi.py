"""
karmazyn_js_phi.py — KarmazynJS Phi Layer v1.1
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

Zmiany v1.1:
  - FIX: override _call() → PhiScope zamiast Scope (Bug 1)
  - FIX: _prune_dead_children() w tick — czyszczenie martwych scope'ów (Bug 5)
  - NEW: PhiScope.scope_name, .depth — metadane śledzenia
  - NEW: scope_tree() — pełne drzewo scope'ów dla debugowania
  - NEW: cmd_js TREE — komenda shella
  - NEW: phi_stats() zawiera scope_count i scope_depth
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
    Scope z termodynamiką i śledzeniem dzieci.
    Każda zmienna to PhiAtom — dostęp ogrzewa, brak dostępu stygnie.

    Śledzenie drzewa scope'ów:
      scope_name  — nazwa czytelna (fn_counter, for_body, if_then)
      depth       — głębokość zagnieżdżenia (global=0)
      children    — lista żywych dzieci (przycinana w tick)
      _born       — czas utworzenia (monotonic)

    Rozszerza Scope z Core — Core nie wie o temperaturach.
    """

    def __init__(self, parent: Optional["PhiScope"] = None,
                 scope_name: str = ""):
        super().__init__(parent)
        self._atoms: Dict[str, PhiAtom] = {}
        self.children: List["PhiScope"] = []
        self.scope_name = scope_name
        self.depth      = (parent.depth + 1) if isinstance(parent, PhiScope) else 0
        self._born      = time.monotonic()
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
        return PhiScope(parent=self, scope_name=name)

    def detach_child(self, child_scope: "PhiScope") -> None:
        """Odłącza dziecko z listy children."""
        if child_scope in self.children:
            self.children.remove(child_scope)

    # ── Termodynamika ─────────────────────────────────────────────────────────

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

    def is_empty(self) -> bool:
        """Scope bez atomów i bez dzieci — kandydat do przycinania."""
        return len(self._atoms) == 0 and len(self.children) == 0

    def age(self) -> float:
        """Wiek scope'a w sekundach."""
        return time.monotonic() - self._born

    # ── Inspekcja ─────────────────────────────────────────────────────────────

    def atom_count(self) -> int:
        return len(self._atoms)

    def total_atom_count(self) -> int:
        """Atomy w tym scope + wszystkich potomkach."""
        total = len(self._atoms)
        for c in self.children:
            if isinstance(c, PhiScope):
                total += c.total_atom_count()
        return total

    def total_scope_count(self) -> int:
        """Liczba scope'ów w poddrzewie (włącznie z self)."""
        total = 1
        for c in self.children:
            if isinstance(c, PhiScope):
                total += c.total_scope_count()
        return total

    def max_depth(self) -> int:
        """Najgłębsze zagnieżdżenie w poddrzewie."""
        if not self.children:
            return self.depth
        return max(
            (c.max_depth() for c in self.children if isinstance(c, PhiScope)),
            default=self.depth
        )

    def hot_atoms(self) -> List[PhiAtom]:
        return [a for a in self._atoms.values() if a.state == "HOT"]

    def cold_atoms(self) -> List[PhiAtom]:
        return [a for a in self._atoms.values() if a.state == "COLD"]

    def thermal_map(self) -> Dict[str, Dict[str, Any]]:
        """Mapa temperatur zmiennych w tym scope + potomkach."""
        result = {}
        self._collect_thermal(result, "")
        return result

    def _collect_thermal(self, out: dict, prefix: str) -> None:
        for name, atom in self._atoms.items():
            key = f"{prefix}{name}" if prefix else name
            out[key] = {
                "T":      round(atom.T, 1),
                "state":  atom.state,
                "reads":  atom._reads,
                "writes": atom._writes,
                "age":    round(atom.age(), 1),
                "scope":  self.scope_name or "(global)",
                "depth":  self.depth,
            }
        for c in self.children:
            if isinstance(c, PhiScope):
                child_prefix = f"{prefix}{c.scope_name}." if c.scope_name else prefix
                c._collect_thermal(out, child_prefix)

    def scope_tree(self, indent: int = 0) -> str:
        """Tekstowa reprezentacja drzewa scope'ów."""
        pad   = "  " * indent
        label = self.scope_name or "(global)"
        atoms = len(self._atoms)
        kids  = len(self.children)
        line  = f"{pad}[d{self.depth}] {label}  atoms={atoms} children={kids}"
        if self._atoms:
            names = ", ".join(sorted(self._atoms.keys())[:5])
            if len(self._atoms) > 5:
                names += f", ...+{len(self._atoms)-5}"
            line += f"  ({names})"
        lines = [line]
        for c in self.children:
            if isinstance(c, PhiScope):
                lines.append(c.scope_tree(indent + 1))
        return "\n".join(lines)


# ─── KarmazynJSPhi ───────────────────────────────────────────────────────────

class KarmazynJSPhi(KarmazynJSCore):
    """
    Interpreter JS z phi-space.
    Dziedziczy Core i zastępuje jedną rzecz: Scope → PhiScope.

    Core nie wie że istnieje PhiScope.
    PhiScope nie wie jak interpretować JS.
    Separation of concerns jest formalny.

    v1.1: override _call() — scope funkcji jako PhiScope, nie Scope.
    v1.1: _prune_dead_children() — czyszczenie pustych scope'ów w tick.
    """

    def __init__(self, runtime=None, context: str = "global",
                 name: str = None):
        super().__init__()
        # Zamień global_scope na PhiScope
        old_vars = self.global_scope.vars.copy()
        self.global_scope = PhiScope(scope_name=name or context)
        # Przenieś builtiny do nowego scope (jako zwykłe vars, nie atoms)
        self.global_scope.vars.update(old_vars)

        self._runtime = runtime
        self._context = name or context
        self._tick_n  = 0
        self._total_gc = 0
        self._pruned_scopes = 0

    # ── FIX Bug 1: override _call() — PhiScope zamiast Scope ─────────────────

    def _call(self, fn: Any, args: List[Any],
              this: Any = None) -> Any:
        """
        Override _call z Core — tworzy PhiScope zamiast Scope.
        Dzięki temu scope'y funkcji są widoczne w drzewie phi-space,
        ich zmienne podlegają termodynamice i GC.
        """
        # Rozpakowywanie spread (skopiowane z Core)
        expanded = []
        for a in args:
            if isinstance(a, tuple) and len(a) == 2 and a[0] == "__spread__":
                expanded.extend(a[1] if isinstance(a[1], list) else [a[1]])
            else:
                expanded.append(a)
        args = expanded

        if isinstance(fn, Function):
            # PhiScope zamiast Scope — to jest cały fix
            closure = fn.closure
            if isinstance(closure, PhiScope):
                local = PhiScope(parent=closure,
                                 scope_name=f"fn_{fn.name}")
            else:
                local = PhiScope(scope_name=f"fn_{fn.name}")
                local.parent = closure
            if this is not None:
                local.vars["this"] = this  # this jako raw var, nie atom
            self._bind_params(local, fn.params, args)   # współdzielone z Core — koniec duplikacji
            try:
                return self.exec(fn.body, local)
            except _Return as r:
                return r.value

        if callable(fn):
            try:
                return fn(*args)
            except Exception as e:
                raise _Throw(str(e))

        raise TypeError(f"'{fn!r}' is not a function")

    # ── Tick — stygnięcie + GC + pruning ──────────────────────────────────────

    def tick(self) -> Dict[str, Any]:
        """
        Jeden tick termodynamiczny: decay + GC + prune w całym drzewie scope.
        Wywoływany przez scheduler lub ręcznie (JS TICK).
        """
        self._tick_n += 1
        collected = self._tick_scope(self.global_scope)
        pruned    = self._prune_dead_children(self.global_scope)
        self._total_gc      += collected
        self._pruned_scopes += pruned
        return {
            "tick":      self._tick_n,
            "collected": collected,
            "pruned":    pruned,
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

    def _prune_dead_children(self, scope) -> int:
        """
        FIX Bug 5: Usuwa puste scope'y-dzieci (brak atomów + brak potomków).
        Rekurencyjne: najpierw prune wnuki, potem dzieci.
        Scope z atomami lub żywymi potomkami przeżywa.
        """
        if not isinstance(scope, PhiScope):
            return 0
        pruned = 0
        # Najpierw prune wnuków (bottom-up)
        for child in list(scope.children):
            pruned += self._prune_dead_children(child)
        # Potem prune dzieci tego scope'a
        alive = []
        for child in scope.children:
            if isinstance(child, PhiScope) and child.is_empty():
                pruned += 1  # puste — wycinamy
            else:
                alive.append(child)
        scope.children = alive
        return pruned

    # ── Profiling ─────────────────────────────────────────────────────────────

    def thermal_map(self) -> Dict[str, Dict[str, Any]]:
        """Pełna mapa temperatur — global scope + potomkowie."""
        if isinstance(self.global_scope, PhiScope):
            return self.global_scope.thermal_map()
        return {}

    def scope_tree(self) -> str:
        """Drzewo scope'ów jako tekst."""
        if isinstance(self.global_scope, PhiScope):
            return self.global_scope.scope_tree()
        return "(no phi-space)"

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

        rw_imbalance = 0.0
        if total_writes > 0:
            rw_imbalance = max(0.0, (total_reads / total_writes - 5.0) / 10.0)
        elif total_reads > 10:
            rw_imbalance = 1.0

        hot_anomaly = max(0.0, hot_ratio - 0.8) * 5.0
        gc_pressure = min(1.0, self._total_gc / max(1, len(atoms) * 2))

        return min(1.0, rw_imbalance * 0.4 + hot_anomaly * 0.4 + gc_pressure * 0.2)

    def phi_stats(self) -> Dict[str, Any]:
        """Statystyki phi-space kontekstu JS."""
        gs = self.global_scope
        if isinstance(gs, PhiScope):
            atoms = list(gs._atoms.values())
            scope_count = gs.total_scope_count()
            scope_depth = gs.max_depth()
            total_atoms = gs.total_atom_count()
        else:
            atoms = []
            scope_count = 1
            scope_depth = 0
            total_atoms = 0
        return {
            "context":       self._context,
            "atoms":         len(atoms),
            "total_atoms":   total_atoms,
            "hot":           sum(1 for a in atoms if a.state == "HOT"),
            "warm":          sum(1 for a in atoms if a.state == "WARM"),
            "cold":          sum(1 for a in atoms if a.state == "COLD"),
            "tomb":          sum(1 for a in atoms if a.state == "TOMB"),
            "tick_n":        self._tick_n,
            "total_gc":      self._total_gc,
            "pruned_scopes": self._pruned_scopes,
            "scope_count":   scope_count,
            "scope_depth":   scope_depth,
            "op_count":      self._op_count,
            "max_ops":       self.MAX_OPS,
            "anomaly":       self.anomaly_score(),
            "reads":         sum(a._reads for a in atoms),
            "writes":        sum(a._writes for a in atoms),
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
    JS THERMAL        — mapa temperatur zmiennych (+ potomkowie)
    JS TICK [n]       — ręczny tick GC
    JS TREE           — drzewo scope'ów
    JS SANDBOX <name> — stwórz izolowany kontekst
    JS STATS          — szczegółowe statystyki
    """
    if not args or args[0].upper() == "STATUS":
        s = vm.phi_stats()
        lines = [
            f"Kontekst: {s['context']}",
            f"Atomy:    {s['atoms']} (global) / {s['total_atoms']} (drzewo)  "
            f"HOT:{s['hot']}  COLD:{s['cold']}",
            f"Scope'y:  {s['scope_count']}  depth={s['scope_depth']}  "
            f"pruned={s['pruned_scopes']}",
            f"Ticki:    {s['tick_n']}  GC:{s['total_gc']}",
            f"Operacje: {s['op_count']}/{s['max_ops']}",
            f"Anomalia: {s['anomaly']:.2f}",
            f"R/W:      {s['reads']}/{s['writes']}",
        ]
        return "\n".join(lines)

    sub = args[0].upper()

    if sub == "THERMAL":
        tmap = vm.thermal_map()
        if not tmap:
            return "Brak atomów JS w phi-space."
        lines = ["Mapa temperatur JS:"]
        for name, info in sorted(tmap.items(), key=lambda x: -x[1]["T"]):
            scope_tag = f"@{info['scope']}" if info.get("scope") else ""
            lines.append(
                f"  [{info['state'][0]}] T={info['T']:5.1f}  "
                f"R:{info['reads']:3}  W:{info['writes']:3}  "
                f"d{info.get('depth',0)}  {name} {scope_tag}"
            )
        return "\n".join(lines)

    if sub == "TICK":
        n = int(args[1]) if len(args) > 1 else 1
        total_collected = 0
        total_pruned    = 0
        for _ in range(n):
            result = vm.tick()
            total_collected += result["collected"]
            total_pruned    += result["pruned"]
        return (f"Tick x{n}: zebrano {total_collected} atomów, "
                f"przycięto {total_pruned} scope'ów, "
                f"łącznie GC: {vm._total_gc}")

    if sub == "TREE":
        tree = vm.scope_tree()
        return f"Drzewo scope'ów:\n{tree}"

    if sub == "SANDBOX":
        name = args[1] if len(args) > 1 else "sandbox"
        sb = vm.sandbox(context=name)
        return f"Sandbox '{name}' utworzony (izolowany phi-space, 0 atomów)"

    if sub == "STATS":
        s = vm.phi_stats()
        lines = [
            f"=== KarmazynJS Phi Stats ===",
            f"Kontekst:      {s['context']}",
            f"Atomy global:  {s['atoms']} (HOT:{s['hot']} WARM:{s['warm']} "
            f"COLD:{s['cold']} TOMB:{s['tomb']})",
            f"Atomy drzewo:  {s['total_atoms']}",
            f"Scope'y:       {s['scope_count']}  max depth={s['scope_depth']}",
            f"Pruned scopes: {s['pruned_scopes']}",
            f"Ticki:         {s['tick_n']}",
            f"GC total:      {s['total_gc']}",
            f"Operacje:      {s['op_count']}/{s['max_ops']}",
            f"Anomalia:      {s['anomaly']:.3f}",
            f"R/W:           {s['reads']}/{s['writes']}",
        ]
        return "\n".join(lines)

    return f"Nieznana subkomenda JS: {args[0]}. Dostępne: STATUS THERMAL TICK TREE SANDBOX STATS"