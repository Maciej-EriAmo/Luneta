"""
karmazyn_async.py — Atom-based Async Engine KarmazynOS v1.0
============================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Zamiennik modelu współbieżności V8/JS oparty na termodynamice atomów.
To czego V8 osiąga przez inżynierię (event loop, GC generacyjny,
izolacja workerów) tutaj wynika z fizyki phi-space.

Trzy warstwy:

  ThermalLoop  — zamiennik event loop.
                 Brak kolejki FIFO. Temperatura JEST priorytetem.
                 Gorący task = pilny. Zimny = czeka. Martwy = GC.
                 Microtask = born hot (T_MICRO). Macrotask = T_MACRO.
                 Timer = stygnący lont (decay → fire). I/O = zimne ziarno
                 ogrzewane przez hosta po zakończeniu operacji.

  AsyncAtom    — zamiennik Promise.
                 PENDING = COLD (czeka). RESOLVED = HOT (host ogrzał).
                 REJECTED = TOMB (host zabił). Kontynuacje (.then) są
                 microtaskami — phi-space widzi gorący atom w następnym ticku.

  PhiWorker    — zamiennik Web Worker.
                 Izolowany sandbox KarmazynJSPhi (parent=None → próżnia).
                 Komunikacja przez skrzynki (mailbox) z kopiowaniem ładunku.
                 Brak współdzielonej pamięci — bo brak współdzielonej geometrii.
                 Dopasowanie kanału = rezonans przy tau=1.0; hak _resonates()
                 pozwala podmienić na pełny Ring-LWE φ-space.

Mapowanie czasu:
  TICK_MS — ile milisekund "reprezentuje" jeden tick (dla setTimeout).
  Domyślnie 16ms (~60 FPS), spójne z modelem renderowania przeglądarki.

Integracja z JS:
  bind_event_loop(vm, loop)  — wstrzykuje setTimeout, setInterval,
                               clearTimeout, queueMicrotask do global scope.
  bind_async(vm, loop)       — wstrzykuje konstruktor Promise-like.
"""

import copy
import time
from typing import Any, Callable, Dict, List, Optional, Tuple


# ─── Progi termodynamiczne schedulera ─────────────────────────────────────────

T_MAX     = 100.0    # maksymalna temperatura
T_MICRO   = 100.0    # microtask — born hot (najwyższy priorytet)
T_MACRO   = 75.0     # macrotask — born warm-hot
T_RUN     = 70.0     # próg wykonania (heat-triggered ready)
T_FIRE    = 2.0      # próg odpalenia lontu (timer fire)
T_COLD    = 10.0     # I/O pending — zimne ziarno
TICK_MS   = 16.0     # ms na tick (~60 FPS)

MAX_DRAIN = 100_000  # ochrona przed nieskończoną pętlą microtasków na tick


# ─── Stany AsyncAtom ──────────────────────────────────────────────────────────

ASYNC_PENDING  = "PENDING"
ASYNC_RESOLVED = "RESOLVED"
ASYNC_REJECTED = "REJECTED"


# ─── Task — jednostka pracy schedulera ────────────────────────────────────────

class Task:
    """
    Jednostka pracy w ThermalLoop.

    Każdy task to atom z temperaturą:
      microtask/macrotask — born hot (T >= T_RUN), wykonuje się hottest-first
      timer               — stygnący lont, fire gdy T <= fire_T
      io                  — zimne ziarno, host ogrzewa przy resolve

    repeating=True (setInterval) — task nie jest usuwany po wykonaniu,
    tylko resetowany do stanu wyjściowego.
    """

    __slots__ = (
        "id", "fn", "args", "kind",
        "T", "decay_rate", "fire_T",
        "repeating", "period_ticks", "_age_ticks",
        "cancelled", "_born",
    )

    def __init__(self,
                 id:           int,
                 fn:           Callable,
                 args:         tuple = (),
                 kind:         str   = "macrotask",
                 T:            float = T_MACRO,
                 decay_rate:   float = 0.92,
                 fire_T:       float = T_FIRE,
                 repeating:    bool  = False,
                 period_ticks: int   = 0):
        self.id           = id
        self.fn           = fn
        self.args         = args
        self.kind         = kind
        self.T            = float(T)
        self.decay_rate   = decay_rate
        self.fire_T       = fire_T
        self.repeating    = repeating
        self.period_ticks = period_ticks
        self._age_ticks   = 0
        self.cancelled    = False
        self._born        = time.monotonic()

    def is_ready(self) -> bool:
        """Heat-triggered task gotowy do wykonania (microtask/macrotask/io)."""
        return self.T >= T_RUN

    def cool(self) -> None:
        """Stygnięcie lontu (timer)."""
        self.T *= self.decay_rate
        self._age_ticks += 1

    def is_fired(self) -> bool:
        """Lont (timer) dopalił się — czas odpalić callback."""
        return self.T <= self.fire_T

    def reset_timer(self) -> None:
        """Reset lontu dla setInterval — znów grzejemy do T_MAX."""
        self.T          = T_MAX
        self._age_ticks = 0

    def __repr__(self) -> str:
        return (f"Task(#{self.id}, {self.kind}, T={self.T:.1f}, "
                f"{'rep' if self.repeating else 'once'})")


# ─── ThermalLoop — zamiennik event loop ───────────────────────────────────────

class ThermalLoop:
    """
    Pętla termodynamiczna. Zamiennik event loop V8.

    Nie ma kolejki priorytetów — temperatura JEST priorytetem.
    Każdy tick:
      1. Dopalanie lontów (timery stygną; dopalone → macrotask).
      2. Dostarczenie rozwiązanych AsyncAtom (kontynuacje → microtask).
      3. Drenaż gotowych tasków hottest-first (micro przed macro przed timer).

    Microtask (T=100) zawsze biją macrotask (T=75) — bo są cieplejsze.
    Nie polityka kolejkowania — fizyka temperatury.
    """

    def __init__(self):
        self._ready:     List[Task] = []          # micro + macro (heat-triggered)
        self._timers:    List[Task] = []          # stygnące lonty
        self._async:     Dict[int, "AsyncAtom"] = {}  # pending promisy
        self._next_id    = 1
        self._tick_n     = 0
        self._ran_total  = 0
        self._fired_total = 0
        self._error_handler: Optional[Callable] = None
        self._vm = None      # opcjonalny KarmazynJSPhi do wywołań JS Function

    def set_vm(self, vm) -> None:
        """Podłącza VM żeby loop umiał wywołać JS Function (a nie tylko Python)."""
        self._vm = vm

    def _invoke(self, fn: Callable, *args) -> Any:
        """
        Wywołuje callback. Rozróżnia JS Function (przez vm._call)
        od zwykłego Python callable. Most między dwoma światami.
        """
        try:
            from karmazyn_js_core import Function
            if isinstance(fn, Function):
                if self._vm is None:
                    raise RuntimeError(
                        "ThermalLoop: JS Function bez VM "
                        "(użyj install_async_runtime lub loop.set_vm)")
                return self._vm._call(fn, list(args))
        except ImportError:
            pass
        return fn(*args)

    # ── Rejestracja tasków ────────────────────────────────────────────────────

    def micro(self, fn: Callable, *args) -> int:
        """queueMicrotask — born hot, najwyższy priorytet."""
        tid = self._next_id; self._next_id += 1
        self._ready.append(Task(tid, fn, args, kind="microtask", T=T_MICRO))
        return tid

    def macro(self, fn: Callable, *args) -> int:
        """Zwykły task — born warm-hot."""
        tid = self._next_id; self._next_id += 1
        self._ready.append(Task(tid, fn, args, kind="macrotask", T=T_MACRO))
        return tid

    def set_timeout(self, fn: Callable, delay_ticks: int, *args) -> int:
        """
        setTimeout — lont termodynamiczny.
        Start T_MAX, stygnie z taką szybkością by dopalić się po delay_ticks.

        decay_rate = (fire_T / T_MAX) ^ (1/delay_ticks)
        Po dokładnie delay_ticks: T_MAX * rate^delay_ticks = fire_T → fire.

        delay_ticks <= 0: odpala się natychmiast (następny tick) jako macrotask.
        """
        tid = self._next_id; self._next_id += 1
        if delay_ticks <= 0:
            # Natychmiastowy — macrotask zamiast lontu
            self._ready.append(Task(tid, fn, args, kind="macrotask", T=T_MACRO))
            return tid
        rate = (T_FIRE / T_MAX) ** (1.0 / delay_ticks)
        self._timers.append(Task(
            tid, fn, args, kind="timer",
            T=T_MAX, decay_rate=rate, fire_T=T_FIRE,
            period_ticks=delay_ticks,
        ))
        return tid

    def set_interval(self, fn: Callable, period_ticks: int, *args) -> int:
        """
        setInterval — powtarzalny lont.
        Po dopaleniu odpala callback i resetuje się do T_MAX.
        """
        tid = self._next_id; self._next_id += 1
        period = max(1, period_ticks)
        rate = (T_FIRE / T_MAX) ** (1.0 / period)
        self._timers.append(Task(
            tid, fn, args, kind="timer",
            T=T_MAX, decay_rate=rate, fire_T=T_FIRE,
            repeating=True, period_ticks=period,
        ))
        return tid

    def clear_timeout(self, tid: int) -> bool:
        """clearTimeout / clearInterval — anuluje task po id."""
        for t in self._timers:
            if t.id == tid:
                t.cancelled = True
                return True
        for t in self._ready:
            if t.id == tid:
                t.cancelled = True
                return True
        return False

    def clear_interval(self, tid: int) -> bool:
        """Alias clear_timeout — symetria z set_interval."""
        return self.clear_timeout(tid)

    # ── AsyncAtom ───────────────────────────────────────────────────────────────

    def create_async(self) -> "AsyncAtom":
        """Tworzy AsyncAtom w stanie PENDING (zimne ziarno)."""
        aid = self._next_id; self._next_id += 1
        aa = AsyncAtom(self, aid)
        self._async[aid] = aa
        return aa

    # ── Obsługa błędów ──────────────────────────────────────────────────────────

    def on_error(self, handler: Callable) -> None:
        """Rejestruje globalny handler błędów (uncaught w taskach)."""
        self._error_handler = handler

    # ── Pętla ─────────────────────────────────────────────────────────────────

    def tick(self) -> Dict[str, int]:
        """
        Jeden obrót pętli termodynamicznej.

        Zwraca statystyki: tick, ran (wykonanych tasków), fired (lontów),
        pending (oczekujących), timers, async_pending.
        """
        self._tick_n += 1

        fired = self._ripen_timers()      # 1. lonty stygną, dopalone → macrotask
        self._deliver_async()             # 2. resolved AsyncAtom → microtasks
        ran   = self._drain_ready()       # 3. drenaż hottest-first

        return {
            "tick":          self._tick_n,
            "ran":           ran,
            "fired":         fired,
            "pending":       len(self._ready),
            "timers":        len(self._timers),
            "async_pending": sum(1 for a in self._async.values()
                                 if a.state == ASYNC_PENDING),
        }

    def run(self, max_ticks: int = 10_000) -> Dict[str, int]:
        """
        Uruchamia pętlę aż do wyczerpania pracy lub max_ticks.
        Praca = ready tasks LUB timery LUB nierozwiązane async z kontynuacjami.

        Zwraca podsumowanie.
        """
        for _ in range(max_ticks):
            if not self._has_work():
                break
            self.tick()
        return {
            "ticks":     self._tick_n,
            "ran_total": self._ran_total,
            "fired_total": self._fired_total,
        }

    def _has_work(self) -> bool:
        """Czy pętla ma jeszcze co robić?"""
        if any(not t.cancelled for t in self._ready):
            return True
        if any(not t.cancelled for t in self._timers):
            return True
        # Settled async z nie-dostarczonymi jeszcze kontynuacjami
        for a in self._async.values():
            if a.state == ASYNC_RESOLVED and a._on_resolve:
                return True
            if a.state == ASYNC_REJECTED and a._on_reject:
                return True
            if (a.state == ASYNC_REJECTED and not a._unhandled_reported
                    and not a._on_resolve and self._error_handler is not None):
                return True
        return False

    def _has_oneshot_work(self) -> bool:
        """
        Czy pętla ma pracę JEDNORAZOWĄ (bez powtarzalnych interwałów)?
        Używane przez settle() — auto-settle nie goni za setInterval
        (który nigdy się nie kończy i zawieszałby ładowanie strony).
        """
        if any(not t.cancelled for t in self._ready):
            return True
        # Tylko jednorazowe lonty (setTimeout), nie interwały (setInterval)
        if any(not t.cancelled and not t.repeating for t in self._timers):
            return True
        for a in self._async.values():
            if a.state == ASYNC_RESOLVED and a._on_resolve:
                return True
            if a.state == ASYNC_REJECTED and a._on_reject:
                return True
            if (a.state == ASYNC_REJECTED and not a._unhandled_reported
                    and not a._on_resolve and self._error_handler is not None):
                return True
        return False

    def settle(self, max_ticks: int = 64) -> Dict[str, int]:
        """
        Drenaż pracy jednorazowej (microtaski, promisy, setTimeout) do
        bezczynności lub max_ticks. NIE goni za setInterval — interwały
        zostają dla jawnego pump()/run() lub schedulera.

        To jest właściwy tryb po załadowaniu strony: ustaw stan początkowy,
        nie blokuj na wiecznych interwałach.
        """
        ticks = 0
        for _ in range(max_ticks):
            if not self._has_oneshot_work():
                break
            self.tick()
            ticks += 1
        return {
            "ticks":       ticks,
            "ran_total":   self._ran_total,
            "fired_total": self._fired_total,
            "intervals":   sum(1 for t in self._timers
                               if t.repeating and not t.cancelled),
        }

    # ── Fazy ticku ──────────────────────────────────────────────────────────────

    def _ripen_timers(self) -> int:
        """
        Stygnięcie lontów. Dopalone (T <= fire_T) → callback jako macrotask.
        Powtarzalne (interval) resetują się; jednorazowe znikają.
        Zwraca liczbę dopalonych w tym ticku.
        """
        fired = 0
        survivors: List[Task] = []
        for t in self._timers:
            if t.cancelled:
                continue
            t.cool()
            if t.is_fired():
                fired += 1
                self._fired_total += 1
                # Dopalony lont → callback wjeżdża jako macrotask (born hot)
                tid = self._next_id; self._next_id += 1
                self._ready.append(Task(
                    tid, t.fn, t.args, kind="macrotask", T=T_MACRO
                ))
                if t.repeating:
                    t.reset_timer()
                    survivors.append(t)
                # jednorazowy — nie wraca do survivors
            else:
                survivors.append(t)
        self._timers = survivors
        return fired

    def _deliver_async(self) -> None:
        """
        Dla każdego rozwiązanego/odrzuconego AsyncAtom: zaplanuj OCZEKUJĄCE
        kontynuacje jako microtaski (callback Promise = microtask).

        Consume-on-deliver: callbacki są zdejmowane z listy po zaplanowaniu,
        więc ponowne .then() na settled atomie nie re-uruchamia starych.
        """
        for aa in list(self._async.values()):
            if aa.state == ASYNC_PENDING:
                continue
            if aa.state == ASYNC_RESOLVED:
                if aa._on_resolve:
                    cbs = aa._on_resolve
                    aa._on_resolve = []
                    aa._on_reject  = []   # resolved → reject-handlery nieaktywne
                    for cb in cbs:
                        self.micro(cb, aa.value)
            elif aa.state == ASYNC_REJECTED:
                if aa._on_reject:
                    cbs = aa._on_reject
                    aa._on_reject  = []
                    aa._on_resolve = []
                    for cb in cbs:
                        self.micro(cb, aa.error)
                elif not aa._unhandled_reported and not aa._on_resolve:
                    # Brak reject-handlera — zgłoś unhandled rejection raz
                    if self._error_handler is not None:
                        self._error_handler(aa.error)
                    aa._unhandled_reported = True

    def _drain_ready(self) -> int:
        """
        Drenaż gotowych tasków hottest-first.
        Po każdym wykonaniu re-scan (task może dodać nowe gorące microtaski).
        Microtask (T=100) zawsze przed macrotask (T=75) — bo cieplejszy.
        Guard przed nieskończoną pętlą microtasków.
        """
        ran = 0
        guard = 0
        while True:
            guard += 1
            if guard > MAX_DRAIN:
                if self._error_handler:
                    self._error_handler(
                        "ThermalLoop: przekroczono MAX_DRAIN — "
                        "prawdopodobnie nieskończona pętla microtasków")
                break

            # Najgorętszy gotowy task
            hottest: Optional[Task] = None
            for t in self._ready:
                if t.cancelled or not t.is_ready():
                    continue
                if hottest is None or t.T > hottest.T:
                    hottest = t

            if hottest is None:
                break

            self._run(hottest)
            ran += 1
            self._ran_total += 1

            if hottest.repeating:
                # interval-macrotask: reset (rzadkie, większość intervali to lonty)
                hottest.T = T_MACRO
            else:
                self._ready.remove(hottest)

        return ran

    def _run(self, task: Task) -> None:
        """Wykonuje callback taska z izolacją błędów."""
        try:
            self._invoke(task.fn, *task.args)
        except Exception as e:
            if self._error_handler is not None:
                self._error_handler(str(e))
            # bez handlera — task umiera cicho (jak uncaught w event loop)

    # ── Inspekcja ─────────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        return {
            "tick":          self._tick_n,
            "ready":         len([t for t in self._ready if not t.cancelled]),
            "timers":        len([t for t in self._timers if not t.cancelled]),
            "async_total":   len(self._async),
            "async_pending": sum(1 for a in self._async.values()
                                 if a.state == ASYNC_PENDING),
            "ran_total":     self._ran_total,
            "fired_total":   self._fired_total,
        }

    def __repr__(self) -> str:
        s = self.stats()
        return (f"ThermalLoop(tick={s['tick']}, ready={s['ready']}, "
                f"timers={s['timers']}, async={s['async_pending']}p)")


# ─── AsyncAtom — zamiennik Promise ────────────────────────────────────────────

class AsyncAtom:
    """
    Atom asynchroniczny. Zamiennik Promise.

    PENDING  = COLD  — czeka na rozwiązanie (host nie ogrzał).
    RESOLVED = HOT   — host ogrzał, ma wartość. Kontynuacje odpalają się.
    REJECTED = TOMB  — host zabił, ma błąd. Reject-handlery odpalają się.

    Settled raz — drugi resolve/reject jest ignorowany (jak Promise).
    Kontynuacje są microtaskami w ThermalLoop.
    """

    __slots__ = (
        "_loop", "id", "state", "value", "error", "T",
        "_on_resolve", "_on_reject", "_unhandled_reported",
    )

    def __init__(self, loop: ThermalLoop, id: int):
        self._loop       = loop
        self.id          = id
        self.state       = ASYNC_PENDING
        self.value       = None
        self.error       = None
        self.T           = T_COLD       # zimne ziarno
        self._on_resolve: List[Callable] = []
        self._on_reject:  List[Callable] = []
        self._unhandled_reported = False

    def resolve(self, value: Any) -> None:
        """Host rozwiązuje — ogrzewa atom do HOT, zapisuje wartość."""
        if self.state != ASYNC_PENDING:
            return  # settled — ignoruj (semantyka Promise)
        # Jeśli rozwiązujemy innym AsyncAtom — łańcuchujemy (Promise resolution)
        if isinstance(value, AsyncAtom):
            value.then(self.resolve, self.reject)
            return
        self.state = ASYNC_RESOLVED
        self.value = value
        self.T     = T_MAX             # ogrzany

    def reject(self, error: Any) -> None:
        """Host odrzuca — zabija atom do TOMB, zapisuje błąd."""
        if self.state != ASYNC_PENDING:
            return
        self.state = ASYNC_REJECTED
        self.error = error
        self.T     = T_FIRE * 0.5      # poniżej progu — martwy

    def then(self,
             on_resolve: Optional[Callable] = None,
             on_reject:  Optional[Callable] = None) -> "AsyncAtom":
        """
        Rejestruje kontynuacje. Zwraca nowy AsyncAtom (łańcuchowanie).
        Wynik on_resolve rozwiązuje następny atom w łańcuchu.
        Callbacki wołane przez loop._invoke — działa dla Python i JS Function.
        """
        next_atom = self._loop.create_async()
        loop = self._loop

        def _wrap_resolve(val):
            try:
                if on_resolve is not None:
                    result = loop._invoke(on_resolve, val)
                    next_atom.resolve(result)
                else:
                    next_atom.resolve(val)
            except Exception as e:
                next_atom.reject(str(e))

        def _wrap_reject(err):
            try:
                if on_reject is not None:
                    result = loop._invoke(on_reject, err)
                    next_atom.resolve(result)  # catch zmienia reject w resolve
                else:
                    next_atom.reject(err)       # propagacja błędu
            except Exception as e:
                next_atom.reject(str(e))

        self._on_resolve.append(_wrap_resolve)
        self._on_reject.append(_wrap_reject)
        return next_atom

    def catch(self, on_reject: Callable) -> "AsyncAtom":
        """Skrót then(None, on_reject)."""
        return self.then(None, on_reject)

    def _has_continuations(self) -> bool:
        return bool(self._on_resolve or self._on_reject)

    def __repr__(self) -> str:
        return f"AsyncAtom(#{self.id}, {self.state}, T={self.T:.1f})"


# ─── PhiWorker — zamiennik Web Worker ──────────────────────────────────────────

class WorkerMessage:
    """
    Komunikat między workerami. Ładunek jest KOPIOWANY przy transferze —
    brak współdzielonej pamięci (structured clone w V8, tutaj deep copy).
    """

    __slots__ = ("channel", "payload", "_born")

    def __init__(self, channel: str, payload: Any):
        self.channel = channel
        # Kopia — izolacja jak structured clone w Web Workers
        self.payload = copy.deepcopy(payload)
        self._born   = time.monotonic()

    def __repr__(self) -> str:
        return f"WorkerMessage({self.channel!r}, {self.payload!r})"


class PhiWorker:
    """
    Worker oparty na izolowanym phi-space. Zamiennik Web Worker.

    Wewnątrz: sandbox KarmazynJSPhi (parent=None → próżnia).
    Worker nie widzi phi-space rodzica — izolacja jest ontologiczna,
    nie polityczna. Nie da się złamać reguły której nie ma.

    Komunikacja przez skrzynki:
      post(channel, payload)         — main → worker (do inbox)
      on_message(channel, handler)   — main rejestruje handler na wiadomości
                                       FROM worker
      _emit(channel, payload)        — worker → main (z kodu workera)

    Dopasowanie kanału = rezonans przy tau=1.0. Hak _resonates() pozwala
    podmienić na pełny φ-space (Ring-LWE) gdzie kanały to wektory.
    """

    def __init__(self, name: str = "worker", vm_factory: Callable = None):
        self.name = name
        # Lazy import — PhiWorker działa też bez pełnego JS (czyste callbacki)
        if vm_factory is not None:
            self._vm = vm_factory()
        else:
            try:
                from karmazyn_js_phi import KarmazynJSPhi
                self._vm = KarmazynJSPhi(runtime=None, context=f"worker_{name}")
            except ImportError:
                self._vm = None

        self._inbox:  List[WorkerMessage] = []     # main → worker
        self._outbox: List[WorkerMessage] = []     # worker → main
        self._main_handlers: Dict[str, List[Callable]] = {}   # na FROM worker
        self._worker_handlers: Dict[str, List[Callable]] = {} # na TO worker
        self._terminated = False
        self._script_fn: Optional[Callable] = None

    # ── Rezonans (dopasowanie kanału) ──────────────────────────────────────────

    @staticmethod
    def _resonates(channel_a: str, channel_b: str, tau: float = 1.0) -> bool:
        """
        Czy dwa kanały rezonują? Domyślnie exact match (tau=1.0).
        Podmień na cosine(φ_a, φ_b) >= tau dla pełnego φ-space.

        '*' to kanał wszechrezonansowy (nasłuch wszystkiego).
        """
        if channel_a == "*" or channel_b == "*":
            return True
        return channel_a == channel_b

    # ── Main → Worker ─────────────────────────────────────────────────────────

    def post(self, channel: str, payload: Any) -> None:
        """Main thread wysyła wiadomość DO workera (kopiowanie ładunku)."""
        if self._terminated:
            raise RuntimeError(f"Worker '{self.name}' zterminowany")
        self._inbox.append(WorkerMessage(channel, payload))

    def on_worker_message(self, channel: str, handler: Callable) -> None:
        """Worker rejestruje handler na wiadomości przychodzące (inbox)."""
        self._worker_handlers.setdefault(channel, []).append(handler)

    # ── Worker → Main ─────────────────────────────────────────────────────────

    def _emit(self, channel: str, payload: Any) -> None:
        """Kod workera wysyła wiadomość DO main thread (do outbox)."""
        if self._terminated:
            return
        self._outbox.append(WorkerMessage(channel, payload))

    def on_message(self, channel: str, handler: Callable) -> None:
        """Main thread rejestruje handler na wiadomości OD workera."""
        self._main_handlers.setdefault(channel, []).append(handler)

    # ── Pump — przetwarzanie skrzynek ───────────────────────────────────────────

    def pump(self) -> Dict[str, int]:
        """
        Jeden cykl przetwarzania workera:
          1. Dostarcz inbox do worker_handlers (main → worker).
          2. Dostarcz outbox do main_handlers (worker → main).

        Wywoływany przez host (np. z ThermalLoop co tick).
        Zwraca liczbę dostarczonych wiadomości w każdą stronę.
        """
        if self._terminated:
            return {"to_worker": 0, "to_main": 0}

        to_worker = self._deliver(self._inbox, self._worker_handlers)
        self._inbox = []

        to_main = self._deliver(self._outbox, self._main_handlers)
        self._outbox = []

        return {"to_worker": to_worker, "to_main": to_main}

    def _deliver(self, mailbox: List[WorkerMessage],
                 handlers: Dict[str, List[Callable]]) -> int:
        """Dostarcza wiadomości do handlerów które rezonują z kanałem."""
        delivered = 0
        for msg in mailbox:
            for ch, hlist in handlers.items():
                if self._resonates(msg.channel, ch):
                    for h in hlist:
                        try:
                            # Kopia ładunku per-handler — brak współdzielenia
                            h(copy.deepcopy(msg.payload))
                            delivered += 1
                        except Exception:
                            pass
        return delivered

    # ── Kod workera ─────────────────────────────────────────────────────────────

    def run_script(self, ast: list) -> Any:
        """
        Uruchamia skrypt JS w izolowanym phi-space workera.
        Skrypt ma dostęp do postMessage(channel, payload) — wysyła do main.
        """
        if self._vm is None:
            raise RuntimeError("PhiWorker bez VM — użyj vm_factory lub czystych callbacków")
        # Wstrzyknij postMessage do scope workera
        self._vm.global_scope.vars["postMessage"] = self._emit
        return self._vm.run(ast)

    def set_script(self, fn: Callable) -> None:
        """Ustawia funkcję workera (czysty Python, bez JS)."""
        self._script_fn = fn

    def terminate(self) -> None:
        """Terminuje workera — phi-space znika, skrzynki czyszczone."""
        self._terminated = True
        self._inbox.clear()
        self._outbox.clear()
        self._vm = None

    @property
    def vm(self):
        return self._vm

    def stats(self) -> Dict[str, Any]:
        return {
            "name":        self.name,
            "terminated":  self._terminated,
            "inbox":       len(self._inbox),
            "outbox":      len(self._outbox),
            "main_channels":   list(self._main_handlers.keys()),
            "worker_channels": list(self._worker_handlers.keys()),
        }

    def __repr__(self) -> str:
        return (f"PhiWorker({self.name!r}, "
                f"in={len(self._inbox)}, out={len(self._outbox)}, "
                f"{'DEAD' if self._terminated else 'ALIVE'})")


# ─── Integracja z JS ──────────────────────────────────────────────────────────

def bind_event_loop(vm, loop: ThermalLoop, tick_ms: float = TICK_MS) -> None:
    """
    Wstrzykuje prymitywy event loop do global scope VM:
      setTimeout(fn, ms)      — lont (ms → ticki przez tick_ms)
      setInterval(fn, ms)     — powtarzalny lont
      clearTimeout(id)        — anuluje
      clearInterval(id)       — anuluje
      queueMicrotask(fn)      — microtask (born hot)

    fn to Function JS — wykonywana przez vm._call.
    """
    def _to_ticks(ms) -> int:
        try:
            return max(0, round(float(ms) / tick_ms))
        except (TypeError, ValueError):
            return 0

    def _set_timeout(fn, ms=0, *rest):
        ticks = _to_ticks(ms)
        return loop.set_timeout(lambda: vm._call(fn, list(rest)), ticks)

    def _set_interval(fn, ms=0, *rest):
        ticks = _to_ticks(ms)
        return loop.set_interval(lambda: vm._call(fn, list(rest)), ticks)

    def _clear(tid):
        return loop.clear_timeout(int(tid)) if tid is not None else False

    def _queue_micro(fn, *rest):
        return loop.micro(lambda: vm._call(fn, list(rest)))

    vm.global_scope.vars["setTimeout"]     = _set_timeout
    vm.global_scope.vars["setInterval"]    = _set_interval
    vm.global_scope.vars["clearTimeout"]   = _clear
    vm.global_scope.vars["clearInterval"]  = _clear
    vm.global_scope.vars["queueMicrotask"] = _queue_micro


def bind_async(vm, loop: ThermalLoop) -> None:
    """
    Wstrzykuje Promise-like do global scope VM.

    Promise(executor) — executor(resolve, reject) wykonywany natychmiast.
    Zwraca AsyncAtom z metodami then/catch.

    Uwaga: AsyncAtom.then/catch są metodami Pythona — dostęp przez prop
    handler w Core (obj.method() działa dla obiektów Python z getattr).
    """
    def _promise(executor):
        aa = loop.create_async()
        try:
            vm._call(executor, [aa.resolve, aa.reject])
        except Exception as e:
            aa.reject(str(e))
        return aa

    vm.global_scope.vars["Promise"] = _promise


def install_async_runtime(vm, loop: Optional[ThermalLoop] = None,
                          tick_ms: float = TICK_MS) -> ThermalLoop:
    """
    Instaluje pełny runtime async do VM: event loop + promisy.
    Zwraca ThermalLoop (utworzony jeśli nie podano).

    Użycie:
        from karmazyn_js_phi import KarmazynJSPhi
        from karmazyn_async import install_async_runtime
        vm = KarmazynJSPhi()
        loop = install_async_runtime(vm)
        vm.run(parse_js("setTimeout(() => console.log('hi'), 100);"))
        loop.run()   # napędza pętlę
    """
    if loop is None:
        loop = ThermalLoop()
    loop.set_vm(vm)
    bind_event_loop(vm, loop, tick_ms)
    bind_async(vm, loop)
    return loop


# ─── Self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 64)
    print("karmazyn_async.py — testy jednostkowe")
    print("=" * 64)

    passed = failed = 0
    def chk(name, ok, detail=""):
        global passed, failed
        print(f"  {'OK ' if ok else 'XX '} {name}")
        if detail and not ok:
            print(f"       {detail}")
        if ok: passed += 1
        else:  failed += 1

    # ── ThermalLoop: kolejność microtask vs macrotask ───────────────────────────
    print("\n[1] ThermalLoop — microtask przed macrotask")
    loop = ThermalLoop()
    order = []
    loop.macro(lambda: order.append("macro1"))
    loop.micro(lambda: order.append("micro1"))
    loop.macro(lambda: order.append("macro2"))
    loop.micro(lambda: order.append("micro2"))
    loop.run()
    chk("microtaski przed macrotaskami",
        order == ["micro1", "micro2", "macro1", "macro2"],
        f"got {order}")

    # ── ThermalLoop: timer fire po N tickach ─────────────────────────────────────
    print("\n[2] ThermalLoop — timer (lont)")
    loop = ThermalLoop()
    fired_at = []
    loop.set_timeout(lambda: fired_at.append(loop._tick_n), 5)
    loop.run()
    chk("timer odpalił się", len(fired_at) == 1, f"fired_at={fired_at}")
    chk("timer odpalił po ~5 tickach", 5 <= fired_at[0] <= 7 if fired_at else False,
        f"fired_at={fired_at}")

    # ── ThermalLoop: setInterval ──────────────────────────────────────────────────
    print("\n[3] ThermalLoop — setInterval")
    loop = ThermalLoop()
    ticks_hit = []
    iid = loop.set_interval(lambda: ticks_hit.append(loop._tick_n), 3)
    for _ in range(20):
        loop.tick()
        if len(ticks_hit) >= 3:
            loop.clear_timeout(iid)
            break
    chk("interval odpalił >=3 razy", len(ticks_hit) >= 3, f"hits={ticks_hit}")
    chk("interval w równych odstępach",
        len(ticks_hit) >= 2 and all(
            abs((ticks_hit[i]-ticks_hit[i-1]) - (ticks_hit[1]-ticks_hit[0])) <= 1
            for i in range(2, len(ticks_hit))),
        f"hits={ticks_hit}")

    # ── ThermalLoop: clearTimeout ─────────────────────────────────────────────────
    print("\n[4] ThermalLoop — clearTimeout")
    loop = ThermalLoop()
    fired = []
    tid = loop.set_timeout(lambda: fired.append(1), 5)
    loop.clear_timeout(tid)
    loop.run()
    chk("anulowany timer nie odpalił", len(fired) == 0, f"fired={fired}")

    # ── AsyncAtom: resolve ────────────────────────────────────────────────────────
    print("\n[5] AsyncAtom — resolve + then")
    loop = ThermalLoop()
    results = []
    aa = loop.create_async()
    aa.then(lambda v: results.append(("got", v)))
    aa.resolve(42)
    loop.run()
    chk("then otrzymał wartość", results == [("got", 42)], f"results={results}")

    # ── AsyncAtom: reject + catch ─────────────────────────────────────────────────
    print("\n[6] AsyncAtom — reject + catch")
    loop = ThermalLoop()
    errs = []
    aa = loop.create_async()
    aa.catch(lambda e: errs.append(e))
    aa.reject("boom")
    loop.run()
    chk("catch otrzymał błąd", errs == ["boom"], f"errs={errs}")

    # ── AsyncAtom: łańcuch then ───────────────────────────────────────────────────
    print("\n[7] AsyncAtom — łańcuch then")
    loop = ThermalLoop()
    chain = []
    aa = loop.create_async()
    (aa.then(lambda v: v + 1)
       .then(lambda v: v * 10)
       .then(lambda v: chain.append(v)))
    aa.resolve(4)
    loop.run()
    chk("łańcuch: (4+1)*10 = 50", chain == [50], f"chain={chain}")

    # ── AsyncAtom: settled raz ────────────────────────────────────────────────────
    print("\n[8] AsyncAtom — settled raz")
    loop = ThermalLoop()
    vals = []
    aa = loop.create_async()
    aa.then(lambda v: vals.append(v))
    aa.resolve(1)
    aa.resolve(2)   # ignorowane
    aa.reject("x")  # ignorowane
    loop.run()
    chk("drugi resolve ignorowany", vals == [1], f"vals={vals}")

    # ── PhiWorker: izolacja + message passing ───────────────────────────────────
    print("\n[9] PhiWorker — message passing")
    w = PhiWorker("test")
    received = []
    w.on_message("result", lambda payload: received.append(payload))
    # Worker handler na inbox
    w.on_worker_message("compute", lambda payload: w._emit("result", payload * 2))
    w.post("compute", 21)
    w.pump()   # inbox → worker_handler → outbox
    w.pump()   # outbox → main_handler
    chk("worker policzył i odesłał", received == [42], f"received={received}")

    # ── PhiWorker: kopiowanie ładunku (brak współdzielenia) ─────────────────────
    print("\n[10] PhiWorker — kopiowanie ładunku")
    w = PhiWorker("isolation")
    original = {"data": [1, 2, 3]}
    leaked = []
    w.on_worker_message("in", lambda p: (p["data"].append(999), leaked.append(p)))
    w.post("in", original)
    w.pump()
    chk("oryginał niezmieniony (deep copy)", original["data"] == [1, 2, 3],
        f"original={original}")
    chk("worker dostał kopię z mutacją", leaked and leaked[0]["data"] == [1,2,3,999],
        f"leaked={leaked}")

    # ── PhiWorker: terminate ──────────────────────────────────────────────────────
    print("\n[11] PhiWorker — terminate")
    w = PhiWorker("dead")
    w.terminate()
    raised = False
    try:
        w.post("x", 1)
    except RuntimeError:
        raised = True
    chk("post po terminate rzuca", raised)

    # ── Integracja JS: setTimeout ─────────────────────────────────────────────────
    print("\n[12] Integracja JS — setTimeout")
    try:
        from karmazyn_js_phi import KarmazynJSPhi
        from karmazyn_js_parser import parse_js
        vm = KarmazynJSPhi()
        js_log = []
        vm.global_scope.vars["log"] = lambda x: js_log.append(x)
        loop = install_async_runtime(vm)
        vm.run(parse_js("setTimeout(function(){ log('fired'); }, 80);"))
        loop.run()
        chk("setTimeout z JS odpalił", js_log == ["fired"], f"js_log={js_log}")
    except Exception as e:
        chk("setTimeout z JS odpalił", False, str(e))

    # ── Integracja JS: Promise ────────────────────────────────────────────────────
    print("\n[13] Integracja JS — Promise")
    try:
        from karmazyn_js_phi import KarmazynJSPhi
        from karmazyn_js_parser import parse_js
        vm = KarmazynJSPhi()
        p_log = []
        vm.global_scope.vars["log"] = lambda x: p_log.append(x)
        loop = install_async_runtime(vm)
        vm.run(parse_js("""
            var p = Promise(function(resolve, reject){ resolve(7); });
            p.then(function(v){ log(v); });
        """))
        loop.run()
        chk("Promise.then z JS dostał wartość", p_log == [7], f"p_log={p_log}")
    except Exception as e:
        chk("Promise.then z JS dostał wartość", False, str(e))

    print(f"\n{'=' * 64}")
    print(f"Wyniki: {passed}/{passed + failed}")
    print("PASS — async engine operacyjny" if failed == 0
          else f"FAIL — {failed} testów nie przeszło")
    print("=" * 64)