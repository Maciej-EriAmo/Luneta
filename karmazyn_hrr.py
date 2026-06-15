"""
karmazyn_hrr.py — HRR Operations KarmazynOS v2.0
=================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Czyste operacje Holographic Reduced Representation.
Żadnego modelu atomu — to należy do karmazyn_atom.py.
Żadnych bąbli — to należy do karmazyn_phi.py.

Tylko matematyka:
  bind(a, b)      — splot kołowy (wiązanie)
  unbind(s, a)    — korelacja kołowa (odwiązanie)
  bundle(*vecs)   — superpozycja (suma)
  similarity(a,b) — cosine similarity (rezonans)

Oraz:
  HRROperations   — stateful wrapper z rejestrem wektorów atomów

Właściwości matematyczne (D=2048):
  E[sim(unbind(bind(a,b)), b)] ≈ 0.707 (granica 1/√2)
  Kapacyt bundle przy sim>0.5  ≈ D/8
  Separacja dla N=20 elementów ≈ 10σ (niezawodny retrieval)

Użycie:
    from karmazyn_hrr import bind, unbind, bundle, similarity

    a = random_unit_vector(2048)
    b = random_unit_vector(2048)
    s = bind(a, b)             # asocjacja
    r = unbind(s, a)           # retrieval
    print(similarity(r, b))   # ≈ 0.707
"""

import hashlib
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from numpy.fft import fft, ifft


# ─── Czyste operacje ─────────────────────────────────────────────────────────

def bind(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """
    Splot kołowy: IFFT(FFT(a) * FFT(b))
    Komutywny, asocjatywny.
    unbind(bind(a,b), a) ≈ b  z sim ≈ 0.707
    """
    return np.real(ifft(fft(a) * fft(b)))


def unbind(bundle_vec: np.ndarray, key: np.ndarray) -> np.ndarray:
    """
    Korelacja kołowa: IFFT(FFT(s) * conj(FFT(key)))
    Odwrotność bind: unbind(bind(a,b), a) ≈ b
    """
    return np.real(ifft(fft(bundle_vec) * np.conj(fft(key))))


def bundle(*vecs: np.ndarray) -> np.ndarray:
    """
    Superpozycja: Σ vecs
    Przechowuje N asocjacji w jednym wektorze.
    Sygnał dla każdej ≈ 1/√N (maleje z liczbą elementów).
    """
    if not vecs:
        raise ValueError("bundle() wymaga co najmniej jednego wektora")
    return sum(vecs)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    Cosine similarity — miara rezonansu.
    1.0 = identyczne, 0.0 = ortogonalne, -1.0 = antyfazowe.
    """
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na < 1e-10 or nb < 1e-10:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def normalize(v: np.ndarray) -> np.ndarray:
    """Normalizacja do wektora jednostkowego."""
    n = np.linalg.norm(v)
    return v / n if n > 1e-10 else v


def random_unit_vector(D: int, seed: Optional[int] = None) -> np.ndarray:
    """Losowy wektor jednostkowy w przestrzeni D-wymiarowej."""
    rng = np.random.RandomState(seed)
    v   = rng.randn(D)
    return normalize(v)


def name_to_vector(name: str, D: int = 2048) -> np.ndarray:
    """
    Deterministyczny wektor jednostkowy dla danej nazwy.
    Hash nazwy → seed → losowy wektor.
    Gwarancja: ten sam string zawsze daje ten sam wektor.
    """
    h   = int(hashlib.sha256(name.encode()).hexdigest(), 16) % (2**32)
    rng = np.random.RandomState(h)
    v   = rng.randn(D)
    return normalize(v)


# ─── HRROperations — stateful wrapper ────────────────────────────────────────

class HRROperations:
    """
    Wrapper z rejestrem wektorów i nearest-neighbor lookup.
    Używany przez PhiSpace.enable_hrr() i KarmazynJSPhi.

    Nie przechowuje atomów ani wartości — tylko wektory.
    Model atomu należy do karmazyn_atom.py.
    """

    def __init__(self, D: int = 2048):
        self.D = D
        self._vectors: Dict[str, np.ndarray] = {}  # name → vector
        # Wektor permutacji dla sekwencji
        self._P: Optional[np.ndarray] = None

    def atom_vector(self, name: str) -> np.ndarray:
        """Pobierz lub wygeneruj deterministyczny wektor dla nazwy."""
        if name not in self._vectors:
            self._vectors[name] = name_to_vector(name, self.D)
        return self._vectors[name]

    def register(self, name: str, vector: np.ndarray) -> None:
        """Zarejestruj zewnętrzny wektor pod nazwą."""
        self._vectors[name] = normalize(vector)

    def nearest(self, vec: np.ndarray,
                k: int = 5,
                threshold: float = 0.1
                ) -> List[Tuple[float, str]]:
        """
        Znajdź k najbliższych zarejestrowanych nazw.
        Zwraca [(similarity, name), ...] malejąco.
        """
        vn = np.linalg.norm(vec)
        if vn < 1e-10 or not self._vectors:
            return []

        results = []
        for name, v in self._vectors.items():
            s = float(np.dot(vec, v) / (vn * np.linalg.norm(v) + 1e-10))
            if s >= threshold:
                results.append((s, name))

        results.sort(key=lambda x: -x[0])
        return results[:k]

    def bind(self, a: np.ndarray, b: np.ndarray) -> np.ndarray:
        return bind(a, b)

    def unbind(self, bundle_vec: np.ndarray, key: np.ndarray) -> np.ndarray:
        return unbind(bundle_vec, key)

    def similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        return similarity(a, b)

    # ── Sekwencje (dla hologramów) ────────────────────────────────────────────

    @property
    def P(self) -> np.ndarray:
        """Wektor permutacji dla kodowania pozycji w sekwencji."""
        if self._P is None:
            self._P = name_to_vector("__position_vector__", self.D)
        return self._P

    def P_power(self, n: int) -> np.ndarray:
        """P^n = P ⊗ P ⊗ ... (n razy). P^0 = delta."""
        if n == 0:
            v = np.zeros(self.D); v[0] = 1.0
            return v
        result = self.P
        for _ in range(n - 1):
            result = bind(result, self.P)
        return result

    def encode_sequence(self, items: List[str]) -> np.ndarray:
        """
        Koduje sekwencję nazw jako wektor HRR.
        Pozycja i = bind(P^i, atom_vector(item_i))
        """
        if not items:
            return np.zeros(self.D)
        return bundle(*[
            bind(self.P_power(i), self.atom_vector(item))
            for i, item in enumerate(items)
        ])

    def decode_position(self, seq_vec: np.ndarray,
                        position: int,
                        threshold: float = 0.1) -> Optional[str]:
        """Wyciągnij element na pozycji z zakodowanej sekwencji."""
        result = unbind(seq_vec, self.P_power(position))
        hits   = self.nearest(result, k=1, threshold=threshold)
        return hits[0][1] if hits else None

    def stats(self) -> Dict[str, Any]:
        return {"D": self.D, "registered": len(self._vectors)}
