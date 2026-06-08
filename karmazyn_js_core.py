"""
karmazyn_js_core.py — KarmazynJS Core v1.0
===========================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Czysty interpreter JS. Zero zależności zewnętrznych.
Zero termodynamiki, zero GC, zero schedulera.
Jedna odpowiedzialność: wykonać program i zwrócić wynik.

Struktura:
  Scope    — environment chain (scope chain JS)
  Function — closure jako referencja do Scope
  Core     — eval(expr) + exec(stmt)

Instrukcje:
  ("let",    name, expr)          — deklaracja
  ("assign", name, expr)          — przypisanie
  ("def",    name, params, body)  — definicja funkcji
  ("return", expr)                — zwrot
  ("expr",   expr)                — wyrażenie jako instrukcja
  ("if",     cond, then, else_)   — warunkowy
  ("while",  cond, body)          — pętla
  ("break")                       — przerwanie pętli
  ("try",    body, catch_name, catch_body) — obsługa błędów

Wyrażenia:
  ("lit",    value)               — literał
  ("var",    name)                — odczyt zmiennej
  ("op",     left, op, right)     — operator binarny
  ("not",    expr)                — negacja
  ("call",   fn_expr, arg_exprs)  — wywołanie
  ("fn",     params, body)        — lambda
  ("prop",   obj_expr, key)       — dostęp do właściwości
  ("idx",    obj_expr, idx_expr)  — dostęp przez indeks
  ("setprop",obj_expr, key, val)  — przypisanie właściwości
  ("array",  [exprs])             — array literal
  ("obj",    {key: expr})         — object literal
  ("typeof", expr)                — typeof
  ("ternary",cond, then, else_)   — wyrażenie warunkowe
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ─── Wyjątki kontrolne ────────────────────────────────────────────────────────

class _Return(Exception):
    def __init__(self, value: Any): self.value = value

class _Break(Exception):    pass
class _Continue(Exception): pass
class _Throw(Exception):
    def __init__(self, value: Any): self.value = value


# ─── Scope ────────────────────────────────────────────────────────────────────

class Scope:
    """
    Environment chain — scope chain JS.
    Lookup idzie do rodzica jeśli nazwa nie istnieje lokalnie.
    """

    __slots__ = ("parent", "vars")

    def __init__(self, parent: Optional["Scope"] = None):
        self.parent = parent
        self.vars:  Dict[str, Any] = {}

    def get(self, name: str) -> Any:
        if name in self.vars:
            return self.vars[name]
        if self.parent is not None:
            return self.parent.get(name)
        raise NameError(f"'{name}' is not defined")

    def set(self, name: str, value: Any) -> None:
        """Utwórz zmienną w bieżącym scope (let/const/def)."""
        self.vars[name] = value

    def assign(self, name: str, value: Any) -> None:
        """Przypisz do istniejącej zmiennej przez scope chain (=)."""
        if name in self.vars:
            self.vars[name] = value
            return
        if self.parent is not None:
            self.parent.assign(name, value)
            return
        raise NameError(f"'{name}' is not defined")

    def child(self, name: str = "") -> "Scope":
        """Zwraca nowy scope zagnieżdżony w bieżącym."""
        return Scope(parent=self)


# ─── Function ─────────────────────────────────────────────────────────────────

@dataclass
class Function:
    """
    Funkcja JS. Closure = referencja do Scope w którym została zdefiniowana.
    Nie kopia zmiennych — referencja. Stąd closures działają przez mutację.
    """
    params:  List[str]
    body:    list
    closure: Scope
    name:    str = "<anonymous>"


# ─── Operatory ────────────────────────────────────────────────────────────────

_OPS = {
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
    "&":   lambda a, b: int(a) & int(b),
    "|":   lambda a, b: int(a) | int(b),
    "^":   lambda a, b: int(a) ^ int(b),
    "<<":  lambda a, b: int(a) << int(b),
    ">>":  lambda a, b: int(a) >> int(b),
}

_TYPEOF = {
    int:      "number",
    float:    "number",
    bool:     "boolean",
    str:      "string",
    type(None): "undefined",
    Function: "function",
    dict:     "object",
    list:     "object",
}


# ─── Core interpreter ─────────────────────────────────────────────────────────

class KarmazynJSCore:
    """
    Czysty interpreter JS. Jedna odpowiedzialność: eval + exec.
    Żadnych efektów ubocznych poza scope chain.
    Żadnych zależności od phi-space, runtime, schedulera.

    Używaj bezpośrednio do testów i jako bazę dla KarmazynJSPhi.
    """

    MAX_OPS = 100_000    # ochrona przed infinite loop na poziomie core

    def __init__(self):
        self.global_scope = Scope()
        self._op_count    = 0
        self._setup_builtins()

    def _setup_builtins(self) -> None:
        """Minimalne wbudowane — tylko to co potrzeba do działania."""
        g = self.global_scope
        g.set("undefined", None)
        g.set("null",      None)
        g.set("true",      True)
        g.set("false",     False)
        g.set("Infinity",  float("inf"))
        g.set("NaN",       float("nan"))

        # console
        g.set("print",    print)     # alias dla testów
        g.set("console",  {
            "log": lambda *a: print(*a),
        })

        # Math
        import math
        g.set("Math", {
            "abs":   abs,
            "floor": math.floor,
            "ceil":  math.ceil,
            "round": round,
            "sqrt":  math.sqrt,
            "pow":   pow,
            "min":   min,
            "max":   max,
            "PI":    math.pi,
            "E":     math.e,
        })

        # Array / String helpers
        g.set("Array",  {"isArray": lambda x: isinstance(x, list)})
        g.set("String", {"fromCharCode": lambda *a: "".join(chr(x) for x in a)})
        g.set("parseInt",   lambda s, base=10: int(str(s), int(base)))
        g.set("parseFloat", lambda s: float(s))
        g.set("isNaN",      lambda x: x != x)
        g.set("String",     str)
        g.set("Number",     lambda x: float(x) if x is not None else 0.0)
        g.set("Boolean",    bool)

    # ── Ewaluacja wyrażeń ─────────────────────────────────────────────────────

    def eval(self, expr: Any, scope: Scope) -> Any:
        """
        Ewaluuje wyrażenie i zwraca wartość.
        Nie-tuple = literał Pythona (int, str, bool, None...).
        """
        self._op_count += 1
        if self._op_count > self.MAX_OPS:
            raise RuntimeError("KarmazynJS: przekroczono limit operacji")

        if not isinstance(expr, tuple):
            return expr

        op = expr[0]

        if op == "lit":
            return expr[1]

        if op == "var":
            val = scope.get(expr[1])
            # Obsługa dostępu do metod obiektów
            return val

        if op == "op":
            _, left_e, operator, right_e = expr
            left  = self.eval(left_e,  scope)
            right = self.eval(right_e, scope)
            if operator not in _OPS:
                raise SyntaxError(f"Nieznany operator: {operator}")
            return _OPS[operator](left, right)

        if op == "not":
            return not self.eval(expr[1], scope)

        if op == "neg":
            return -self.eval(expr[1], scope)

        if op == "call":
            _, fn_e, arg_es = expr
            args = [self.eval(a, scope) for a in arg_es]

            # Zachowaj this dla wywołań obj.method()
            if isinstance(fn_e, tuple) and fn_e[0] == "prop":
                # ("prop", obj_expr, key) — obj.method(...)
                this_obj = self.eval(fn_e[1], scope)
                key      = fn_e[2]
                # FIX: jedno źródło prawdy — metody list/str są w _get_property,
                # nie jako atrybuty Pythona. getattr() zwracało None dla push/map/itd.
                fn = self._get_property(this_obj, key)
                return self._call(fn, args, this=this_obj)

            fn = self.eval(fn_e, scope)
            return self._call(fn, args)

        if op == "fn":
            _, params, body = expr
            return Function(params, body, scope)

        if op == "prop":
            # ("prop", obj_expr, key) — obj.key
            _, obj_e, key = expr
            obj = self.eval(obj_e, scope)
            return self._get_property(obj, key)

        if op == "idx":
            # ("idx", obj_expr, idx_expr) — obj[key]
            _, obj_e, idx_e = expr
            obj = self.eval(obj_e, scope)
            idx = self.eval(idx_e, scope)
            if isinstance(obj, (list, str)):
                if isinstance(idx, float): idx = int(idx)
                if 0 <= idx < len(obj): return obj[idx]
                return None
            if isinstance(obj, dict):
                return obj.get(idx)
            return None

        if op == "setprop":
            # ("setprop", obj_expr, key, val_expr) — obj.key = val
            _, obj_e, key, val_e = expr
            obj = self.eval(obj_e, scope)
            val = self.eval(val_e, scope)
            if isinstance(obj, dict):
                obj[key] = val
            elif hasattr(obj, key) or hasattr(type(obj), key):
                # LiveNode, obiekty zewnętrzne — setattr zamiast cichego pominięcia
                try:
                    setattr(obj, key, val)
                except AttributeError:
                    pass
            elif hasattr(obj, '__setitem__'):
                obj[key] = val
            return val

        if op == "setidx":
            # ("setidx", obj_expr, idx_expr, val_expr) — obj[i] = val
            _, obj_e, idx_e, val_e = expr
            obj = self.eval(obj_e, scope)
            idx = self.eval(idx_e, scope)
            val = self.eval(val_e, scope)
            if isinstance(obj, list):
                if isinstance(idx, float): idx = int(idx)
                while len(obj) <= idx: obj.append(None)
                obj[idx] = val
            elif isinstance(obj, dict):
                obj[idx] = val
            elif hasattr(obj, '__setitem__'):
                obj[idx] = val
            return val

        if op == "array":
            return [self.eval(e, scope) for e in expr[1]]

        if op == "obj":
            result = {}
            for k, v_e in expr[1].items():
                result[k] = self.eval(v_e, scope)
            return result

        if op == "typeof":
            val = self.eval(expr[1], scope)
            return _TYPEOF.get(type(val), "object")

        if op == "ternary":
            _, cond_e, then_e, else_e = expr
            return self.eval(then_e if self.eval(cond_e, scope) else else_e, scope)

        if op == "spread":
            # ("spread", expr) — zwraca listę do rozpakowywania w call
            return ("__spread__", self.eval(expr[1], scope))

        if op == "new":
            # ("new", fn_expr, arg_exprs) — uproszczony new
            _, fn_e, arg_es = expr
            fn   = self.eval(fn_e, scope)
            args = [self.eval(a, scope) for a in arg_es]
            obj  = {}
            if isinstance(fn, Function):
                local = Scope(fn.closure)
                local.set("this", obj)
                for p, a in zip(fn.params, args):
                    local.set(p, a)
                try:
                    self.exec(fn.body, local)
                except _Return as r:
                    if isinstance(r.value, dict):
                        return r.value
            return obj

        if op == "throw":
            raise _Throw(self.eval(expr[1], scope))

        if op == "await":
            # uproszczone await — synchroniczne wykonanie
            return self.eval(expr[1], scope)

        if op == "assign":
            # ("assign", target, val_expr)
            # Wyrażenia przypisania: x = y = 5, foo(a++)
            _, target, val_e = expr
            val = self.eval(val_e, scope)
            # Wykonaj przypisanie przez scope.assign (nie exec_stmt)
            if isinstance(target, str):
                scope.assign(target, val)
            elif isinstance(target, tuple) and target[0] == "setprop":
                obj = self.eval(target[1], scope)
                key = target[2]
                if isinstance(obj, dict): obj[key] = val
                elif hasattr(obj, key) or hasattr(type(obj), key):
                    try: setattr(obj, key, val)
                    except AttributeError: pass
            elif isinstance(target, tuple) and target[0] == "var":
                scope.assign(target[1], val)
            return val   # assign jako wyrażenie zwraca wartość

        if op == "seq":
            # ("seq", expr1, expr2, ...) — sekwencja wyrażeń, zwraca ostatnie
            result = None
            for sub in expr[1:]:
                result = self.eval(sub, scope)
            return result

        raise SyntaxError(f"Nieznane wyrażenie: {op!r}")

    def _reduce(self, fn: Any, arr: list, init: Any) -> Any:
        acc = init
        for i, x in enumerate(arr):
            if acc is None and i == 0:
                acc = x
                continue
            acc = self._call(fn, [acc, x, i, arr])
        return acc

    # ── Wywołanie funkcji ─────────────────────────────────────────────────────

    def _get_property(self, obj: Any, key: str) -> Any:
        """
        Rozwiązuje obj.key dla wszystkich typów. Jedno źródło prawdy
        używane przez handler 'prop' ORAZ ścieżkę wywołania metody obj.method().

        FIX: wcześniej obj.method() na liście/stringu robiło getattr(obj, key)
        — metody tablic/stringów nie są atrybutami Pythona, więc zwracało None
        ('None' is not a function). Teraz oba miejsca używają tej metody.
        """
        if isinstance(obj, dict):
            return obj.get(key)
        if isinstance(obj, list):
            if key == "length":    return len(obj)
            if key == "push":      return lambda *a: [obj.append(x) for x in a] or len(obj)
            if key == "pop":       return lambda: obj.pop() if obj else None
            if key == "shift":     return lambda: obj.pop(0) if obj else None
            if key == "unshift":   return lambda *a: ([obj.insert(0, x) for x in reversed(a)] or len(obj))
            if key == "join":      return lambda sep="," : sep.join(str(x) for x in obj)
            if key == "slice":     return lambda s=0,e=None: obj[s:e]
            if key == "indexOf":   return lambda x: obj.index(x) if x in obj else -1
            if key == "includes":  return lambda x: x in obj
            if key == "reverse":   return lambda: obj.reverse() or obj
            if key == "map":       return lambda fn: [self._call(fn, [x, i, obj]) for i, x in enumerate(obj)]
            if key == "filter":    return lambda fn: [x for i, x in enumerate(obj) if self._call(fn, [x, i, obj])]
            if key == "reduce":    return lambda fn, init=None: self._reduce(fn, obj, init)
            if key == "forEach":   return lambda fn: [self._call(fn, [x, i, obj]) for i, x in enumerate(obj)]
            if key == "find":      return lambda fn: next((x for i,x in enumerate(obj) if self._call(fn,[x,i,obj])), None)
            if key == "some":      return lambda fn: any(self._call(fn,[x,i,obj]) for i,x in enumerate(obj))
            if key == "every":     return lambda fn: all(self._call(fn,[x,i,obj]) for i,x in enumerate(obj))
            if key == "concat":    return lambda *a: obj + [x for arr in a for x in (arr if isinstance(arr,list) else [arr])]
            if key == "flat":      return lambda: [x for sub in obj for x in (sub if isinstance(sub,list) else [sub])]
            if key == "sort":      return lambda fn=None: sorted(obj, key=lambda x: self._call(fn,[x,x]) if fn else x) or obj
            return None
        if isinstance(obj, str):
            if key == "length":    return len(obj)
            if key == "split":     return lambda sep="": obj.split(sep) if sep else list(obj)
            if key == "trim":      return lambda: obj.strip()
            if key == "toUpperCase": return lambda: obj.upper()
            if key == "toLowerCase": return lambda: obj.lower()
            if key == "includes":  return lambda s: s in obj
            if key == "indexOf":   return lambda s: obj.find(s)
            if key == "slice":     return lambda s=0, e=None: obj[s:e]
            if key == "replace":   return lambda pat, rep: obj.replace(pat, rep)
            if key == "startsWith": return lambda s: obj.startswith(s)
            if key == "endsWith":  return lambda s: obj.endswith(s)
            if key == "charCodeAt": return lambda i: ord(obj[i]) if 0 <= i < len(obj) else float("nan")
            if key == "substring": return lambda s, e=None: obj[s:e]
            if key == "padStart":  return lambda n, c=" ": obj.rjust(n, c)
            if key == "repeat":    return lambda n: obj * n
            return None
        if callable(obj):
            # Funkcja/lambda — property na funkcji (rzadkie); zwróć ją samą
            return obj
        # Ogólny obiekt Python (np. LiveNode, LiveDOM) — getattr
        try:
            return getattr(obj, key)
        except AttributeError:
            return None

    def _call(self, fn: Any, args: List[Any],
               this: Any = None) -> Any:
        """Wywołuje Function, callable lub metodę obiektu.
        this — kontekst obiektu dla wywołań obj.method() (Bug 4 fix).
        """
        # Rozpakowywanie spread
        expanded = []
        for a in args:
            if isinstance(a, tuple) and len(a) == 2 and a[0] == "__spread__":
                expanded.extend(a[1] if isinstance(a[1], list) else [a[1]])
            else:
                expanded.append(a)
        args = expanded

        if isinstance(fn, Function):
            local = Scope(fn.closure)
            for p, a in zip(fn.params, args):
                local.set(p, a)
            for p in fn.params[len(args):]:
                local.set(p, None)
            # Wstrzyknij this do scope jeśli podany
            if this is not None:
                local.set("this", this)
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

    # ── Wykonanie instrukcji ──────────────────────────────────────────────────

    def exec(self, block: list, scope: Scope) -> Any:
        """Wykonuje blok instrukcji. Zwraca wartość ostatniej."""
        result = None

        for stmt in block:
            self._op_count += 1
            if self._op_count > self.MAX_OPS:
                raise RuntimeError("KarmazynJS: limit operacji przekroczony")

            op = stmt[0]

            if op == "let":
                _, name, expr = stmt
                scope.set(name, self.eval(expr, scope))

            elif op == "assign":
                _, name, expr = stmt
                scope.assign(name, self.eval(expr, scope))

            elif op == "def":
                _, name, params, body = stmt
                scope.set(name, Function(params, body, scope, name=name))

            elif op == "return":
                raise _Return(self.eval(stmt[1], scope))

            elif op == "expr":
                inner = stmt[1]
                # Parser może wygenerować ("expr", ("assign", ...))
                # assign nie jest wyrażeniem — obsłuż jako instrukcję
                if (isinstance(inner, tuple) and len(inner) >= 3
                        and inner[0] == "assign"):
                    scope.assign(inner[1], self.eval(inner[2], scope))
                elif (isinstance(inner, tuple) and len(inner) >= 4
                        and inner[0] == "setprop"):
                    obj = self.eval(inner[1], scope)
                    val = self.eval(inner[3], scope)
                    if isinstance(obj, dict):
                        obj[inner[2]] = val
                    elif hasattr(obj, inner[2]) or hasattr(type(obj), inner[2]):
                        try:    setattr(obj, inner[2], val)
                        except AttributeError: pass
                    elif hasattr(obj, '__setitem__'):
                        obj[inner[2]] = val
                elif (isinstance(inner, tuple) and len(inner) >= 4
                        and inner[0] == "setidx"):
                    obj = self.eval(inner[1], scope)
                    idx = self.eval(inner[2], scope)
                    val = self.eval(inner[3], scope)
                    if isinstance(obj, list):
                        if isinstance(idx, float): idx = int(idx)
                        while len(obj) <= idx: obj.append(None)
                        obj[idx] = val
                    elif isinstance(obj, dict):
                        obj[idx] = val
                    elif hasattr(obj, '__setitem__'):
                        obj[idx] = val
                else:
                    result = self.eval(inner, scope)

            elif op == "if":
                _, cond_e, then_b, else_b = stmt
                cond = self.eval(cond_e, scope)
                if cond:
                    result = self.exec(then_b, scope.child())
                elif else_b:
                    result = self.exec(else_b, scope.child())

            elif op == "while":
                _, cond_e, body = stmt
                while self.eval(cond_e, scope):
                    try:
                        self.exec(body, scope.child())
                    except _Break:
                        break
                    except _Continue:
                        continue

            elif op == "for":
                # ("for", init_stmt, cond_expr, update_stmt, body)
                _, init_s, cond_e, update_s, body = stmt
                for_scope = scope.child()
                if init_s: self.exec([init_s], for_scope)
                while self.eval(cond_e, for_scope):
                    try:
                        self.exec(body, for_scope.child())
                    except _Break:
                        break
                    except _Continue:
                        pass
                    if update_s: self.exec([update_s], for_scope)

            elif op == "for_of":
                # ("for_of", var_name, iter_expr, body)
                _, var_name, iter_e, body = stmt
                items = self.eval(iter_e, scope)
                if isinstance(items, str): items = list(items)
                for item in (items or []):
                    loop_scope = scope.child()
                    loop_scope.set(var_name, item)
                    try:
                        self.exec(body, loop_scope)
                    except _Break:
                        break
                    except _Continue:
                        continue

            elif op == "for_in":
                # ("for_in", var_name, obj_expr, body)
                _, var_name, obj_e, body = stmt
                obj = self.eval(obj_e, scope)
                keys = list(obj.keys()) if isinstance(obj, dict) else []
                for key in keys:
                    loop_scope = scope.child()
                    loop_scope.set(var_name, key)
                    try:
                        self.exec(body, loop_scope)
                    except _Break:
                        break
                    except _Continue:
                        continue

            elif op == "break":
                raise _Break()

            elif op == "continue":
                raise _Continue()

            elif op == "throw":
                raise _Throw(self.eval(stmt[1], scope))

            elif op == "try":
                # ("try", body, catch_name, catch_body, finally_body)
                _, try_body, catch_name, catch_body, *rest = stmt
                finally_body = rest[0] if rest else None
                try:
                    result = self.exec(try_body, scope.child())
                except _Throw as e:
                    if catch_body:
                        catch_scope = scope.child()
                        if catch_name:
                            catch_scope.set(catch_name, e.value)
                        result = self.exec(catch_body, catch_scope)
                except Exception as e:
                    if catch_body:
                        catch_scope = scope.child()
                        if catch_name:
                            catch_scope.set(catch_name, str(e))
                        result = self.exec(catch_body, catch_scope)
                finally:
                    if finally_body:
                        self.exec(finally_body, scope.child())

            elif op == "block":
                result = self.exec(stmt[1], scope.child())

            else:
                raise SyntaxError(f"Nieznana instrukcja: {op!r}")

        return result

    # ── Publiczny interfejs ───────────────────────────────────────────────────

    def run(self, program: list) -> Any:
        """Uruchamia program w global scope."""
        self._op_count = 0
        try:
            return self.exec(program, self.global_scope)
        except _Return as r:
            return r.value
        except _Throw as t:
            raise RuntimeError(f"Uncaught: {t.value}")

    def get(self, name: str) -> Any:
        """Odczyt z global scope (testy/debug)."""
        return self.global_scope.get(name)

    def set_global(self, name: str, value: Any) -> None:
        """Wstrzyknij wartość do global scope."""
        self.global_scope.set(name, value)