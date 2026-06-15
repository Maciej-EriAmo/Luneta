# Luneta Browser

Termodynamiczna przeglądarka terminalowa dla KarmazynOS.

> **JavaScript wykonuje się na własnym silniku phi-space — nie na V8.** Strony
> renderują się z atomów o zmiennej temperaturze, zbieranych przez GC po
> osiągalności. To nie nakładka na `requests` + parser — to język programowania
> wykonywany na termodynamicznym substracie.

Luneta zamiast ciężkiego, tradycyjnego drzewa DOM mapuje stronę na **phi-space** –
przestrzeń atomów o zmiennej temperaturze. Zamiast frameworków używa heurystyk
**Data Islands** do wyciągania ustrukturyzowanych danych (JSON-LD, embedded state)
z nowoczesnych stron, a wydajność zapewnia autorski silnik asynchroniczny
i semantyczna pamięć masowa.

## Architektura systemu

Wysoce modularna — przeglądarka i rdzeń systemu rozwijają się niezależnie:

- **karmazyn_atom.py** — Unified Atom Model: jeden, kanoniczny model atomu dla całego OS.
- **karmazyn_browser.py** — Silnik przeglądarki (Luneta v5.6): HTTP, rendering ANSI, obsługa danych z wysp (Data Islands).
- **karmazyn_dom.py** — DOM → Phi-Space Mapper: natywne mapowanie struktury stron na atomy i bąble.
- **karmazyn_js_core.py** — Czysty interpreter JS: sama logika języka, bez termiki i bez zależności zewnętrznych.
- **karmazyn_js_phi.py** — Warstwa Phi-Space dla JS: dodaje termodynamikę, sandbox i wykrywanie anomalii.
- **karmazyn_js_parser.py** — Lexer + Parser JS: pełne przetwarzanie na AST dla KarmazynJSCore.
- **karmazyn_js_web.py** — JSBridge: most łączący silnik JS z przeglądarką Luneta.
- **karmazyn_live_dom.py** — Dynamiczny DOM: każdy węzeł jako żywy atom phi-space.
- **luneta_text.py** — Dekodowanie i normalizacja treści HTTP (Content-Encoding, wykrywanie czytelnego tekstu). Wymagany przez `browser` i `dom`.
- **karmazyn_kafd.py** — KAFD: binarny format przepływu informacji (Karmazyn Atom Flow Datum).
- **luneta_runtime.py** — Lekki adapter runtime dla przeglądarki.

## Instalacja

Luneta wymaga Pythona 3.10+.

1. Sklonuj repozytorium:

```bash
git clone https://github.com/Maciej-EriAmo/EriAmo
cd EriAmo
```

> Jeśli pliki Lunety leżą w podkatalogu repozytorium, wejdź do niego (`cd Luneta`).

2. Zainstaluj zależności zewnętrzne:

```bash
pip install numpy brotli wcwidth
```

> Reszta opiera się na bibliotece standardowej Pythona.

## Uruchomienie

Punkt wejścia w terminalu:

```bash
python luneta.py
```

W powłoce Lunety wpisz `help`, aby poznać dostępne komendy (np. `dom map`,
`js status`, `recall`).

## Wyzwania i rozwój

Projekt jest obecnie w fazie intensywnej integracji. Kluczowe kierunki:

- Migracja rdzenia do języka **Rust** (wsparcie wielordzeniowości, zamrożony silnik).
- Dalsza implementacja **Tier 3** silnika JS (generatory, async/await).
- Sprzętowo akcelerowany renderer graficzny.

## Licencja i autorstwo

Copyright (c) 2026 Maciej Mazur.

Projekt na licencji MIT. Zezwala się na wykorzystanie, modyfikację i dystrybucję
kodu pod warunkiem zachowania informacji o autorze w nagłówkach plików.