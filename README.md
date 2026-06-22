# Luneta Browser

Termodynamiczna przeglądarka terminalowa dla KarmazynOS.

> **JavaScript wykonuje się na własnym silniku phi-space — nie na V8.** Strony
> renderują się z atomów o zmiennej temperaturze, zbieranych przez GC po
> osiągalności (reach-GC). To nie nakładka na `requests` + parser — to język
> programowania wykonywany na termodynamicznym substracie.

Luneta zamiast ciężkiego, tradycyjnego drzewa DOM mapuje stronę na **phi-space**
– przestrzeń atomów o zmiennej temperaturze. Zamiast frameworków używa heurystyk
**Data Islands** do wyciągania ustrukturyzowanych danych (JSON-LD, embedded
state) z nowoczesnych stron, a wydajność zapewnia autorski silnik asynchroniczny
i semantyczna pamięć masowa.

## Architektura systemu

Wysoce modularna — silnik OS i przeglądarka rozwijają się niezależnie.

**Rdzeń (silnik):**
- **karmazyn_atom.py** — Unified Atom Model: jeden kanoniczny atom (FSM HOT/WARM/COLD/TOMB, twarz wektora).
- **karmazyn_substrate.py** — Kanoniczny substrat: posiadający magazyn atomów + bąble + korzenie z **reach-GC**. Temperatura mówi *kiedy*, osiągalność mówi *czy* — zimny+osiągalny → archiwum, zimny+sierota → GC. To jest silnik, na którym stoi cała reszta.
- **karmazyn_hrr.py** — Operacje HRR (splot/korelacja kołowa, D=2048). Trzecia twarz atomu: wektor odtwarzany z nazwy. Opcjonalna (wymaga numpy) — bez niej rdzeń działa, traci tylko rezonans.

**Runtime:**
- **luneta_runtime.py** — Runtime Lunety na substracie (engine = `Store`, reach-GC). Atom trzymany przez bąbel przeżywa stygnięcie; sierota ginie.
- **karmazyn_atomstore.py** — Kanoniczny kontrakt magazynu atomów (protokół, nie implementacja) — jeden dialekt atomu dla całego OS.

**Sieć i treść:**
- **karmazyn_browser.py** — Silnik przeglądarki (Luneta v5.5): HTTP, rendering ANSI, Smart Data Islands.
- **luneta_text.py** — Dekodowanie i normalizacja treści HTTP (Content-Encoding, charset, ocena czytelności). Wspólne dla browsera i DOM.
- **karmazyn_dom.py** — DOM → Phi-Space Mapper: natywne mapowanie struktury strony na atomy i bąble.

**JavaScript (własny silnik):**
- **karmazyn_js_parser.py** — Lexer + Parser: ES5 + klasy + destrukturyzacja → AST.
- **karmazyn_js_core.py** — Czysty interpreter: sama logika języka, bez termiki i zależności zewnętrznych.
- **karmazyn_js_phi.py** — Warstwa phi-space nad interpreterem: termodynamika, GC, sandbox, wykrywanie anomalii.
- **karmazyn_live_dom.py** — Dynamiczny DOM: każdy węzeł to żywy atom phi-space.
- **karmazyn_js_web.py** — JSBridge: most łączący silnik JS z przeglądarką.

**Asynchroniczność:**
- **karmazyn_async.py** — Silnik async oparty na termodynamice: ThermalLoop (event loop, temperatura = priorytet), AsyncAtom (Promise), PhiWorker (Web Worker).
- **luneta_async_bridge.py** — Podpięcie async engine + `fetch` pod JSBridge (setTimeout, setInterval, Promise, fetch).

**Pamięć i format:**
- **karmazyn_recall.py** — Pamięć semantyczna: części wspólne zapisanych stron i przywoływanie fragmentów (recall/common), lokalnie, na naparstku energii.
- **karmazyn_kafd.py** — KAFD: binarny protokół przepływu informacji KarmazynOS (warstwa OS, trwałość — poza ścieżką renderowania).

**Wejście:**
- **luneta.py** — Punkt wejścia CLI (REPL, system pomocy).

## Instalacja

Luneta wymaga Pythona 3.10+.

1. Sklonuj repozytorium:

```bash
git clone https://github.com/Maciej-EriAmo/EriAmo
cd EriAmo
```

> Jeśli pliki Lunety leżą w podkatalogu repozytorium, wejdź do niego (`cd Luneta`).

2. Zależności są **opcjonalne** — rdzeń działa na czystej bibliotece standardowej.
   Zainstaluj dla pełni funkcji:

```bash
pip install brotli numpy wcwidth
```

> `brotli` — nowoczesne strony (np. onet); `numpy` — twarz wektora HRR i rezonans
> w `recall`; `wcwidth` — poprawna szerokość znaków CJK. Bez nich silnik chodzi,
> degradując łagodnie tylko te funkcje.

## Uruchomienie

```bash
python luneta.py
```

W powłoce Lunety wpisz `help`, aby poznać dostępne komendy (np. `dom map`,
`js status`, `recall`).

## Wyzwania i rozwój

Projekt jest obecnie w fazie intensywnej integracji. Kluczowe kierunki:

- Migracja rdzenia do języka **Rust** (wsparcie wielordzeniowości, zamrożony silnik).
- **Tier 3** silnika JS: generatory, async/await (pętla async już działa).
- Sprzętowo akcelerowany renderer graficzny.

## Licencja i autorstwo

Copyright (c) 2026 Maciej Mazur.

Projekt na licencji MIT. Zezwala się na wykorzystanie, modyfikację i dystrybucję
kodu pod warunkiem zachowania informacji o autorze w nagłówkach plików.