"""
js_pipeline_check.py — Diagnostyk potoku JS w Lunecie
======================================================
Sprawdza, gdzie JS pęka w drodze do wykonania:
  EXTRACT (z HTML) → PARSE (parse_js) → EXEC (KarmazynJSPhi.run)

Najważniejsze: wykrywa ZŁE UMOCOWANIE — sytuację, gdy parser parsuje
poprawnie, ale VM nie rozumie wyemitowanego AST (parser i VM mówią
innym dialektem węzłów). To objawia się: PARSE OK, EXEC błąd/zły wynik.
"""

import sys
import traceback

from karmazyn_js_parser import parse_js
from karmazyn_js_phi import KarmazynJSPhi
from karmazyn_js_web import extract_scripts


def _classify_exec_error(err: str, root) -> str:
    """
    Rozróżnia DWIE bardzo różne przyczyny exec-fail:
      'mismatch'    — VM nie zna węzła/operatora, który parser wyemitował
                      (prawdziwe złe umocowanie parser↔VM).
      'parser_drop' — parser po cichu zgubił konstrukcję i wyemitował
                      zdegenerowany AST (np. ('lit', None)); VM słusznie
                      wywala się na pustce. To luka PARSERA, nie VM.
    """
    low = (err or "").lower()
    if ("nieznany operator" in low or "nieznane wyrażenie" in low
            or "nieznany węzeł" in low or "unhandled" in low):
        return "mismatch"
    if root in (("lit", None), ("expr", ("lit", None)), None):
        return "parser_drop"
    return "mismatch"


def _run_one(src):
    """Zwraca (etap, wynik|błąd). etap ∈ {parse, exec, ok}."""
    try:
        ast = parse_js(src)
    except Exception as e:
        return "parse", f"{type(e).__name__}: {str(e)[:80]}", None
    try:
        vm = KarmazynJSPhi()
        result = vm.run(ast)
        return "ok", result, (ast[0] if ast else None)
    except Exception as e:
        return "exec", f"{type(e).__name__}: {str(e)[:80]}", (ast[0] if ast else None)


# Bateria realnego JS: (nazwa, kod, oczekiwany_wynik | None gdy tylko "ma się wykonać")
BATTERY = [
    ("literał liczbowy",        "42;", 42),
    ("arytmetyka",              "1 + 2 * 3;", 7),
    ("string",                  "'abc';", "abc"),
    ("var + odczyt",            "var x = 5; x;", 5),
    ("=== (ścisła równość)",    "1 === 1;", True),
    ("!== ",                    "1 !== 2;", True),
    ("ternary",                 "1 < 2 ? 'tak' : 'nie';", "tak"),
    ("if/else",                 "var a; if (1<2){a=10;}else{a=20;} a;", 10),
    ("funkcja klasyczna",       "function f(n){return n*2;} f(21);", 42),
    ("domknięcie/currying",     "function a(x){return function(y){return x+y;};} a(3)(4);", 7),
    ("arrow 1-arg",             "var inc = x => x + 1; inc(41);", 42),
    ("arrow wieloarg",          "var add = (a,b) => a+b; add(20,22);", 42),
    ("arrow z blokiem",         "var g = (n) => { return n*n; }; g(7);", 49),
    ("let/const",               "let a = 2; const b = 3; a+b;", 5),
    ("obiekt + pole",           "var o = {x:1, y:2}; o.y;", 2),
    ("tablica + indeks",        "var t = [10,20,30]; t[1];", 20),
    ("array.map",               "[1,2,3].map(function(x){return x*2;})[2];", 6),
    ("array.filter+length",     "[1,2,3,4].filter(function(x){return x>2;}).length;", 2),
    ("for loop",                "var s=0; for(var i=0;i<5;i=i+1){s=s+i;} s;", 10),
    ("while",                   "var n=0; while(n<3){n=n+1;} n;", 3),
    ("typeof",                  "typeof 5;", "number"),
    ("template literal",        "var n='świat'; `cześć ${n}`;", "cześć świat"),
    ("string.toUpperCase",      "'abc'.toUpperCase();", "ABC"),
    ("zagnieżdżony obiekt",     "var o={a:{b:7}}; o.a.b;", 7),
    ("destrukturyzacja",        "var [a,b] = [1,2]; a+b;", 3),
    ("default param",           "function f(x){if(x===undefined){x=9;} return x;} f();", 9),
]


def run():
    print("=" * 70)
    print("  DIAGNOSTYK POTOKU JS LUNETY  (extract → parse → exec)")
    print("=" * 70)

    # ── Etap 0: EXTRACT ─────────────────────────────────────────────────────
    print("\n[EXTRACT] Czy skrypty są wyjmowane z HTML?")
    html = ('<html><body><h1>Test</h1>'
            '<script>var a = 1;</script>'
            '<script type="module">var b = 2;</script>'
            '<script src="app.js"></script>'
            '<script type="text/plain">IGNORE</script>'
            '</body></html>')
    scripts = extract_scripts(html)
    inline = [c for t, c in scripts if t == "inline"]
    external = [c for t, c in scripts if t == "external"]
    print(f"  inline:   {len(inline)}  (oczekiwane 2: var a, var b)")
    print(f"  external: {len(external)} (oczekiwane 1: app.js)")
    print(f"  {'OK' if len(inline)==2 and len(external)==1 else 'UWAGA: ekstrakcja gubi/dubluje skrypty'}")

    # ── Etap 1+2: PARSE + EXEC bateria ──────────────────────────────────────
    print("\n[PARSE+EXEC] Bateria realnego JS:")
    parse_fail, exec_fail, wrong, ok = [], [], [], 0
    for name, src, expected in BATTERY:
        stage, res, root = _run_one(src)
        if stage == "parse":
            parse_fail.append((name, res))
            print(f"  PARSE✗  {name:24} {res}")
        elif stage == "exec":
            exec_fail.append((name, res, root))
            print(f"  EXEC✗   {name:24} [AST root={root!r}] {res}")
        else:
            if expected is None or res == expected:
                ok += 1
                print(f"  OK      {name:24} → {res!r}")
            else:
                wrong.append((name, res, expected))
                print(f"  WYNIK✗  {name:24} → {res!r} (oczekiwano {expected!r})")

    total = len(BATTERY)
    print("\n" + "=" * 70)
    print(f"  WYNIK: {ok}/{total} działa  |  "
          f"parse-fail: {len(parse_fail)}  exec-fail: {len(exec_fail)}  zły-wynik: {len(wrong)}")
    print("=" * 70)

    # ── Diagnoza umocowania ──────────────────────────────────────────────────
    mismatches = [(n, r, root) for (n, r, root) in exec_fail
                  if _classify_exec_error(r, root) == "mismatch"]
    parser_drops = [(n, r, root) for (n, r, root) in exec_fail
                    if _classify_exec_error(r, root) == "parser_drop"]

    if mismatches:
        print("\n⚠ ZŁE UMOCOWANIE — VM nie rozumie AST, który parser wyemitował:")
        for name, r, root in mismatches:
            print(f"    {name}: {r}  [AST {repr(root)[:50]}]")
        print("  → parser i KarmazynJSPhi rozjechały się; wyrównaj węzeł/operator w VM.")
    if parser_drops:
        print("\nℹ LUKA PARSERA — parser po cichu gubi konstrukcję (nie wina VM):")
        for name, r, root in parser_drops:
            print(f"    {name}: parser wyemitował {repr(root)[:40]} → VM dostał pustkę")
        print("  → dodaj obsługę w PARSERZE; VM jest tu niewinny.")
    if not mismatches and not parser_drops and parse_fail:
        print("\n→ Umocowanie OK (co parsuje, to się wykonuje). Braki są w PARSERZE.")
    if ok == total:
        print("\n→ Potok zdrowy: extract→parse→exec działa na całej baterii.")
    elif not mismatches:
        print("\n→ FUNDAMENT OK: zero rozjazdów parser↔VM. "
              f"Braki ({len(parser_drops)+len(parse_fail)}) są w parserze, nie w umocowaniu.")

    return ok, total, parse_fail, exec_fail, wrong


if __name__ == "__main__":
    run()