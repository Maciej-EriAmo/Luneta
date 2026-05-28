"""
test_karmazyn_js.py — Testy KarmazynJS v1.0
=============================================
Testuje Core i Phi oddzielnie — każda warstwa niezależnie.
"""
import sys
sys.path.insert(0, '.')

from karmazyn_js_core import KarmazynJSCore
from karmazyn_js_phi  import KarmazynJSPhi, PhiAtom, PhiScope

results = {}


# ════════════════════════════════════════════════════════════════
# CORE TESTS — czysty interpreter, zero phi-space
# ════════════════════════════════════════════════════════════════

def test_core():
    vm = KarmazynJSCore()

    # ── Literały i arytmetyka ─────────────────────────────────
    vm.run([
        ("let", "a", ("lit", 10)),
        ("let", "b", ("lit", 20)),
        ("let", "c", ("op", ("var","a"), "+", ("var","b"))),
    ])
    results["core: a+b=30"]       = vm.get("c") == 30

    # ── Closure / Counter ─────────────────────────────────────
    vm2 = KarmazynJSCore()
    vm2.run([
        ("def", "makeCounter", [], [
            ("let", "n", ("lit", 0)),
            ("def", "inc", [], [
                ("let", "old", ("var", "n")),
                ("assign", "n", ("op", ("var","n"),"+",("lit",1))),
                ("return", ("var","old")),
            ]),
            ("return", ("var","inc")),
        ]),
        ("let", "c", ("call", ("var","makeCounter"), [])),
        ("let", "r0", ("call", ("var","c"), [])),
        ("let", "r1", ("call", ("var","c"), [])),
        ("let", "r2", ("call", ("var","c"), [])),
    ])
    results["core: closure r0=0"] = vm2.get("r0") == 0
    results["core: closure r1=1"] = vm2.get("r1") == 1
    results["core: closure r2=2"] = vm2.get("r2") == 2

    # ── Rekurencja — fibonacci ─────────────────────────────────
    vm3 = KarmazynJSCore()
    vm3.run([
        ("def", "fib", ["n"], [
            ("if",
                ("op", ("var","n"), "<=", ("lit",1)),
                [("return", ("var","n"))],
                [("return", ("op",
                    ("call", ("var","fib"), [("op",("var","n"),"-",("lit",1))]),
                    "+",
                    ("call", ("var","fib"), [("op",("var","n"),"-",("lit",2))]),
                ))],
            ),
        ]),
        ("let", "f10", ("call", ("var","fib"), [("lit",10)])),
    ])
    results["core: fib(10)=55"] = vm3.get("f10") == 55

    # ── While + break ──────────────────────────────────────────
    vm4 = KarmazynJSCore()
    vm4.run([
        ("let", "i",   ("lit", 0)),
        ("let", "sum", ("lit", 0)),
        ("while", ("op", ("var","i"), "<", ("lit",100)), [
            ("if",
                ("op", ("var","i"), "==", ("lit",5)),
                [("break",)],
                [],
            ),
            ("assign", "sum", ("op",("var","sum"),"+",("var","i"))),
            ("assign", "i",   ("op",("var","i"),  "+",("lit",1))),
        ]),
    ])
    results["core: while+break sum=0+1+2+3+4=10"] = vm4.get("sum") == 10

    # ── Array methods ─────────────────────────────────────────
    vm5 = KarmazynJSCore()
    vm5.run([
        ("let", "arr", ("array", [("lit",1),("lit",2),("lit",3),("lit",4),("lit",5)])),
        ("let", "doubled", ("call",
            ("prop", ("var","arr"), "map"),
            [("fn", ["x"], [("return", ("op",("var","x"),"*",("lit",2)))])]
        )),
        ("let", "evens", ("call",
            ("prop", ("var","arr"), "filter"),
            [("fn", ["x"], [("return", ("op",("op",("var","x"),"%",("lit",2)),"==",("lit",0)))])]
        )),
        ("let", "total", ("call",
            ("prop", ("var","arr"), "reduce"),
            [("fn", ["acc","x"], [("return",("op",("var","acc"),"+",("var","x")))]),
             ("lit", 0)]
        )),
    ])
    results["core: map [2,4,6,8,10]"] = vm5.get("doubled") == [2,4,6,8,10]
    results["core: filter [2,4]"]      = vm5.get("evens")   == [2,4]
    results["core: reduce sum=15"]     = vm5.get("total")   == 15

    # ── For-of ────────────────────────────────────────────────
    vm6 = KarmazynJSCore()
    vm6.run([
        ("let", "items", ("array", [("lit","a"),("lit","b"),("lit","c")])),
        ("let", "out",   ("array", [])),
        ("for_of", "item", ("var","items"), [
            ("expr", ("call",
                ("prop", ("var","out"), "push"),
                [("op", ("var","item"), "+", ("lit","!"))]
            )),
        ]),
    ])
    results["core: for_of"]  = vm6.get("out") == ["a!","b!","c!"]

    # ── Try/catch ─────────────────────────────────────────────
    vm7 = KarmazynJSCore()
    vm7.run([
        ("let", "caught", ("lit", False)),
        ("try",
            [("throw", ("lit", "oops"))],
            "err",
            [("assign", "caught", ("op", ("var","err"), "==", ("lit","oops")))],
            None,
        ),
    ])
    results["core: try/catch"] = vm7.get("caught") == True

    # ── Object literal + prop access ──────────────────────────
    vm8 = KarmazynJSCore()
    vm8.run([
        ("let", "person", ("obj", {
            "name": ("lit", "Maciej"),
            "age":  ("lit", 35),
        })),
        ("let", "name", ("prop", ("var","person"), "name")),
        ("expr", ("setprop", ("var","person"), "city", ("lit","Warsaw"))),
        ("let", "city", ("prop", ("var","person"), "city")),
    ])
    results["core: object.name"]   = vm8.get("name") == "Maciej"
    results["core: object.city"]   = vm8.get("city") == "Warsaw"

    # ── String methods ────────────────────────────────────────
    vm9 = KarmazynJSCore()
    vm9.run([
        ("let", "s",     ("lit", "  hello world  ")),
        ("let", "tr",    ("call", ("prop",("var","s"),"trim"),    [])),
        ("let", "up",    ("call", ("prop",("var","s"),"toUpperCase"), [])),
        ("let", "parts", ("call", ("prop",("var","tr"),"split"),  [("lit"," ")])),
        ("let", "len",   ("prop", ("var","tr"), "length")),
    ])
    results["core: string.trim"]    = vm9.get("tr")    == "hello world"
    results["core: string.upper"]   = vm9.get("up")    == "  HELLO WORLD  "
    results["core: string.split"]   = vm9.get("parts") == ["hello","world"]
    results["core: string.length"]  = vm9.get("len")   == 11

    # ── Ternary ───────────────────────────────────────────────
    vm10 = KarmazynJSCore()
    vm10.run([
        ("let", "x", ("lit", 10)),
        ("let", "label", ("ternary",
            ("op", ("var","x"), ">", ("lit",5)),
            ("lit","big"),
            ("lit","small"),
        )),
    ])
    results["core: ternary big"] = vm10.get("label") == "big"


# ════════════════════════════════════════════════════════════════
# PHI TESTS — termodynamika i sandbox, core nie dotykane
# ════════════════════════════════════════════════════════════════

def test_phi():

    # ── PhiAtom temperatura ───────────────────────────────────
    atom = PhiAtom(42, "test")
    initial_T = atom.T
    for _ in range(10):
        atom.touch_read()
    results["phi: touch_read ogrzewa"] = atom.T > initial_T

    cold = PhiAtom(0, "cold")
    for _ in range(50):
        cold.decay()
    results["phi: decay stygnie"]      = cold.T < initial_T
    results["phi: decay → TOMB"]       = cold.is_dead()

    # ── PhiScope scope chain z temperaturami ──────────────────
    parent = PhiScope(name="parent")
    child  = PhiScope(parent=parent, name="child")

    parent.set("x", 100)
    child.set("y", 200)

    results["phi: scope chain x"] = child.get("x") == 100
    results["phi: scope local y"] = child.get("y") == 200

    # Dostęp przez chain ogrzewa atom w parent
    T_before = parent._atoms["x"].T
    child.get("x")
    T_after  = parent._atoms["x"].T
    results["phi: cross-scope touch"] = T_after >= T_before

    # ── GC przez tick ─────────────────────────────────────────
    # Realistyczny scenariusz: hot_var dotykana co tick (aktywna),
    # cold_var nigdy nie używana po inicjalizacji (zapomniana).
    scope = PhiScope(name="gc_test")
    scope.set("hot_var",  "używana")
    scope.set("cold_var", "zapomniana")

    # hot_var jest dotykana CO tick — symulacja aktywnego użycia
    # cold_var nigdy nie dotykana — potrzeba ~39 ticków żeby spaść poniżej T_TOMB=2
    for _ in range(50):
        scope.get("hot_var")   # dostęp między tickami
        scope.tick()

    hot_alive  = "hot_var"  in scope._atoms
    cold_alive = "cold_var" in scope._atoms
    hot_T      = scope._atoms["hot_var"].T  if hot_alive  else 0.0

    results["phi: hot_var cieplejsza po tickach"] = (
        hot_alive and hot_T > 5.0
    )
    results["phi: cold_var zebrany przez GC"] = not cold_alive

    # ── Sandbox izolacja ──────────────────────────────────────
    vm_main = KarmazynJSPhi(name="main")
    vm_main.run([
        ("let", "secret", ("lit", "top_secret_data")),
    ])

    vm_sandbox = vm_main.sandbox("untrusted")

    # Sandbox nie może dostać się do secret z main
    try:
        vm_sandbox.run([
            ("let", "stolen", ("var", "secret")),
        ])
        results["phi: sandbox nie izoluje FAIL"] = False
    except (NameError, RuntimeError):
        results["phi: sandbox izolacja secret"] = True

    # Sandbox ma własne zmienne
    vm_sandbox.run([
        ("let", "local_var", ("lit", 42)),
    ])
    results["phi: sandbox ma własne zmienne"] = (
        vm_sandbox.get("local_var") == 42
    )

    # Parent nie widzi zmiennych sandbox
    try:
        vm_main.get("local_var")
        results["phi: parent izolowany od sandbox FAIL"] = False
    except NameError:
        results["phi: parent izolowany od sandbox"] = True

    # ── Phi stats ─────────────────────────────────────────────
    vm_stats = KarmazynJSPhi(name="stats_test")
    vm_stats.run([
        ("let", "a", ("lit", 1)),
        ("let", "b", ("lit", 2)),
        ("let", "c", ("op", ("var","a"), "+", ("var","b"))),
    ])
    s = vm_stats.phi_stats()
    results["phi: stats atoms >= 3"] = s["atoms"] >= 3
    results["phi: stats reads > 0"]  = s["reads"]  > 0

    # ── Thermal map ───────────────────────────────────────────
    tmap = vm_stats.thermal_map()
    results["phi: thermal_map niepusta"] = len(tmap) > 0
    results["phi: thermal_map posortowana"] = (
        len(tmap) < 2
        or tmap[0][1] >= tmap[-1][1]
    )

    # ── Tick zwraca wyniki ────────────────────────────────────
    tick_result = vm_stats.tick()
    results["phi: tick zwraca dict"] = "tick" in tick_result


# ════════════════════════════════════════════════════════════════
# RUN
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=== KarmazynJS Core ===")
    test_core()
    print("=== KarmazynJS Phi  ===")
    test_phi()

    print()
    all_ok = True
    for name, ok in results.items():
        icon = "OK  " if ok else "FAIL"
        print(f"  [{icon}] {name}")
        if not ok:
            all_ok = False

    total  = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n{passed}/{total} testów OK")
    print("=== WSZYSTKIE OK ===" if all_ok else "=== BŁĘDY ===")
    sys.exit(0 if all_ok else 1)