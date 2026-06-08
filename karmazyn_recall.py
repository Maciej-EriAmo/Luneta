"""
karmazyn_recall.py — Pamięć semantyczna Lunety (wydobycie + części wspólne)
============================================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Mechanizm, który robi z automatu to, za co wielkie silniki semantyczne palą
megawaty: bierze strony, które Luneta już pobrała i zapisała w phi-space,
i znajduje CZĘŚCI WSPÓLNE — tematy, które powtarzają się między stronami —
lokalnie, na naparstku energii.

Działa nad istniejącym substratem (runtime + DOMMapper._page_atoms),
bez nowych zależności. Czyta to, co DOMMapper już zmapował:
  atom.S = rola ("heading:h1", "text:p", "link")
  atom.E = treść
  atom.T = temperatura (waga ważności — nagłówki gorętsze niż tekst)

Komendy (przez LUNETA ...):
  COMMON [min_stron]   — tematy wspólne dla ≥min_stron zapisanych stron
  RECALL <zapytanie>   — przywołaj fragmenty rezonujące z zapytaniem
  MEMORY               — co jest w pamięci (strony, atomy, słownik)
  EXPORT [plik]        — zrzuć pamięć jako skrypt KarminQL do karmazyn_db

Skalowanie: term-overlap tutaj jest dowodem na małej energii. Ten sam
interfejs przepina się na rezonans HRR (karmazyn_db, D=2048) bez zmiany
API — wystarczy podmienić miarę podobieństwa term-set → wektor.
"""

import math
import re
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

try:
    from karmazyn_atomstore import capabilities
except ImportError:
    def capabilities(store):   # fallback, gdy kontrakt niedostępny
        return {"semantic": callable(getattr(store, "find_resonating", None)),
                "hrr": False, "hologram": False, "consolidate": False}

# Stopwords PL+EN — odsiewamy szum, zostawiamy znaczące pojęcia
_STOP = {
    # polski
    "i","w","we","na","z","ze","do","to","że","się","nie","jak","dla","po",
    "o","a","ale","czy","co","jest","są","był","była","było","być","oraz",
    "od","za","przez","tym","ten","ta","te","tego","tej","tych","jego","jej",
    "który","która","które","gdzie","kiedy","już","tylko","także","może",
    "lub","albo","bo","gdy","jeśli","aby","żeby","przy","bez","pod","nad",
    "wszystko","wszystkie","bardzo","tak","nią","nim","ich","im","mu","go",
    # angielski
    "the","of","and","to","a","in","is","for","on","with","as","by","at",
    "an","be","this","that","it","are","or","from","was","were","has","have",
    "not","but","you","your","all","can","will","more","about","which","they",
}

_WORD = re.compile(r"[0-9a-ząćęłńóśźż]+", re.IGNORECASE)


def _terms(text: str) -> List[str]:
    """Znaczące tokeny: małe litery, ≥3 znaki, bez stopwords."""
    out = []
    for t in _WORD.findall((text or "").lower()):
        if len(t) >= 3 and t not in _STOP:
            out.append(t)
    return out


def _short_label(url: str) -> str:
    """Czytelna etykieta strony z URL."""
    u = re.sub(r"^https?://", "", url or "").rstrip("/")
    return u[:48] if u else (url or "?")


class Recall:
    """
    Czyta pamięć Lunety (DOMMapper._page_atoms + runtime) i liczy części
    wspólne między stronami. Nie tworzy własnego stanu — substrat jest
    źródłem prawdy.
    """

    def __init__(self, runtime, dom_mapper):
        self.runtime = runtime
        self.dom = dom_mapper
        self.caps = capabilities(runtime)   # zgodnie z kontraktem AtomStore

    def _atom_page(self, aid: str) -> Optional[str]:
        """Która zapisana strona zawiera ten atom (odwrotne mapowanie)."""
        for url, ids in self._pages().items():
            if aid in ids:
                return url
        return None

    # ── Dostęp do zapisanych stron ────────────────────────────────────────────

    def _pages(self) -> Dict[str, List[str]]:
        return dict(getattr(self.dom, "_page_atoms", {}) or {})

    def _atom(self, aid: str):
        try:
            a = self.runtime.get_atom(aid)
            if a and not a.is_dead():
                return a
        except Exception:
            pass
        return None

    def _page_atoms(self, url: str) -> List[Any]:
        out = []
        for aid in self._pages().get(url, []):
            a = self._atom(aid)
            if a:
                out.append(a)
        return out

    def _content(self, a) -> str:
        """
        Treść atomu — JEDNA reguła, odporna na role/sygnatury.
        Link niesie znaczenie w S (po prefiksie 'link:'); URL jest w E
        i go pomijamy. Każdy inny atom (text:, sent:, heading:, title,
        code, nieznane) ma treść w E — NIGDY nie tokenizujemy nazwy roli.
        """
        s = a.S or ""
        if s.startswith("link:"):
            return s[5:]
        return a.E or ""

    def _page_profile(self, url: str) -> Dict[str, float]:
        """
        Profil strony: term → waga (częstość × temperatura atomu).
        Nagłówki (gorętsze) ważą więcej niż tekst — ważność z termodynamiki.
        """
        prof: Dict[str, float] = defaultdict(float)
        for a in self._page_atoms(url):
            weight = max(a.T, 1.0) / 50.0   # T~50 → waga ~1
            for term in _terms(self._content(a)):
                prof[term] += weight
        return dict(prof)

    def _hot_fragment(self, url: str, term: str) -> Tuple[str, float]:
        """Najgorętszy fragment strony zawierający dany term."""
        best, bestT = "", -1.0
        for a in self._page_atoms(url):
            e = self._content(a)
            if term in e.lower() and a.T > bestT:
                best, bestT = e.strip()[:120], a.T
        return best, max(bestT, 0.0)

    # ── 1. CZĘŚCI WSPÓLNE ───────────────────────────────────────────────────

    def common(self, min_pages: int = 2, top: int = 12
               ) -> Tuple[List[Tuple[str, int, float, List[str]]],
                          List[Tuple[str, str, float]]]:
        """
        Tematy wspólne dla ≥min_pages stron.
        Zwraca:
          terms = [(term, liczba_stron, łączna_waga, [etykiety_stron]), ...]
          pairs = [(strona_a, strona_b, podobieństwo_jaccard), ...]
        """
        pages = self._pages()
        profiles = {u: self._page_profile(u) for u in pages}
        profiles = {u: p for u, p in profiles.items() if p}  # tylko niepuste

        # term → {url: waga}
        term_pages: Dict[str, Dict[str, float]] = defaultdict(dict)
        for url, prof in profiles.items():
            for term, w in prof.items():
                term_pages[term][url] = w

        terms = []
        for term, pg in term_pages.items():
            if len(pg) >= min_pages:
                total = sum(pg.values())
                labels = [_short_label(u) for u in pg]
                terms.append((term, len(pg), total, labels))
        terms.sort(key=lambda x: (-x[1], -x[2]))   # najpierw najszerzej dzielone

        # Podobieństwo par stron (Jaccard na zbiorach znaczących termów)
        urls = list(profiles.keys())
        pairs = []
        for i in range(len(urls)):
            for j in range(i + 1, len(urls)):
                sa = set(profiles[urls[i]])
                sb = set(profiles[urls[j]])
                if not sa or not sb:
                    continue
                jac = len(sa & sb) / len(sa | sb)
                if jac > 0.0:
                    pairs.append((_short_label(urls[i]),
                                  _short_label(urls[j]), jac))
        pairs.sort(key=lambda x: -x[2])
        return terms[:top], pairs[:8]

    # ── 2. PRZYWOŁANIE (RECALL) ───────────────────────────────────────────────

    def recall(self, query: str, limit: int = 8
               ) -> List[Tuple[str, str, float, float]]:
        """
        Przywołaj fragmenty rezonujące z zapytaniem ze WSZYSTKICH stron.
        Jeśli magazyn ma rezonans (karmazyn_phi) — używa go; inaczej term-overlap.
        Zwraca [(etykieta_strony, fragment, score, T), ...].
        """
        if self.caps.get("semantic"):
            sem = self._recall_semantic(query, limit)
            if sem is not None:        # None = ścieżka semantyczna zawiodła → fallback
                return sem
        return self._recall_terms(query, limit)

    def _recall_semantic(self, query: str, limit: int
                         ) -> Optional[List[Tuple[str, str, float, float]]]:
        """
        Rezonans przez magazyn (karmazyn_phi.find_resonating). W pełni osłonięty:
        każdy błąd → None → fallback do term-overlap. Aktywuje się po wpięciu
        Lunety w PhiSpace z HRR; tu nie wywróci działającej ścieżki.
        """
        try:
            seen, hits = set(), []
            for term in set(_terms(query)):
                atoms = self.runtime.find_resonating(term, T_min=0.0, limit=limit) or []
                for a in atoms:
                    aid = getattr(a, "id", None)
                    if aid in seen:
                        continue
                    seen.add(aid)
                    try:
                        a.touch()      # odczyt grzeje atom (termodynamika)
                    except Exception:
                        pass
                    url = self._atom_page(aid) or "?"
                    content = self._content(a)
                    hits.append((_short_label(url), content.strip()[:140],
                                 float(getattr(a, "T", 0.0)), float(getattr(a, "T", 0.0))))
            if not hits and query.strip():
                return None            # nic nie znalazł — spróbuj term-overlap
            hits.sort(key=lambda x: -x[2])
            return hits[:limit]
        except Exception:
            return None

    def _recall_terms(self, query: str, limit: int
                      ) -> List[Tuple[str, str, float, float]]:
        q = set(_terms(query))
        if not q:
            return []
        hits = []
        for url in self._pages():
            label = _short_label(url)
            for a in self._page_atoms(url):
                content = self._content(a)
                terms = set(_terms(content))
                overlap = len(q & terms)
                if overlap:
                    score = overlap * (max(a.T, 1.0) / 50.0)
                    hits.append((label, content.strip()[:140], score, a.T))
        hits.sort(key=lambda x: -x[2])
        return hits[:limit]

    # ── 3. STAN PAMIĘCI ────────────────────────────────────────────────────────

    def memory_stats(self) -> Dict[str, Any]:
        pages = self._pages()
        vocab = set()
        n_atoms = 0
        for url in pages:
            for a in self._page_atoms(url):
                n_atoms += 1
                vocab.update(_terms(self._content(a)))
        return {
            "strony": len(pages),
            "atomy": n_atoms,
            "słownik": len(vocab),
            "tryb": "rezonans (HRR)" if self.caps.get("semantic") else "term-overlap",
        }

    # ── 4. EKSPORT DO KARMINQL (skalowanie → karmazyn_db) ─────────────────────

    def to_karminql(self, max_atoms_per_page: int = 20) -> str:
        """
        Zrzuca pamięć jako skrypt KarminQL — każda strona to bąbel,
        każdy znaczący atom to WSTRZYKNIJ. Wynik wykonuje karmazyn_db
        na PhiSpace+HRR, gdzie części wspólne liczą się przez rezonans
        wektorowy (D=2048) zamiast term-overlap.
        """
        lines = ["# Pamięć Lunety → KarmazynDB (KarminQL)",
                 f"# wygenerowano {time.strftime('%Y-%m-%d %H:%M')}", ""]
        for url in self._pages():
            label = "strona_" + re.sub(r"[^a-z0-9]+", "_",
                                       _short_label(url).lower())[:40]
            lines.append(f'UTRWAL "{label}" JAKO BĄBEL')
            atoms = sorted(self._page_atoms(url), key=lambda a: -a.T)
            for a in atoms[:max_atoms_per_page]:
                rola = (a.S or "text").split(":")[0]
                tresc = self._content(a).replace('"', "'").strip()[:80]
                if tresc:
                    lines.append(f'WSTRZYKNIJ "{rola}" -> "{tresc}" DO "{label}"')
            lines.append("")
        return "\n".join(lines)


# ─── Prezentacja ──────────────────────────────────────────────────────────────

_C = {
    'bold': '\033[1m', 'reset': '\033[0m', 'cyan': '\033[96m',
    'yellow': '\033[93m', 'green': '\033[92m', 'gray': '\033[90m',
}


def _present_common(terms, pairs) -> str:
    if not terms and not pairs:
        return ("Brak części wspólnych — odwiedź kilka stron o pokrewnej "
                "tematyce, a Luneta sama znajdzie, co je łączy.")
    out = [f"{_C['bold']}Części wspólne zapisanych stron{_C['reset']}"]
    if pairs:
        out.append(f"\n{_C['cyan']}Strony o tym samym:{_C['reset']}")
        for a, b, jac in pairs:
            out.append(f"  {int(jac*100):3}%  {a}  ~  {b}")
    if terms:
        out.append(f"\n{_C['cyan']}Powracające pojęcia:{_C['reset']}")
        for term, n, total, labels in terms:
            uniq = []
            for l in labels:
                if l not in uniq:
                    uniq.append(l)
            src = ", ".join(uniq[:3]) + (f" +{len(uniq)-3}" if len(uniq) > 3 else "")
            out.append(f"  {_C['yellow']}{term}{_C['reset']} "
                       f"{_C['gray']}({n} stron: {src}){_C['reset']}")
    return "\n".join(out)


def _present_recall(query, hits) -> str:
    if not hits:
        return f"Nic nie rezonuje z '{query}' w zapisanych stronach."
    out = [f"{_C['bold']}Rezonans z '{query}':{_C['reset']}"]
    for label, frag, score, T in hits:
        out.append(f"  {_C['gray']}[{label}]{_C['reset']} {frag}")
    return "\n".join(out)


# ─── Komenda shella ─────────────────────────────────────────────────────────

_RECALL: Optional[Recall] = None


def _get_recall(browser) -> Optional[Recall]:
    global _RECALL
    if not getattr(browser, "_has_dom", False) or not getattr(browser, "dom_mapper", None):
        return None
    if _RECALL is None or _RECALL.dom is not browser.dom_mapper:
        _RECALL = Recall(browser.runtime, browser.dom_mapper)
    return _RECALL


def cmd_recall(args: List[str], browser) -> str:
    r = _get_recall(browser)
    if r is None:
        return "Pamięć niedostępna (brak DOMMappera)."
    sub = (args[0].upper() if args else "MEMORY")

    if sub == "COMMON":
        min_pages = 2
        if len(args) > 1 and args[1].isdigit():
            min_pages = max(2, int(args[1]))
        terms, pairs = r.common(min_pages=min_pages)
        return _present_common(terms, pairs)

    if sub == "RECALL":
        if len(args) < 2:
            return "LUNETA RECALL <zapytanie>"
        return _present_recall(" ".join(args[1:]), r.recall(" ".join(args[1:])))

    if sub in ("MEMORY", "PAMIEC"):
        s = r.memory_stats()
        return (f"Pamięć Lunety: {s['strony']} stron, {s['atomy']} atomów, "
                f"{s['słownik']} pojęć w słowniku. Tryb: {s['tryb']}.\n"
                f"Komendy: LUNETA COMMON | RECALL <q> | EXPORT [plik]")

    if sub == "EXPORT":
        script = r.to_karminql()
        path = args[1] if len(args) > 1 else "luneta_memory.karminql"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(script)
            n = script.count("UTRWAL")
            return (f"Wyeksportowano {n} stron do {path} (KarminQL).\n"
                    f"Wykonaj w karmazyn_db: db.execute(open('{path}').read())")
        except Exception as e:
            return f"Błąd eksportu: {e}"

    return "Opcje: COMMON [min], RECALL <q>, MEMORY, EXPORT [plik]"