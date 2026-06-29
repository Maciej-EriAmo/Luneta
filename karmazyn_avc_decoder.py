"""
karmazyn_avc_decoder.py — Termodynamiczny dekoder AVC (E_video)
================================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Wideo importowane przez PyAV/ffmpeg — świadomie NIE reimplementujemy kompresora
h.xxx (nie przeskakujemy nad nim, importujemy). Rdzeń odpowiada tylko za jedno:
zamienić zdekodowaną luminancję w siatkę atomów substratu.

Model (różni się od pierwotnego szkicu w trzech istotnych punktach):

  • SIATKA PRZESTRZENNA, nie strumień. Każdy makroblok BxB ma JEDEN trwały atom
    o id zależnym od POZYCJI (`avc_mb_{y}_{x}`), nie od numeru klatki. Atom jest
    odtwarzany w miejscu klatka po klatce → liczba atomów jest STAŁA (= liczba
    makrobloków), nie rośnie z czasem. (Pierwotne `avc_{frame}_{y}_{x}` tworzyło
    nowy atom na każdą klatkę — eksplozja substratu i martwa gałąź `has()`.)

  • TEMPERATURA = JASNOŚĆ. T makrobloku ustawiane bezwzględnie ze średniej
    luminancji (0..255 → 0..T_MAX) i przeliczany jest stan FSM. Wideo „pulsuje"
    w mapie cieplnej PhiBuffer — jasne bloki HOT, ciemne COLD.

  • OSIĄGALNOŚĆ jak w E2. Póki wideo gra, atomy siatki są zgłaszane jako
    osiągalne (runtime.set_reach), więc ciemny (zimny) blok nie jest żęty przez
    reach-GC tylko archiwizowany — następna klatka znów go ogrzeje. Po close()
    osiągalność jest zwalniana → siatka stygnie i znika.

Łagodna degradacja: bez PyAV/numpy moduł importuje się, AVAILABLE=False,
a próba użycia rzuca czytelny RuntimeError.
"""

from karmazyn_atom import AtomRegistry, T_MAX

try:
    import av
    import numpy as np
    AVAILABLE = True
except Exception:                       # brak PyAV/ffmpeg lub numpy
    av = None
    np = None
    AVAILABLE = False


class KarminAVCDecoder:
    """Dekoder AVC mapujący makrobloki luminancji na trwałą siatkę atomów.

    registry : AtomRegistry — magazyn atomów (create/get/has).
    runtime  : opcjonalny LunetaRuntime — jeśli ma set_reach(), siatka jest
               zgłaszana jako osiągalna (bez churnu ciemnych bloków). Bez tego
               dekoder dalej działa poprawnie (liczba atomów ograniczona), tylko
               ciemne bloki mogą być żęte i odtwarzane między klatkami.
    block    : bok makrobloku w pikselach (AVC: 16).
    """

    REACH_NAME = "video"

    def __init__(self, registry: AtomRegistry, runtime=None, block: int = 16,
                 mb_prefix: str = "avc_mb"):
        self.reg = registry
        self.runtime = runtime
        self.block = max(1, int(block))
        self._prefix = mb_prefix

        self._container = None
        self._stream = None
        self._grid_ids: set = set()      # id wszystkich atomów-makrobloków
        self.frames_done = 0

    # ── Cykl życia kontenera ─────────────────────────────────────────────────
    def open(self, video_path: str) -> None:
        """Otwiera plik i wybiera pierwszy strumień wideo. Idempotentne względem
        close() (ponowne open zamyka poprzedni)."""
        if not AVAILABLE:
            raise RuntimeError(
                "karmazyn_avc_decoder: brak PyAV/numpy — zainstaluj 'av' i 'numpy'"
            )
        self.close()
        self._container = av.open(video_path)
        if not self._container.streams.video:
            self.close()
            raise RuntimeError(f"brak strumienia wideo w: {video_path}")
        self._stream = self._container.streams.video[0]
        self._decoder = self._container.decode(self._stream)
        self.frames_done = 0

    def decode_next(self):
        """Dekoduje JEDNĄ klatkę i wstrzykuje makrobloki. Tryb krokowy dla pętli
        renderu (pompuj co klatkę zamiast blokować na całym pliku).

        Zwraca indeks klatki (int) albo None przy końcu strumienia / braku open().
        """
        if self._container is None:
            return None
        try:
            frame = next(self._decoder)
        except StopIteration:
            return None
        idx = self.frames_done                  # PyAV 17 nie ma frame.index — liczymy sami
        self._inject_luma(frame)
        self.frames_done += 1
        if self.runtime is not None:
            setter = getattr(self.runtime, "set_reach", None)
            if callable(setter):
                setter(self.REACH_NAME, self._grid_ids)
        return idx

    def process_stream(self, video_path: str, max_frames: int = None) -> int:
        """Wygoda wsadowa: open → pętla decode_next → (NIE zamyka, żeby siatka
        została do obejrzenia; zawołaj close() sam, gdy skończysz). Zwraca liczbę
        przetworzonych klatek. Dla odtwarzania na żywo używaj decode_next()."""
        self.open(video_path)
        n = 0
        while True:
            if max_frames is not None and n >= max_frames:
                break
            if self.decode_next() is None:
                break
            n += 1
        return n

    def close(self) -> None:
        """Zamyka kontener i zwalnia osiągalność siatki (po tym stygnie i znika).
        Same atomy zostawiamy substratowi — reach-GC je zbierze."""
        if self.runtime is not None:
            setter = getattr(self.runtime, "set_reach", None)
            if callable(setter):
                setter(self.REACH_NAME, set())
        if self._container is not None:
            try:
                self._container.close()
            except Exception:
                pass
        self._container = None
        self._stream = None
        self._decoder = None

    # ── Rdzeń: luminancja → makrobloki-atomy ─────────────────────────────────
    def _inject_luma(self, frame) -> None:
        # Luminancja jako czyste 2D (H, W). reformat('gray') omija pułapkę
        # planar yuv420p (gdzie ndarray ma kształt (H*3/2, W), a nie kanały).
        luma = frame.reformat(format="gray").to_ndarray()
        if luma.ndim == 3:                         # niektóre wersje: (H, W, 1)
            luma = luma[:, :, 0]
        h, w = luma.shape
        B = self.block
        scale = T_MAX / 255.0

        for by in range(0, h, B):
            for bx in range(0, w, B):
                block = luma[by:by + B, bx:bx + B]
                T = float(block.mean()) * scale     # 0..T_MAX
                aid = f"{self._prefix}_{by}_{bx}"
                atom = self.reg.get(aid)
                if atom is None:
                    self.reg.create(aid, S="mb", E="luma", T=T)
                    self._grid_ids.add(aid)
                else:
                    # Ustawienie bezwzględne + przeliczenie stanu FSM (samo .T=
                    # nie zaktualizowałoby HOT/WARM/COLD).
                    atom.T = T
                    atom._update_state()

    # ── Wgląd ────────────────────────────────────────────────────────────────
    def active_atom_ids(self) -> set:
        """Id wszystkich atomów-makrobloków bieżącej siatki (do osiągalności)."""
        return set(self._grid_ids)

    def grid_size(self) -> int:
        return len(self._grid_ids)
