#!/usr/bin/env python3
"""
karmazyn_substrate.py — kanoniczny rdzeń KarmazynOS (krok 1 konsolidacji)
==========================================================================
Maciej Mazur, Warsaw 2026

Jeden substrat zamiast pięciu. Neutralny językowo, zbudowany na NAJBOGATSZYM
istniejącym atomie — `karmazyn_atom.Atom` (pełny FSM HOT/WARM/COLD/TOMB, wektor).

Dlaczego ten kształt, a nie AtomRegistry:
  bezpiecznego GC atomów NIE DA SIĘ zrobić bez grafu osiągalności (scope'y,
  korzenie, domknięcia). `AtomRegistry` posiada same atomy → kasuje po samej
  temperaturze (udowodniona katastrofa P6: zimne-ale-żywe domknięcie ginie po
  39 tickach). Ten Store POSIADA atomy + bąble + korzenie razem → liczy
  osiągalność → reguła reach: zimny+nieosiągalny → GC; zimny+osiągalny →
  archiwum. Temperatura mówi KIEDY, osiągalność mówi CZY.

Co rdzeń wie o językach: nic. Reachability domknięć idzie przez wstrzykiwany
callback `env_of(value) -> Bubble | None`. Front-end (Scheme/JS/LOGO) dostarcza
go; rdzeń tylko śledzi zwrócony env. To jest cała wiedza rdzenia o "domknięciu".

Powierzchnia zgodna z evaluatorem referencji (drop-in):
  atom_new(S,E,T,value) · get_atom · heat · bubble_new(label,parent) ·
  bind/lookup · set_root/unset_root · tick · settle · stats
  wartość: atom.metadata["v"]   (jak w referencji)
"""

import karmazyn_atom as ka

# Twarz wektora (HRR) — opcjonalna. Degradacja łagodna bez numpy/hrr:
# atom_vector zwraca None, resonance zwraca []. Wykonanie/GC niezależne od tego.
try:
    import karmazyn_hrr as _hrr
    _HAS_HRR = True
except Exception:
    _hrr = None
    _HAS_HRR = False

VEC_DIM = 2048   # zamrożone D

# Stałe termiczne spójne z karmazyn_atom (is_dead == T < T_TOMB)
T_INIT = 50.0
T_MAX = 100.0
T_TOMB = ka.T_TOMB if hasattr(ka, "T_TOMB") else 2.0
DECAY = 0.92
HEAT_READ = 10.0


class EventBus:
    """Kanoniczny bus zdarzeń silnika: on/emit. Atomy ogłaszają stan, słuchacze
    reagują — nikt nie odpytuje (odpytanie zimnego atomu by go ogrzało).
    Dublety EventBus w runtime/phi idą potem do scalenia (herezja)."""
    __slots__ = ("_handlers",)

    def __init__(self):
        self._handlers = {}

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for h in list(self._handlers.get(event, ())):
            try:
                h(*args)
            except Exception:
                pass


class Bubble:
    """Scope/grupa: rodzic + nazwane wiązania (§4.4 schemat A). lookup grzeje."""
    __slots__ = ("store", "label", "parent", "bindings")

    def __init__(self, store, label="", parent=None):
        self.store = store
        self.label = label
        self.parent = parent
        self.bindings = {}              # name -> atom id

    def bind(self, name, atom):
        self.bindings[name] = atom.id

    def lookup(self, name):
        b = self
        while b is not None:
            aid = b.bindings.get(name)
            if aid is not None:
                atom = self.store.get_atom(aid)
                if atom is not None and self.store.thermal:
                    self.store.heat(atom)     # twarz termiczna: odczyt grzeje
                return atom                   # wartość NIEZALEŻNA od termiki
            b = b.parent
        return None


class Store:
    """Posiadający magazyn: atomy (karmazyn_atom.Atom) + bąble + korzenie + reach-GC."""

    def __init__(self, thermal=True, env_of=None, decay=DECAY, extra_reach=None):
        self.reg = ka.AtomRegistry()         # kanoniczne przechowywanie + FSM atomu
        self.bubbles = []
        self.roots = []
        self.thermal = thermal
        self._env_of = env_of or (lambda v: None)   # rdzeń nie zna języka
        # extra_reach: dostawca dodatkowych osiągalnych id atomów dla front-endów
        # o płaskim członkostwie (np. Luneta: atomy trzymane przez bąble-kontenery).
        # Scheme go nie używa (osiągalność idzie przez korzenie leksykalne + env_of).
        self._extra_reach = extra_reach or (lambda: ())
        self._decay = decay
        self._n = 0
        self.reaped = 0
        self._archived = set()               # liczone raz na atom (nie co tick)
        self.events = EventBus()             # silnik = źródło zdarzeń termicznych

    # ── atomy (§4.1) ──────────────────────────────────────────────────────────
    def atom_new(self, S, E="", T=T_INIT, value=None):
        aid = f"a{self._n}"
        self._n += 1
        atom = self.reg.create(aid, S=S, E=str(E), T=T)
        atom.metadata["v"] = value           # payload językowy (jak w referencji)
        atom.on_state_change(self._announce) # atom sam ogłosi przejście stanu
        self.events.emit("atom_created", atom)
        return atom

    def _announce(self, atom):
        # Atom przekroczył próg stanu — emituj na bus. Scheduler słucha,
        # nie odpytuje (odpytanie zimnego atomu by go ogrzało).
        if atom.state == "TOMB":
            self.events.emit("vacuum_decay", atom)
        else:
            self.events.emit("state_changed", atom)

    def get_atom(self, aid):
        return self.reg.get(aid)

    def heat(self, atom):
        atom.heat(HEAT_READ)                 # FSM: podnosi T, ożywia z COLD/TOMB

    # ── scope (§4.4) ──────────────────────────────────────────────────────────
    def bubble_new(self, label="", parent=None):
        b = Bubble(self, label, parent)
        self.bubbles.append(b)
        return b

    def set_root(self, b):
        self.roots.append(b)

    def unset_root(self, b):
        try:
            self.roots.remove(b)
        except ValueError:
            pass

    # ── osiągalność: korzenie + parent chain + env domknięć (przez env_of) ─────
    def _reachable(self):
        reach = set()
        seen = set()
        stack = list(self.roots)
        while stack:
            b = stack.pop()
            if id(b) in seen:
                continue
            seen.add(id(b))
            for name, aid in b.bindings.items():
                reach.add(aid)
                a = self.get_atom(aid)
                if a is not None:
                    env = self._env_of(a.metadata.get("v"))
                    if env is not None:
                        stack.append(env)        # env przechwyconego domknięcia
            if b.parent is not None:
                stack.append(b.parent)           # łańcuch leksykalny
        reach.update(self._extra_reach())            # płaskie członkostwo (np. bąble Lunety)
        return reach

    # ── upływ czasu: stygnięcie + reguła reach. No-op poza thermal. ───────────
    def tick(self):
        if not self.thermal:
            return
        reach = self._reachable()
        for atom in self.reg.atoms():
            atom.decay(self._decay)          # hak atomu → state_changed/vacuum_decay
            self.events.emit("tick", atom)   # źródło dla progów THRESHOLD
        for atom in list(self.reg.atoms()):
            if atom.is_dead():                   # zimny (T < T_TOMB)
                if atom.id in reach:
                    self._archived.add(atom.id)  # osiągalny → zostaje (archiwum)
                else:
                    self.reg.delete(atom.id)     # nieosiągalny → GC
                    self._archived.discard(atom.id)
                    self.reaped += 1

    def settle(self, n):
        for _ in range(n):
            self.tick()

    # ── twarz wektora (HRR) — leniwa, odtwarzana z nazwy ───────────────────────
    def atom_vector(self, atom):
        """Trzecia twarz: wektor HRR z kanonicznej nazwy (E). Liczony NA ŻĄDANIE
        i cache'owany w slocie atomu — zgodnie z 'wektory nie są przechowywane,
        odtwarzane z nazwy'. Tylko dla nazwanych (E niepuste). None bez HRR.

        Hot path wykonania go nie woła — koszt płaci dopiero rezonans."""
        if not _HAS_HRR or not atom.E:
            return None
        if not atom.has_vector:
            atom.vector = _hrr.name_to_vector(atom.E, VEC_DIM)
        return atom.vector

    def resonance(self, query, k=5, threshold=0.1):
        """Adresowanie przez falę, nie przez id: zwraca k nazwanych atomów
        najbliższych zapytaniu (nazwa lub wektor) w sensie podobieństwa HRR.
        Lista [(sim, atom_id), ...] malejąco. [] bez HRR."""
        if not _HAS_HRR:
            return []
        qv = query if hasattr(query, "shape") else _hrr.name_to_vector(query, VEC_DIM)
        hits = []
        for atom in self.reg.atoms():
            if not atom.E:
                continue
            v = self.atom_vector(atom)
            if v is None:
                continue
            s = _hrr.similarity(qv, v)
            if s >= threshold:
                hits.append((s, atom.id))
        hits.sort(key=lambda x: -x[0])
        return hits[:k]

    def stats(self):
        live = self.reg.atoms()
        total = len(live)
        cold = sum(1 for a in live if a.is_dead())   # is_dead to metoda (T < T_TOMB)
        return {
            "total": total,
            "hot": total - cold,
            "cold": cold,
            "reaped": self.reaped,
            "archived": len(self._archived),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Samowalidacja: uruchom front-end Scheme NA TYM rdzeniu (drop-in)
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import karmazyn_scheme as ks

    # callback: rdzeń pyta front-end "czy ta wartość trzyma env?" — Scheme: domknięcie
    def env_of_scheme(v):
        return v.env if isinstance(v, ks.Closure) else None

    def run_on_core(src, thermal):
        store = Store(thermal=thermal, env_of=env_of_scheme)
        g = store.bubble_new("global", parent=None)
        store.set_root(g)
        for name, fn in ks.make_primitives().items():
            g.bind(name, store.atom_new("prim", name, value=fn))
        ks._CUR_STORE = store
        r = None
        for form in ks.parse_all(src):
            r = ks.seval(form, g, store)
            store.tick()
        return ks.swrite(r), store

    # orakl (jeśli jest) — inaczej wartości oczekiwane z referencji
    try:
        import calysto_scheme.scheme as oracle
        def onorm(s):
            s = str(s).strip()
            return {"True": "#t", "False": "#f"}.get(s, s)
        OV = {tid: onorm(oracle.execute_string_top(c, "t")) for tid, c in ks.BATTERY}
    except Exception:
        OV = None

    print("=" * 64)
    print("  karmazyn_substrate — Scheme na kanonicznym rdzeniu (karmazyn_atom)")
    print("=" * 64)

    for mode_name, thermal in [("static", False), ("dynamic", True)]:
        ok = 0
        for tid, code in ks.BATTERY:
            got, _ = run_on_core(code, thermal)
            got = str(got).strip()
            got = {"True": "#t", "False": "#f"}.get(got, got)
            exp = OV[tid] if OV else None
            if exp is None or got == exp:
                if OV is None:
                    ok += 1            # bez orakla: liczymy wykonane bez błędu
                elif got == exp:
                    ok += 1
            else:
                print(f"    XX {tid}: got={got} exp={exp}")
        tag = "vs orakl" if OV else "wykonane"
        print(f"  [{mode_name:7}] bateria {tag}: {ok}/{len(ks.BATTERY)}")

    print("-" * 64)
    # przeciwnik: zimne domknięcie (60 form bezczynności, potem wywołanie)
    adv = "(define (mk n) (lambda () n)) (define g (mk 42)) "
    adv += " ".join(f"(define z{i} {i})" for i in range(60))
    adv += " (g)"
    r, _ = run_on_core(adv, True)
    print(f"  zimne domknięcie (oczek. 42): {r} -> {'OK' if r == '42' else 'ZŁAMANE'}")

    # GC: rekurencja tworzy martwe ramki, settle sprząta
    r2, st2 = run_on_core("(define (fact n)(if (= n 0) 1 (* n (fact (- n 1))))) (fact 6)", True)
    st2.settle(80)
    s = st2.stats()
    print(f"  GC: fact 6={r2} reaped={s['reaped']} archived={s['archived']} "
          f"-> {'OK (martwe zebrane, żywe nie)' if r2 == '720' and s['reaped'] > 0 else 'PROBLEM'}")
    print("=" * 64)
