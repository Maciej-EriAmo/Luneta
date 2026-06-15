"""
luneta_text.py — Wspólne narzędzia tekstowe Lunety v1.0
========================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Jedno źródło prawdy dla dekodowania odpowiedzi HTTP i oceny czytelności
tekstu. Używane przez karmazyn_browser (dekodowanie strony) ORAZ
karmazyn_dom (filtrowanie atomów).

Dwa problemy które rozwiązuje:

  1. Strony nie-UTF-8 / źle skompresowane (onet.pl).
     decode_body() — dekompresja (gzip/deflate/br) + wykrycie charsetu
     z nagłówka i <meta>, z polskimi fallbackami (cp1250, iso-8859-2).
     Zwraca (text, ok) — ok=False sygnalizuje że dekodowanie zawiodło,
     więc warstwa wyżej NIE mapuje śmiecia do phi-space.

  2. DOMMapper odrzucający czytelną treść (Premium, Gdańsk, 21°C, CAIQ: 5).
     is_readable_text() — odrzuca TYLKO prawdziwy śmieć (znaki kontrolne,
     replacement chars z błędu dekodowania, surogaty), a akceptuje krótkie
     słowa, polskie diakrytyki, symbole (°, %, :) i cyfry.

Zasada: czytelność ≠ długość ani ≠ brak symboli. 'Menu', '19', '21°C'
i 'Gdańsk' to poprawna treść. Śmieć to bajty których nie dało się
zdekodować — wykrywalne po replacement char (\\ufffd) i znakach kontrolnych.
"""

import gzip
import re
import unicodedata
import zlib
from typing import Dict, Optional, Tuple

try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

# Polskie kodowania legacy — kolejność fallbacków po utf-8
_FALLBACK_CHARSETS = ["utf-8", "cp1250", "iso-8859-2", "windows-1250"]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_META_CHARSET_RE = re.compile(rb'<meta[^>]+charset=["\']?([\w\-]+)', re.I)
_META_CONTENT_RE = re.compile(rb'content=["\'][^"\']*charset=([\w\-]+)', re.I)


# ─── ANSI ──────────────────────────────────────────────────────────────────────

def strip_ansi(text: str) -> str:
    """Usuwa sekwencje ANSI (kolory) z tekstu."""
    return _ANSI_RE.sub("", text)


# ─── Wykrywanie charsetu ────────────────────────────────────────────────────────

def _charset_from_headers(headers: Optional[Dict]) -> str:
    if not headers:
        return ""
    for k, v in headers.items():
        if str(k).lower() == "content-type":
            m = re.search(r"charset=([\w\-]+)", str(v).lower())
            return m.group(1) if m else ""
    return ""


def _charset_from_meta(raw: bytes) -> str:
    """Czyta <meta charset> z pierwszych 4 KB (jeszcze nie zdekodowanych)."""
    head = raw[:4096]
    m = _META_CHARSET_RE.search(head)
    if m:
        return m.group(1).decode("ascii", errors="ignore").lower()
    m = _META_CONTENT_RE.search(head)
    if m:
        return m.group(1).decode("ascii", errors="ignore").lower()
    return ""


# ─── Dekompresja ────────────────────────────────────────────────────────────────

def _decompress(body: bytes, content_encoding: str) -> Tuple[bytes, Optional[str]]:
    """
    Dekompresuje body wg Content-Encoding.
    Zwraca (body, error). error != None = nie da się zdekompresować
    (np. brotli bez biblioteki).
    """
    encodings = [e.strip() for e in content_encoding.lower().split(",") if e.strip()]
    for enc in encodings:
        if enc in ("identity", ""):
            continue
        if enc == "gzip":
            try:
                body = gzip.decompress(body)
            except Exception:
                pass  # może body nie jest faktycznie gzip — próbujemy dalej decode
        elif enc == "deflate":
            try:
                body = zlib.decompress(body)
            except zlib.error:
                try:
                    body = zlib.decompress(body, -zlib.MAX_WBITS)  # raw deflate
                except Exception:
                    pass
            except Exception:
                pass
        elif enc == "br":
            if not HAS_BROTLI:
                return body, "brotli"
            try:
                body = brotli.decompress(body)
            except Exception:
                pass
    return body, None


# ─── Dekodowanie ────────────────────────────────────────────────────────────────

def decode_body(body: bytes,
                headers: Optional[Dict] = None,
                replacement_threshold: float = 0.05) -> Tuple[str, bool]:
    """
    Dekompresja + dekodowanie odpowiedzi HTTP do tekstu.

    Zwraca (text, ok):
      ok=True  — zdekodowano czysto (mało replacement chars)
      ok=False — dekodowanie zawiodło; text to komunikat/best-effort.
                 Warstwa wyżej powinna NIE mapować tego do phi-space.

    Kolejność charsetów: nagłówek Content-Type → <meta charset> →
    utf-8 → cp1250 → iso-8859-2 (polskie legacy).
    """
    headers = headers or {}
    ce = ""
    for k, v in headers.items():
        if str(k).lower() == "content-encoding":
            ce = str(v)
            break

    body, decomp_err = _decompress(body, ce)
    if decomp_err == "brotli":
        return ("[Luneta] Serwer użył kodowania brotli (br), "
                "ale biblioteka brotli nie jest zainstalowana. "
                "Zainstaluj: pip install brotli", False)

    # Lista kandydatów na charset
    candidates = []
    declared = _charset_from_headers(headers) or _charset_from_meta(body)
    if declared:
        candidates.append(declared)
    for c in _FALLBACK_CHARSETS:
        if c not in candidates:
            candidates.append(c)

    for enc in candidates:
        try:
            text = body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # Akceptuj jeśli mało replacement chars w próbce
        sample = text[:1000]
        if sample:
            ratio = sample.count("\ufffd") / len(sample)
            if ratio < replacement_threshold:
                return text, True

    # Ostatnia deska ratunku — utf-8 z podmianą; ok=False sygnalizuje problem
    return body.decode("utf-8", errors="replace"), False


# ─── Ocena czytelności ────────────────────────────────────────────────────────

def is_readable_text(text: str, max_bad_ratio: float = 0.15) -> bool:
    """
    Czy tekst nadaje się jako treść atomu phi-space?

    Akceptuje: krótkie słowa ('Menu'), cyfry ('19'), symbole ('21°C', '%'),
    polskie diakrytyki ('Gdańsk', 'Łódź'), interpunkcję ('CAIQ: 5').

    Odrzuca TYLKO prawdziwy śmieć: znaki kontrolne C0 (poza \\t\\n\\r),
    replacement chars (\\ufffd z błędu dekodowania), surogaty, znaki
    nieprzypisane/prywatne. Próg: >15% złych znaków = śmieć.

    NIE odrzuca po długości ani po obecności symboli — to były błędne
    heurystyki które ucinały poprawną treść menu/pogody.
    """
    if not text:
        return False
    text = strip_ansi(text).strip()
    if not text:
        return False

    bad = 0
    for ch in text:
        o = ord(ch)
        if ch == "\ufffd":                                  # replacement char
            bad += 1
        elif o < 0x20 and ch not in "\t\n\r":               # C0 control
            bad += 1
        elif unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cn", "Cs"):
            bad += 1                                          # control/unassigned/surrogate

    return (bad / len(text)) < max_bad_ratio


def looks_like_decode_error(text: str) -> bool:
    """
    Wykrywa czy 'treść strony' to w istocie komunikat o błędzie dekodowania
    (z browsera). Pozwala DOMMapperowi pominąć mapowanie takiej strony.
    """
    if not text:
        return False
    sample = strip_ansi(text)[:200]
    markers = (
        "Nie udało się zdekodować",
        "[Luneta] Serwer użył kodowania brotli",
        "prawdopodobnie binarna lub uszkodzona",
    )
    return any(m in sample for m in markers)


# ─── Self-test ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("luneta_text.py — testy")
    print("=" * 60)

    passed = failed = 0
    def chk(name, ok, detail=""):
        global passed, failed
        print(f"  {'OK ' if ok else 'XX '} {name}")
        if detail and not ok: print(f"       {detail}")
        if ok: passed += 1
        else:  failed += 1

    # ── is_readable_text ───────────────────────────────────────────────────────
    print("\n[1] is_readable_text — czytelna treść z logu")
    valid = ["wp.pl","Premium","Wyniki","Quizy","Radio","Poczta","Menu",
             "Zaloguj","MENU","Pogoda","Świat","CAIQ: 5","12:59","19",
             "21°C","Gdańsk","Łódź","Kraków","Wrocław","."]
    n_ok = sum(is_readable_text(s) for s in valid)
    chk(f"akceptuje czytelne ({n_ok}/{len(valid)})", n_ok == len(valid))

    print("\n[2] is_readable_text — odrzuca śmieć")
    garbage = ["\x1b[91mBłąd: " + "\ufffd"*20, "\x00\x01\x02\x03", "\ufffd"*10]
    n_rej = sum(not is_readable_text(s) for s in garbage)
    chk(f"odrzuca śmieć ({n_rej}/{len(garbage)})", n_rej == len(garbage))

    # ── decode_body ────────────────────────────────────────────────────────────
    print("\n[3] decode_body — gzip + UTF-8 polski")
    html = "<html><body>Gdańsk Łódź 21°C</body></html>".encode("utf-8")
    t, ok = decode_body(gzip.compress(html),
                        {"content-encoding":"gzip","content-type":"text/html; charset=utf-8"})
    chk("gzip+utf8", ok and "Gdańsk" in t)

    print("\n[4] decode_body — cp1250 legacy bez deklaracji")
    t, ok = decode_body("Zażółć gęślą".encode("cp1250"), {"content-type":"text/html"})
    chk("cp1250 fallback", "Zażółć" in t)

    print("\n[5] decode_body — <meta charset>")
    html = '<meta charset="utf-8">Świętokrzyskie'.encode("utf-8")
    t, ok = decode_body(html, {})
    chk("meta charset", "Świętokrzyskie" in t)

    print("\n[6] decode_body — brotli bez biblioteki")
    t, ok = decode_body(b"fake", {"content-encoding":"br"})
    if HAS_BROTLI:
        chk("brotli dostępne (skip)", True)
    else:
        chk("brotli brak → czytelny komunikat", (not ok) and "brotli" in t.lower())

    print("\n[7] looks_like_decode_error")
    err = "\x1b[91mBłąd: Nie udało się zdekodować odpowiedzi jako tekst."
    chk("wykrywa komunikat błędu", looks_like_decode_error(err))
    chk("nie myli z normalną treścią", not looks_like_decode_error("Gdańsk 21°C"))

    print(f"\n{'=' * 60}")
    print(f"Wyniki: {passed}/{passed + failed}")
    print("PASS" if failed == 0 else f"FAIL — {failed}")
    print(f"brotli: {'dostępne' if HAS_BROTLI else 'BRAK (pip install brotli)'}")
    print("=" * 60)
