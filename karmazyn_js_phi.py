"""
karmazyn_js.py — KarmazynJS Engine v0.2
========================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Silnik JS oparty na phi-space. Każda wartość to Atom,
każdy scope to Bubble, każde closure to referencja do Bubble.

Zmiany v0.2 względem MVP:
  - Ewaluator wyrażeń (expr) oddzielony od instrukcji (stmt)
    MVP miał tylko wartości statyczne — teraz można pisać a+b, call(fn, args)
  - touch() wywoływane przy każdym dostępie do atomu
  - decay() wywoływane przez scheduler (tick)
  - Function owinięty w Atom — spójny model
  - if / while / porównania
  - Sandbox: każdy kontekst dostaje izolowany phi-space
  - Detekcja anomalii termicznej (crypto miner / infinite loop)

Model instrukcji (program jako AST w Pythonie):

  Wyrażenia (expr — zwracają wartość):
    ("lit",  value)              — literał: 42, "hello", True
    ("var",  name)               — odczyt zmiennej
    ("op",   left, op, right)    — arytmetyka/porównanie: a + b, x == y
    ("call", fn_expr, arg_exprs) — wywołanie funkcji
    ("fn",   params, body)       — definicja funkcji (wyrażenie lambda)
    ("not",  expr)               — negacja logiczna

  Instrukcje (stmt — efekt uboczny):
    ("let",    name, expr)       — deklaracja zmiennej
    ("assign", name, expr)       — przypisanie do istniejącej
    ("return", expr)             — zwrot wartości
    ("expr",   expr)             — wyrażenie jako instrukcja (wywołanie)
    ("if",     cond, then, else_)— warunkowy
    ("while",  cond, body)       — pętla
    ("def",    name, params, body)— definicja funkcji (instrukcja)
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ─── Atom ────────────────────────────────────────────────────────────────────

class Atom:
    """
    Wartość JS jako atom phi-space.
    Temperatura = częstotliwość użycia.
    Zimny atom → kandydat do GC.
    """
    T_HOT   = 100.0
    T_DECAY = 0.95    # mnożnik przy każdym tick()
    T_TOMB  = 1.0     # próg GC

    def __init__(self, value: Any, name: str = ""):
        self.value  = value
        self.name   = name
        self.T      = 50.0     # temperatura startowa — WARM
        self.state  = "WARM"
        self._born  = time.time()

    def touch(self) -> None:
        """Dostęp do atomu go ogrzewa."""
        self.T = min(self.T_HOT, self.T + 10.0)
        self._update_state()

    def decay(self) -> None:
        """Tick schedulera — atom stygnie."""
        self.T *= self.T_DECAY
        self._update_state()

    def _update_state(self) -> None:
        if self.T >= 70:   self.state = "HOT"
        elif self.T >= 30: self.state = "WARM"
        elif self.T >= self.T_TOMB: self.state = "COLD"
        else:              self.state = "TOMB"

    def is_dead(self) -> bool:
        return self.T < self.T_TOMB

    def __repr__(self) -> str:
        return f"Atom({self.value!r}, T={self.T:.1f}, {self.state})"


# ─── Bubble (Scope) ───────────────────────────────────────────────────────────

class Bubble:
    """
    Scope JS jako bąbel phi-space.
    Hierarchia bąbli = scope chain.
    Lookup: bieżący bąbel → rodzic → ... → global → NameError.

    Ucieczka z bąbla prowadzi do pustego bąbla — nie do zewnętrznego runtime.
    Nie ma "na zewnątrz" — jest tylko próżnia phi-space.
    """

    def __init__(self, parent: Optional["Bubble"] = None,
                 name: str = "bubble"):
        self.name     = name
        self.parent   = parent
        self.vars:    Dict[str, Atom] = {}
        self.children: List["Bubble"] = []
        self._ticks   = 0        # licznik dostępów (anomalia termiczna)

        if parent is not None:
            parent.children.append(self)

    # ── Lookup ───────────────────────────────────────────────────────────────

    def get(self, key: str) -> Atom:
        """
        Szukaj atomu przez scope chain.
        Każdy dostęp ogrzewa atom — to jest JIT profiling za darmo.
        """
        if key in self.vars:
            atom = self.vars[key]
            atom.touch()
            return atom
        if self.parent is not None:
            return self.parent.get(key)
        # Ucieczka z bąbla — lądujemy w próżni
        raise NameError(f"'{key}' is not defined")

    def set_local(self, key: str, value: Any) -> None:
        """Utwórz atom w bieżącym bąblu (let / const)."""
        atom = Atom(value, name=key)
        self.vars[key] = atom

    def assign(self, key: str, value: Any) -> None:
        """
        Przypisz wartość do istniejącego atomu (=).
        Szuka przez scope chain — jak JS assignment.
        """
        if key in self.vars:
            self.vars[key].value = value
            self.vars[key].touch()
            return
        if self.parent is not None:
            self.parent.assign(key, value)
            return
        raise NameError(f"'{key}' is not defined (assign)")

    # ── Termodynamika ─────────────────────────────────────────────────────────

    def tick(self) -> List[str]:
        """
        Tick schedulera — wszystkie atomy stygną.
        Zwraca nazwy atomów które osiągnęły TOMB (kandydaci GC).
        """
        self._ticks += 1
        dead = []
        for key, atom in list(self.vars.items()):
            atom.decay()
            if atom.is_dead():
                dead.append(key)
        return dead

    def gc(self) -> int:
        """Usuń martwe atomy (TOMB). Zwraca liczbę usuniętych."""
        dead = [k for k, a in self.vars.items() if a.is_dead()]
        for k in dead:
            del self.vars[k]
        return len(dead)

    def thermal_anomaly(self, threshold: float = 200.0) -> bool:
        """
        Detekcja anomalii — bąbel który ma zbyt dużo ticków bez wyników
        to kandydat na crypto miner lub infinite loop.
        """
        return self._ticks > threshold and not self.vars

    def __repr__(self) -> str:
        keys = list(self.vars.keys())
        return f"Bubble({self.name!r}, vars={keys}, parent={self.parent.name if self.parent else None})"


# ─── Function (closure = referencja do bąbla) ────────────────────────────────

class JSFunction:
    """
    Funkcja JS. Closure = referencja do bąbla w którym została zdefiniowana.
    Bąbel nie umiera dopóki funkcja żyje — termodynamiczne weak reference.
    """

    def __init__(self, params: List[str], body: list, closure: Bubble,
                 name: str = "<anonymous>"):
        self.params  = params
        self.body    = body
        self.closure = closure   # ← to jest cała magia closures
        self.name    = name

    def call(self, runtime: "KarmazynJS", args: List[Any]) -> Any:
        # Nowy scope = child bąbla closure (nie global)
        local = Bubble(parent=self.closure,
                       name=f"fn_{self.name}_scope")
        # Binduj argumenty jako atomy lokalne
        for param, arg in zip(self.params, args):
            local.set_local(param, arg)
        # Uzupełnij brakujące parametry jako undefined
        for param in self.params[len(args):]:
            local.set_local(param, None)
        return runtime._exec_block(self.body, local)

    def __repr__(self) -> str:
        return f"JSFunction({self.name}, params={self.params})"


# ─── Return / Break wyjątki kontrolne ────────────────────────────────────────

class _Return(Exception):
    def __init__(self, value: Any):
        self.value = value

class _Break(Exception):
    pass

class _Continue(Exception):
    pass


# ─── Runtime ─────────────────────────────────────────────────────────────────

class KarmazynJS:
    """
    Silnik JS na bąblach.

    Każdy kontekst ma własny, izolowany phi-space.
    "Ucieczka" z bąbla trafia do pustej próżni — nie do zewnętrznego runtime.

    Sandbox jest strukturalny, nie polityczny:
      - Kod JS widzi tylko swój phi-space
      - Nie ma referencji do external runtime
      - Puste bąble poza granicą nie mają atomów do odczytu/modyfikacji
    """

    MAX_TICKS   = 10_000   # limit iteracji (ochrona przed infinite loop)

    def __init__(self, name: str = "global"):
        self.global_bubble = Bubble(name=name)
        self._tick_count   = 0
        self._setup_builtins()

    def _setup_builtins(self) -> None:
        """Wbudowane funkcje JS — tylko bezpieczne, bez dostępu do runtime."""
        builtins = {
            "console_log": JSFunction(
                ["x"], [("_builtin", "print")],
                self.global_bubble, "console.log"
            ),
            "Math_abs":    JSFunction(
                ["x"], [("_builtin", "abs")],
                self.global_bubble, "Math.abs"
            ),
            "String":      JSFunction(
                ["x"], [("_builtin", "str")],
                self.global_bubble, "String"
            ),
            "Number":      JSFunction(
                ["x"], [("_builtin", "num")],
                self.global_bubble, "Number"
            ),
        }
        for name, fn in builtins.items():
            self.global_bubble.set_local(name, fn)

    # ── Ewaluacja wyrażeń ─────────────────────────────────────────────────────

    def _eval(self, expr: Any, scope: Bubble) -> Any:
        """
        Ewaluuje wyrażenie i zwraca wartość Pythona.
        To jest kluczowa warstwa której brakowało w MVP.
        """
        # Nie-tuple = literał
        if not isinstance(expr, tuple):
            return expr

        op = expr[0]

        # Literał
        if op == "lit":
            return expr[1]

        # Odczyt zmiennej
        if op == "var":
            return scope.get(expr[1]).value

        # Operacje binarne: ("op", left, operator, right)
        if op == "op":
            _, left_expr, operator, right_expr = expr
            left  = self._eval(left_expr, scope)
            right = self._eval(right_expr, scope)
            return self._apply_op(left, operator, right)

        # Negacja
        if op == "not":
            return not self._eval(expr[1], scope)

        # Wywołanie: ("call", fn_expr, [arg_exprs...])
        if op == "call":
            _, fn_expr, arg_exprs = expr
            fn   = self._eval(fn_expr, scope)
            args = [self._eval(a, scope) for a in arg_exprs]
            return self._call_fn(fn, args, scope)

        # Definicja funkcji jako wyrażenie (lambda)
        if op == "fn":
            _, params, body = expr
            return JSFunction(params, body, scope)

        # Dostęp do właściwości obiektu (uproszczony)
        if op == "prop":
            _, obj_expr, prop = expr
            obj = self._eval(obj_expr, scope)
            if isinstance(obj, dict):
                return obj.get(prop)
            return getattr(obj, prop, None)

        # Array literal
        if op == "array":
            return [self._eval(e, scope) for e in expr[1]]

        # Object literal
        if op == "object":
            return {k: self._eval(v, scope) for k, v in expr[1].items()}

        raise SyntaxError(f"Nieznane wyrażenie: {op}")

    def _apply_op(self, left: Any, op: str, right: Any) -> Any:
        """Operatory JS."""
        ops = {
            "+":   lambda a, b: a + b,
            "-":   lambda a, b: a - b,
            "*":   lambda a, b: a * b,
            "/":   lambda a, b: a / b if b != 0 else float("inf"),
            "%":   lambda a, b: a % b,
            "**":  lambda a, b: a ** b,
            "==":  lambda a, b: a == b,
            "!=":  lambda a, b: a != b,
            "<":   lambda a, b: a < b,
            ">":   lambda a, b: a > b,
            "<=":  lambda a, b: a <= b,
            ">=":  lambda a, b: a >= b,
            "&&":  lambda a, b: a and b,
            "||":  lambda a, b: a or b,
        }
        if op not in ops:
            raise SyntaxError(f"Nieznany operator: {op}")
        return ops[op](left, right)

    def _call_fn(self, fn: Any, args: List[Any], scope: Bubble) -> Any:
        """Wywołuje funkcję — JSFunction lub wbudowaną."""
        if isinstance(fn, JSFunction):
            try:
                return fn.call(self, args)
            except _Return as r:
                return r.value
        if callable(fn):
            return fn(*args)
        raise TypeError(f"Nie jest funkcją: {fn!r}")

    # ── Wykonanie instrukcji ──────────────────────────────────────────────────

    def _exec_block(self, block: list, scope: Bubble) -> Any:
        """Wykonuje blok instrukcji."""
        result = None

        for stmt in block:
            self._tick_count += 1
            if self._tick_count > self.MAX_TICKS:
                raise RuntimeError("KarmazynJS: przekroczono limit ticków "
                                   "(infinite loop lub anomalia termiczna)")

            result = self._exec_stmt(stmt, scope)

        return result

    def _exec_stmt(self, stmt: tuple, scope: Bubble) -> Any:
        op = stmt[0]

        # Deklaracja zmiennej (let)
        if op == "let":
            _, name, expr = stmt
            value = self._eval(expr, scope)
            scope.set_local(name, value)
            return None

        # Przypisanie (=)
        if op == "assign":
            _, name, expr = stmt
            value = self._eval(expr, scope)
            scope.assign(name, value)
            return None

        # Definicja funkcji (statement)
        if op == "def":
            _, name, params, body = stmt
            fn = JSFunction(params, body, scope, name=name)
            scope.set_local(name, fn)
            return None

        # Return
        if op == "return":
            value = self._eval(stmt[1], scope)
            raise _Return(value)

        # Wyrażenie jako instrukcja (np. console.log, wywołanie funkcji)
        if op == "expr":
            return self._eval(stmt[1], scope)

        # Wbudowane (print, abs itp.)
        if op == "_builtin":
            # Wywoływane z wnętrza JSFunction — pobiera 'x' z local scope
            builtin = stmt[1]
            x = scope.get("x").value
            if builtin == "print":
                print(x)
            elif builtin == "abs":
                return abs(x)
            elif builtin == "str":
                return str(x)
            elif builtin == "num":
                return float(x) if x is not None else 0.0
            return None

        # Warunkowy (if)
        if op == "if":
            _, cond_expr, then_block, else_block = stmt
            cond = self._eval(cond_expr, scope)
            if cond:
                then_scope = Bubble(parent=scope, name="if_then")
                return self._exec_block(then_block, then_scope)
            elif else_block:
                else_scope = Bubble(parent=scope, name="if_else")
                return self._exec_block(else_block, else_scope)
            return None

        # Pętla while
        if op == "while":
            _, cond_expr, body = stmt
            iterations = 0
            while self._eval(cond_expr, scope):
                iterations += 1
                if iterations > 10_000:
                    raise RuntimeError("while: przekroczono 10000 iteracji")
                loop_scope = Bubble(parent=scope, name=f"while_{iterations}")
                try:
                    self._exec_block(body, loop_scope)
                except _Break:
                    break
                except _Continue:
                    continue
            return None

        # Break / Continue
        if op == "break":    raise _Break()
        if op == "continue": raise _Continue()

        raise SyntaxError(f"Nieznana instrukcja: {op}")

    # ── Publiczny interfejs ───────────────────────────────────────────────────

    def run(self, program: list) -> Any:
        """Uruchamia program w global scope."""
        self._tick_count = 0
        try:
            return self._exec_block(program, self.global_bubble)
        except _Return as r:
            return r.value

    def get(self, name: str) -> Any:
        """Odczyt wartości z global scope (dla testów/debugowania)."""
        return self.global_bubble.get(name).value

    def tick_gc(self) -> Dict[str, int]:
        """
        Tick schedulera — stygnięcie atomów i GC.
        Wywoływany przez KarmazynOS scheduler co N sekund.
        """
        def _tick_bubble(b: Bubble) -> Tuple[int, int]:
            dead  = b.tick()
            count = b.gc()
            total_dead  = count
            total_ticks = 1
            for child in b.children:
                cd, ct = _tick_bubble(child)
                total_dead  += cd
                total_ticks += ct
            return total_dead, total_ticks

        collected, bubbles = _tick_bubble(self.global_bubble)
        return {"collected": collected, "bubbles": bubbles}

    def sandbox(self, name: str = "untrusted") -> "KarmazynJS":
        """
        Tworzy izolowany kontekst JS.
        Nie ma referencji do self — pusty phi-space.
        Ucieczka prowadzi do próżni, nie do parent runtime.
        """
        return KarmazynJS(name=f"sandbox_{name}")


# ─── Demo ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    vm = KarmazynJS()

    # ── Test 1: arytmetyka i zmienne ─────────────────────────────────────────
    print("=== Test 1: Arytmetyka ===")
    vm.run([
        ("let", "x",   ("lit", 10)),
        ("let", "y",   ("lit", 20)),
        ("let", "sum", ("op", ("var", "x"), "+", ("var", "y"))),
        ("expr", ("call", ("var", "console_log"), [("var", "sum")])),
    ])
    assert vm.get("sum") == 30

    # ── Test 2: funkcja z closure ─────────────────────────────────────────────
    print("\n=== Test 2: Closure / Counter ===")
    vm.run([
        # function makeCounter() { let n = 0; return () => n++ }
        ("def", "makeCounter", [], [
            ("let", "n", ("lit", 0)),
            ("def", "increment", [], [
                ("let", "old", ("var", "n")),
                ("assign", "n", ("op", ("var", "n"), "+", ("lit", 1))),
                ("return", ("var", "old")),
            ]),
            ("return", ("var", "increment")),
        ]),

        ("let", "counter", ("call", ("var", "makeCounter"), [])),
        ("let", "a", ("call", ("var", "counter"), [])),   # 0
        ("let", "b", ("call", ("var", "counter"), [])),   # 1
        ("let", "c", ("call", ("var", "counter"), [])),   # 2
        ("expr", ("call", ("var", "console_log"), [("var", "a")])),
        ("expr", ("call", ("var", "console_log"), [("var", "b")])),
        ("expr", ("call", ("var", "console_log"), [("var", "c")])),
    ])
    assert vm.get("a") == 0
    assert vm.get("b") == 1
    assert vm.get("c") == 2

    # ── Test 3: rekurencja (fibonacci) ────────────────────────────────────────
    print("\n=== Test 3: Rekurencja (fib) ===")
    vm.run([
        ("def", "fib", ["n"], [
            ("if",
                ("op", ("var", "n"), "<=", ("lit", 1)),
                [("return", ("var", "n"))],
                [("return", ("op",
                    ("call", ("var", "fib"), [("op", ("var","n"),"-",("lit",1))]),
                    "+",
                    ("call", ("var", "fib"), [("op", ("var","n"),"-",("lit",2))])
                ))],
            ),
        ]),
        ("let", "fib10", ("call", ("var", "fib"), [("lit", 10)])),
        ("expr", ("call", ("var", "console_log"), [("var", "fib10")])),
    ])
    assert vm.get("fib10") == 55

    # ── Test 4: while loop ────────────────────────────────────────────────────
    print("\n=== Test 4: While loop ===")
    vm.run([
        ("let", "i",    ("lit", 0)),
        ("let", "acc",  ("lit", 0)),
        ("while",
            ("op", ("var", "i"), "<", ("lit", 5)),
            [
                ("assign", "acc", ("op", ("var", "acc"), "+", ("var", "i"))),
                ("assign", "i",   ("op", ("var", "i"),   "+", ("lit", 1))),
            ]
        ),
        ("expr", ("call", ("var", "console_log"), [("var", "acc")])),
    ])
    assert vm.get("acc") == 10   # 0+1+2+3+4

    # ── Test 5: sandbox (izolacja) ────────────────────────────────────────────
    print("\n=== Test 5: Sandbox isolation ===")
    untrusted = vm.sandbox("malicious_script")
    untrusted.run([
        ("let", "secret_attempt", ("lit", "trying to escape")),
    ])
    try:
        untrusted.global_bubble.parent.get("x")
        print("FAIL: ucieczka udana!")
    except (AttributeError, NameError):
        print("OK: sandbox izolowany — parent == None (próżnia phi-space)")

    # ── Test 6: termodynamika ─────────────────────────────────────────────────
    print("\n=== Test 6: Termodynamika ===")
    import time as _time
    vm2 = KarmazynJS()
    vm2.run([
        ("let", "hot_var", ("lit", "używana często")),
        ("let", "cold_var", ("lit", "zapomniana")),
    ])

    hot = vm2.global_bubble.vars["hot_var"]
    cold = vm2.global_bubble.vars["cold_var"]

    # Symuluj używanie hot_var
    for _ in range(20):
        vm2.global_bubble.get("hot_var")   # touch()

    # Symuluj 30 ticków schedulera
    for _ in range(30):
        vm2.tick_gc()

    print(f"hot_var:  T={hot.T:.1f}  state={hot.state}")
    print(f"cold_var: T={cold.T:.1f} state={cold.state}")
    assert hot.T > cold.T, "Używana zmienna powinna być cieplejsza"
    print("OK: termodynamika działa — cold_var ostygła szybciej")

    print("\n=== Wszystkie testy OK ===")
