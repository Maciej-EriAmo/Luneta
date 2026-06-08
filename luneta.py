"""
luneta.py — Punkt wejścia Windows CLI dla Lunety v1.2
======================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Most łączący środowisko Windows z przeglądarką phi-space.
Wersja 1.2 dodaje wbudowany system pomocy.
"""

import os
import sys

# Odblokowanie sekwencji ANSI (VT100) dla Windows CMD i PowerShell.
if os.name == 'nt':
    os.system("")

try:
    from luneta_runtime import LunetaRuntime
    from karmazyn_browser import LunetaBrowser
    from karmazyn_dom import attach_to_browser, cmd_dom
except ImportError as e:
    print(f"Błąd importu środowiska KarmazynOS: {e}")
    print("Upewnij się, że plik luneta.py znajduje się w głównym katalogu systemu.")
    sys.exit(1)

# Async engine (opcjonalny — Luneta działa bez niego, ale bez setTimeout/Promise/fetch)
try:
    from luneta_async_bridge import install_async_engine_on_browser, cmd_async
    _HAS_ASYNC = True
except ImportError:
    _HAS_ASYNC = False
    def cmd_async(args, bridge):
        return None

def show_help():
    help_text = """
=== LUNETA — Przeglądarka KarmazynOS ===

NAWIGACJA:
  <url>               — Otwórz stronę (np. wp.pl, https://example.com)
  links | ln | l      — Wyświetl listę linków na bieżącej stronie
  <numer>             — Podążaj za linkiem o danym numerze (np. 5)
  f <numer>           — Alternatywa dla podążania za linkiem (follow 5)
  b | back            — Wstecz (poprzednia strona)
  r | reload          — Przeładuj bieżącą stronę

WIDOK:
  j <n> | scroll <n>  — Przewiń w dół o <n> linii (domyślnie 1, np. j 10)
  k                   — Przewiń o 1 linię w górę

WARSTWA PHI-SPACE (DOM Mapper):
  dom                 — Statystyki phi-space aktualnej strony
  dom map             — (Re)mapuj aktualną stronę do bąbli i atomów
  dom outline         — Szkielet strony (nagłówki jako atomy wg T)
  dom reader [T]      — Wyświetl atomy powyżej progu temperatury T (domyślnie 60)
  dom find <query>    — Szukaj słowa w atomach aktualnej strony
  dom phi <query>     — Szukaj słowa we wszystkich odwiedzonych stronach

WARSTWA JAVASCRIPT:
  js status           — Statystyki phi-space silnika JS
  js thermal          — Mapa temperatur zmiennych w środowisku JS

SILNIK ASYNC (ThermalLoop):
  js loop             — Statystyki pętli termodynamicznej (timery, promisy)
  js pump [n]         — Przesuń pętlę o n obrotów (domyślnie 1)
  js run [max]        — Napędź pętlę aż do bezczynności (cap max ticków)
  js errors           — Ostatnie błędy async

SYSTEM:
  help | h | ?        — Wyświetl tę pomoc
  exit | quit | q     — Zamknij środowisko
"""
    print(help_text)

def main():
    print("Inicjalizacja rdzenia...")
    runtime = LunetaRuntime()
    browser = LunetaBrowser(runtime)
    
    # Podpięcie DOMMappera
    mapper = attach_to_browser(browser, runtime)

    # Podpięcie silnika async (setTimeout/Promise/fetch + ThermalLoop)
    if _HAS_ASYNC:
        if install_async_engine_on_browser(browser):
            print("Silnik async aktywny (setTimeout, Promise, fetch, ThermalLoop).")
    
    print("\nLUNETA gotowa. Otwórz URL (np. wp.pl) lub wpisz 'help'.")

    while True:
        try:
            cmd_in = input("\nLUNETA> ").strip()
            if not cmd_in:
                continue
            
            parts = cmd_in.split()
            cmd_upper = parts[0].upper()

            # 1. System pomocy
            if cmd_upper in ("HELP", "H", "?"):
                show_help()

            # 2. Wyjście z programu
            elif cmd_upper in ("EXIT", "QUIT", "Q"):
                break
                
            # 3. Most termodynamiczny (DOM Mapper)
            elif cmd_upper == "DOM":
                print(cmd_dom(parts[1:], browser, mapper))

            # 4. Lista linków
            elif cmd_upper in ("LINKS", "LN", "L"):
                if getattr(browser, "_current", None) and hasattr(browser._current, "links"):
                    print("─" * 40)
                    for i, lnk in enumerate(browser._current.links):
                        print(f" [{i}] {lnk}")
                    print("─" * 40)
                else:
                    print("Brak linków w strukturze strony lub brak załadowanej strony.")

            # 5. Podążanie za linkiem (po numerze)
            elif cmd_upper.isdigit():
                if hasattr(browser, "follow_link"):
                    ok, msg = browser.follow_link(int(cmd_upper))
                    if msg: print(msg)
                else:
                    print("[!] Brak metody browser.follow_link(n) w Twojej klasie.")

            elif cmd_upper in ("F", "FOLLOW") and len(parts) > 1 and parts[1].isdigit():
                if hasattr(browser, "follow_link"):
                    ok, msg = browser.follow_link(int(parts[1]))
                    if msg: print(msg)

            # 6. Nawigacja w historii (Wstecz)
            elif cmd_upper in ("B", "BACK"):
                if hasattr(browser, "back"):
                    ok, msg = browser.back()
                    if msg: print(msg)
                else:
                    print("[!] Brak metody browser.back() w Twojej klasie.")

            # 7. Odświeżenie (Reload)
            elif cmd_upper in ("R", "RELOAD"):
                if hasattr(browser, "reload"):
                    ok, msg = browser.reload()
                    if msg: print(msg)
                else:
                    print("[!] Brak metody browser.reload() w Twojej klasie.")

            # 8. Przewijanie (Scroll vi-style)
            elif cmd_upper in ("J", "SCROLL"):
                steps = int(parts[1]) if len(parts) > 1 else 1
                if hasattr(browser, "scroll"):
                    res = browser.scroll(steps)
                    if res: print(res)
                else:
                    print("[!] Brak metody browser.scroll(n) w Twojej klasie.")

            elif cmd_upper == "K":
                if hasattr(browser, "scroll"):
                    res = browser.scroll(-1)
                    if res: print(res)

            # 9. Wywołanie silnika JS
            elif cmd_upper == "JS":
                if hasattr(browser, "_has_js") and browser._has_js and browser.js_bridge:
                    # Najpierw komendy async (pump/run/loop/errors)
                    async_out = cmd_async(parts[1:], browser.js_bridge)
                    if async_out is not None:
                        print(async_out)
                    else:
                        from karmazyn_js_web import cmd_js_bridge
                        print(cmd_js_bridge(parts[1:], browser.js_bridge))
                else:
                    print("Silnik JS niedostępny w tej sesji Lunety.")

            # 10. Domyślny Fallback — potraktuj wpis jako URL
            else:
                ok, msg = browser.go(cmd_in)
                if msg:
                    print(msg)

        except KeyboardInterrupt:
            print("\n[Ctrl+C] - Wpisz 'exit' aby zamknąć środowisko.")
        except EOFError:
            break
        except Exception as e:
            print(f"Błąd krytyczny wykonania REPL: {e}")

    print("Zamykanie Lunety. Powrót do systemu nadrzędnego.")

if __name__ == "__main__":
    main()