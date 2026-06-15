"""
karmazyn_atomstore.py — Kanoniczny kontrakt magazynu atomów KarmazynOS
=======================================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

JEDEN dialekt atomu dla całego OS — żeby Luneta, karmazyn_db i wszystko inne
mówiło tym samym językiem i nie powstała herezja dwóch niekompatybilnych
przestrzeni.

To jest SPECYFIKACJA (protokół), nie kolejna implementacja. Definiuje
minimalną powierzchnię, którą musi spełniać każdy magazyn atomów —
zarówno lekki LunetaRuntime, jak i pełny karmazyn_phi.PhiSpace.

Rdzeń (WYMAGANY — każdy magazyn musi mieć):
    create_atom(id, S, E, T, **kw) -> id
    get_atom(id) -> Atom | None
    has_atom(id) -> bool
    create_bubble(label, atom_ids=None) -> label    # atom_ids OPCJONALNE
    import_to_bubble(bubble_label, atom_id) -> bool
    get_bubble(label) -> bubble | None

Rozszerzenia (OPCJONALNE — wykrywane przez capabilities()):
    find_resonating(concept, T_min, limit) -> [Atom]   # rezonans semantyczny
    enable_hrr(D)                                      # wiązanie HRR (splot)
    archive_to_hologram(topic, atom_ids, ...)          # hologram

Konsumenci (np. karmazyn_recall) zależą od TEGO kontraktu, nie od konkretnej
klasy. Gdy magazyn ma find_resonating → używają rezonansu; gdy nie → term-overlap.
Jeden kod, oba magazyny, zero dialektów.
"""

from typing import Any, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class AtomStore(Protocol):
    """Rdzeń kontraktu — to musi mieć każdy magazyn atomów w KarmazynOS."""

    def create_atom(self, id: str, S: str, E: str, T: float, **kwargs) -> Any: ...
    def get_atom(self, id: str) -> Optional[Any]: ...
    def has_atom(self, id: str) -> bool: ...
    def create_bubble(self, label: str,
                      atom_ids: Optional[List[str]] = None) -> Optional[str]: ...
    def import_to_bubble(self, bubble_label: str, atom_id: str) -> bool: ...
    def get_bubble(self, label: str) -> Optional[Any]: ...


# ─── Wykrywanie zdolności (zamiast zgadywania typu) ───────────────────────────

def capabilities(store: Any) -> dict:
    """
    Co dany magazyn potrafi ponad rdzeń. Konsumenci dobierają ścieżkę
    wg zdolności, nie wg klasy — to jest mechanizm anty-herezja.
    """
    return {
        "semantic":  callable(getattr(store, "find_resonating", None)),
        "tinted":    callable(getattr(store, "resonate", None)),
        "hrr":       callable(getattr(store, "enable_hrr", None)),
        "hologram":  callable(getattr(store, "archive_to_hologram", None)),
        "consolidate": callable(getattr(store, "consolidate", None)),
    }


# ─── Sprawdzian zgodności (do testów / startu OS) ─────────────────────────────

CORE_METHODS = ("create_atom", "get_atom", "has_atom",
                "create_bubble", "import_to_bubble", "get_bubble")


def conforms(store: Any) -> List[str]:
    """
    Zwraca listę BRAKUJĄCYCH metod rdzenia. Pusta lista = magazyn zgodny
    z kanonem. Użyj na starcie OS, by wykryć dialekt zanim wybuchnie.
    """
    return [m for m in CORE_METHODS
            if not callable(getattr(store, m, None))]


def assert_conforms(store: Any, name: str = "store") -> None:
    """Twardy strażnik — rzuca, jeśli magazyn łamie kanon."""
    missing = conforms(store)
    if missing:
        raise TypeError(
            f"{name} nie spełnia kontraktu AtomStore — brak: {', '.join(missing)}. "
            f"Ujednolić obsługę atomów (karmazyn_atomstore.AtomStore)."
        )
