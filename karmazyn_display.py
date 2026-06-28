#!/usr/bin/env python3
"""
karmazyn_display.py — KarmazynOS Display v1.1.2
================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Immediate Mode renderer na SDL2/pygame.
v1.1.2: parametryzacja nazwy aplikacji na pasku HUD.
"""

import math
import os
import queue
import threading
import time
import hashlib
import random
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    import pygame
    PYGAME_OK = True
except ImportError:
    PYGAME_OK = False

# ─── Stałe layoutu ───────────────────────────────────────────────────────────
W, H       = 1440, 900
FPS        = 60
FONT_SIZE  = 20

# ─── Kolory ──────────────────────────────────────────────────────────────────
C_BG     = (12,  12,  20)
C_FG     = (255, 255, 255)
C_ACCENT = (180, 60,  60)
C_HOT    = (255, 80,  40)
C_WARM   = (120,  210, 40)
C_COLD   = (40,  80,  140)
C_TURTLE = (100, 220, 100)
C_TRAIL  = (50,  120, 50)
C_GRID   = (20,  20,  35)
C_STATUS = (160, 160, 180)


# ─── Viewport ─────────────────────────────────────────────────────────────────
class Viewport:
    """Opisuje dostępny obszar roboczy z informacją o HUD overlay."""
    def __init__(self, x: int, y: int, w: int, h: int, hud_height: int = 0):
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.hud_height = hud_height
    
    def to_rect(self) -> "pygame.Rect":
        return pygame.Rect(self.x, self.y, self.w, self.h)


# ─── TerminalState ────────────────────────────────────────────────────────────
class TerminalState:
    MAX_LINES = 500

    def __init__(self):
        self.prompt:     str              = "ksh> "
        self.input_buf:  str              = ""
        self._lines:     List[Tuple[str, Tuple[int,int,int]]] = []
        self._lock:      threading.Lock   = threading.Lock()
        self._key_queue: queue.Queue      = queue.Queue()
        self._shutdown:  bool             = False
        self._history:   List[str]        = []
        self._hist_idx:  int              = 0
        self._scroll_offset: int          = 0

    def scroll(self, delta: int) -> None:
        with self._lock:
            self._scroll_offset = max(0, self._scroll_offset + delta)

    def shutdown(self) -> None:
        self._shutdown = True
        self._key_queue.put("")

    def append(self, text: str, color: Tuple[int,int,int] = C_FG) -> None:
        with self._lock:
            for line in str(text).split("\n"):
                self._lines.append((line, color))
            if len(self._lines) > self.MAX_LINES:
                self._lines = self._lines[-self.MAX_LINES:]

    def get_input_blocking(self) -> str:
        self.append(self.prompt, C_ACCENT)
        while not self._shutdown:
            try:
                return self._key_queue.get(timeout=0.1)
            except queue.Empty:
                continue
        return ""

    def push_key(self, event: "pygame.event.Event") -> None:
        if not PYGAME_OK:
            return
        with self._lock:
            if event.key == pygame.K_RETURN:
                line            = self.input_buf
                self.input_buf  = ""
                if line.strip():
                    self._history.append(line)
                    self._hist_idx = len(self._history)
                self._key_queue.put(line)
            elif event.key == pygame.K_BACKSPACE:
                self.input_buf = self.input_buf[:-1]
            elif event.key == pygame.K_DELETE:
                self.input_buf = ''
            elif event.key == pygame.K_UP:
                if self._history and self._hist_idx > 0:
                    self._hist_idx -= 1
                    self.input_buf = self._history[self._hist_idx]
            elif event.key == pygame.K_DOWN:
                if self._history and self._hist_idx < len(self._history) - 1:
                    self._hist_idx += 1
                    self.input_buf = self._history[self._hist_idx]
                else:
                    self._hist_idx = len(self._history)
                    self.input_buf = ""
            elif event.unicode and event.unicode.isprintable():
                self.input_buf += event.unicode

    def snapshot(self) -> Tuple[List[Tuple[str, Tuple]], str]:
        with self._lock:
            return list(self._lines), self.input_buf


# ─── LogoState ────────────────────────────────────────────────────────────────
class LogoState:
    SCALE = 3

    def __init__(self):
        self.x:           float              = 0.0
        self.y:           float              = 0.0
        self.heading:     float              = 0.0
        self.pendown:     bool               = True
        self._canvas:     Optional["pygame.Surface"] = None
        self._w:          int                = 0
        self._h:          int                = 0
        self._seg_queue:  queue.Queue        = queue.Queue()
        self._state_lock: threading.Lock     = threading.Lock()

    def init_canvas(self, w: int, h: int) -> None:
        if not PYGAME_OK:
            return
        self._w      = w
        self._h      = h
        self._canvas = pygame.Surface((w, h))
        self._canvas.fill(C_BG)

    def world_to_px(self, lx: float, ly: float) -> Tuple[int, int]:
        return (int(self._w // 2 + lx * self.SCALE),
                int(self._h // 2 - ly * self.SCALE))

    def add_segment(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self._seg_queue.put((x0, y0, x1, y1))

    def set_turtle(self, x: float, y: float, heading: float, pendown: bool) -> None:
        with self._state_lock:
            self.x       = x
            self.y       = y
            self.heading = heading % 360.0
            self.pendown = pendown

    def clear(self) -> None:
        self._seg_queue.put(("CLEAR",))

    def flush_segments(self) -> int:
        if self._canvas is None:
            return 0
        count = 0
        try:
            while True:
                item = self._seg_queue.get_nowait()
                if item[0] == "CLEAR":
                    self._canvas.fill(C_BG)
                else:
                    x0, y0, x1, y1 = item
                    pygame.draw.line(
                        self._canvas, C_TRAIL,
                        self.world_to_px(x0, y0),
                        self.world_to_px(x1, y1), 1)
                count += 1
        except queue.Empty:
            pass
        return count

    def snapshot(self) -> Tuple[float, float, float, bool, Optional["pygame.Surface"]]:
        with self._state_lock:
            return (self.x, self.y, self.heading, self.pendown, self._canvas)


# ─── DrawCtx ─────────────────────────────────────────────────────────────────
class DrawCtx:
    def __init__(self, surface: "pygame.Surface", font: "pygame.font.Font", rect: "pygame.Rect"):
        self.surface = surface
        self.font    = font
        self.rect    = rect
        self._line_h = font.get_height() + 2

    def text(self, txt: str, color: Tuple[int,int,int] = C_FG,
             x: Optional[int] = None, y: Optional[int] = None) -> "pygame.Rect":
        sx = x if x is not None else self.rect.x
        sy = y if y is not None else self.rect.y
        s  = self.font.render(str(txt), True, color)
        self.surface.blit(s, (sx, sy))
        return s.get_rect(topleft=(sx, sy))

    def line(self, p0: Tuple, p1: Tuple, color: Tuple = C_FG, w: int = 1) -> None:
        pygame.draw.line(self.surface, color, p0, p1, w)

    def box(self, r: "pygame.Rect", fill: Optional[Tuple] = None,
            outline: Optional[Tuple] = None, radius: int = 0) -> None:
        if fill:
            pygame.draw.rect(self.surface, fill, r, 0, radius)
        if outline:
            pygame.draw.rect(self.surface, outline, r, 1, radius)

    def polygon(self, pts: List[Tuple], color: Tuple) -> None:
        pygame.draw.polygon(self.surface, color, pts)

    def clear(self, color: Tuple = C_BG, alpha: int = 220) -> None:
        if alpha >= 255:
            self.surface.fill(color, self.rect)
        else:
            overlay = pygame.Surface((self.rect.w, self.rect.h), pygame.SRCALPHA)
            overlay.fill((*color[:3], alpha))
            self.surface.blit(overlay, self.rect.topleft)


# ─── Czyste funkcje rysowania ─────────────────────────────────────────────────
def _T_to_color(T: float) -> Tuple[int,int,int]:
    t = max(0.0, min(1.0, T / 100.0))
    if t >= 0.7:
        r = int(255 * t)
        g = int(80  * (1 - t))
        return (r, g, 40)
    if t >= 0.3:
        return (60, int(160 * t), 255)
    return (40, 80, int(140 * t + 40))


def draw_terminal(ctx: DrawCtx, state: TerminalState, t: float) -> None:
    r = ctx.rect
    ctx.clear(C_BG, alpha=230)
    ctx.box(r, outline=C_ACCENT)

    line_h  = ctx._line_h
    visible = max(1, (r.h - 8) // line_h - 1)
    lines, input_buf = state.snapshot()

    offset = getattr(state, '_scroll_offset', 0)
    total  = len(lines)
    
    end    = max(0, min(total, total - offset))
    start  = max(0, end - visible)
    view   = lines[start:end]

    y = r.y + 4
    for text, color in view:
        ctx.text(text, color, x=r.x + 6, y=y)
        y += line_h

    cursor  = "|" if int(t * 2) % 2 == 0 else " "
    inp_txt = state.prompt + input_buf + cursor
    ctx.text(inp_txt, (255, 220, 100), x=r.x + 6, y=y)


def draw_logo(ctx: DrawCtx, state: LogoState) -> None:
    r = ctx.rect
    x, y, heading, pendown, canvas = state.snapshot()

    if canvas is not None:
        ctx.surface.blit(canvas, r.topleft)
    else:
        ctx.clear()

    ctx.box(r, outline=C_ACCENT)

    sx, sy = state.world_to_px(x, y)
    sx += r.x - state._w // 2 + r.w // 2
    sy += r.y - state._h // 2 + r.h // 2

    h_r   = math.radians(heading)
    size  = 9
    pts   = [
        (sx + int(size * math.cos(h_r)), sy - int(size * math.sin(h_r))),
        (sx + int(size * 0.45 * math.cos(h_r + 2.4)), sy - int(size * 0.45 * math.sin(h_r + 2.4))),
        (sx + int(size * 0.45 * math.cos(h_r - 2.4)), sy - int(size * 0.45 * math.sin(h_r - 2.4))),
    ]
    ctx.polygon(pts, C_TURTLE)

    if not pendown:
        pygame.draw.circle(ctx.surface, C_ACCENT, (sx, sy), 4, 1)


def draw_phi_map(ctx: DrawCtx, atoms: List[Any], highlight_id: Optional[str] = None) -> None:
    r = ctx.rect
    ctx.surface.fill(C_GRID, r)
    ctx.box(r, outline=(40, 20, 20))

    ctx.text(f"φ-space  {len(atoms)} atomów", C_ACCENT, x=r.x + 6, y=r.y + 4)

    if not atoms:
        ctx.text("brak atomów", C_COLD, x=r.x + 8, y=r.y + 28)
        return

    def _get(a, key, default):
        if isinstance(a, dict):
            return a.get(key, default)
        return getattr(a, key, default)

    visible = [a for a in atoms
               if _get(a, "state", "WARM") != "TOMB"
               and _get(a, "T", 50) >= 2.0]
    visible.sort(key=lambda a: -_get(a, "T", 50))
    visible = visible[:120]

    cols    = max(1, (r.w - 12) // 115)
    cell_w  = (r.w - 12) // cols
    cell_h  = 22
    top     = r.y + 20

    for idx, atom in enumerate(visible):
        col_i = idx % cols
        row_i = idx // cols
        ax    = r.x + 6 + col_i * cell_w
        ay    = top + row_i * (cell_h + 3)

        if ay + cell_h > r.bottom - 4:
            break

        T      = float(_get(atom, "T", 50))
        atom_id = str(_get(atom, "id", "?"))
        color  = _T_to_color(T)
        is_hot = T >= 70.0
        cell   = pygame.Rect(ax, ay, cell_w - 4, cell_h)

        ctx.box(cell, fill=color,
                outline=(min(255, color[0]+60),
                         min(255, color[1]+40),
                         min(255, color[2]+40)) if is_hot else C_BG,
                radius=3)

        if highlight_id and atom_id == highlight_id:
            ctx.box(cell, outline=(255, 255, 100), radius=3)

        label = f"{atom_id[:11]}  {T:4.0f}°"
        text_c = (230, 230, 230) if T < 50 else (20, 20, 20)
        ctx.text(label, text_c, x=ax + 4, y=ay + 7)


def draw_hud(surface: "pygame.Surface", font: "pygame.font.Font",
             stats: Dict[str, Any], t: float, app_name: str = "KarmazynOS") -> None:
    r = pygame.Rect(0, 0, W, 26)
    surface.fill((8, 8, 16), r)

    uptime = f"{t:.0f}s"
    hot    = stats.get("HOT",  0)
    warm   = stats.get("WARM", 0)
    cold   = stats.get("COLD", 0)

    parts = [
        (f" {app_name}  ", C_ACCENT),
        (f"HOT:{hot} ",    C_HOT),
        (f"WARM:{warm} ",  C_WARM),
        (f"COLD:{cold} ",  C_COLD),
        (f"up:{uptime}",   (100, 100, 100)),
    ]
    x = 4
    for text, color in parts:
        s = font.render(text, True, color)
        surface.blit(s, (x, 3))
        x += s.get_width()

    btn_r = pygame.Rect(W - 30, 1, 28, 20)
    pygame.draw.rect(surface, (120, 30, 30), btn_r, 0, 3)
    pygame.draw.rect(surface, C_ACCENT,      btn_r, 1, 3)
    lbl = font.render("×", True, (220, 220, 220))
    surface.blit(lbl, (btn_r.x + (btn_r.w - lbl.get_width()) // 2, btn_r.y + 2))

    pygame.draw.line(surface, C_ACCENT, (0, 25), (W, 25), 1)


# ─── PhiBuffer — mgła termodynamiczna (zoptymalizowana) ─────────────────────
class PhiBuffer:
    _REGIONS = {
        "shell": (0.15, 0.15), "file": (0.85, 0.15), "module": (0.50, 0.15),
        "program": (0.50, 0.50), "bubble": (0.15, 0.80), "run": (0.85, 0.80),
        "cache": (0.50, 0.85), "out": (0.85, 0.80), "code": (0.85, 0.15),
        "nooedit": (0.15, 0.15), "luneta": (0.50, 0.50), "logo": (0.15, 0.50),
    }

    def __init__(self, width: int, height: int):
        self.width   = width
        self.height  = height
        self.surface = pygame.Surface((width, height), pygame.SRCALPHA)
        self.surface.fill((0, 0, 0, 0))
        self._C_HOT  = (255,  50,  50)
        self._C_WARM = (180,  20,  50)
        self._C_COLD = (100, 100, 100)
        self._fade   = pygame.Surface((width, height), pygame.SRCALPHA)
        self._fade.fill((255, 255, 255, 210))
        self._frame_count = 0

    def _project(self, atom) -> tuple:
        S = None
        try:    S = atom["S"]
        except Exception: pass
        if S is None:
            S = getattr(atom, "S", None)

        if S is not None and hasattr(S, "__len__") and not isinstance(S, str):
            try:
                x = int((float(S[0]) + 1.0) / 2.0 * self.width)
                y = int((float(S[1]) + 1.0) / 2.0 * self.height)
                return max(0, min(self.width-1, x)), max(0, min(self.height-1, y))
            except Exception:
                pass

        atom_id = atom.get("id", None) if isinstance(atom, dict) else getattr(atom, "id", None)
        atom_id = str(atom_id or S or "?")
        prefix  = atom_id.split(".")[0]
        cx, cy  = self._REGIONS.get(prefix, (0.50, 0.50))

        h  = int(hashlib.md5(atom_id.encode()).hexdigest(), 16)
        dx = ((h & 0xFF) / 255.0 - 0.5) * 0.25
        dy = (((h >> 8) & 0xFF) / 255.0 - 0.5) * 0.25

        x  = int((cx + dx) * self.width)
        y  = int((cy + dy) * self.height)
        return max(0, min(self.width-1, x)), max(0, min(self.height-1, y))

    def sync_matrix(self, matrix) -> None:
        import math as _math
        self._frame_count += 1
        
        if self._frame_count % 2 == 0:
            self.surface.blit(self._fade, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

        if matrix is None:
            return

        try:
            atoms = matrix.atoms() if callable(matrix.atoms) else matrix.atoms
        except Exception:
            return

        for atom in atoms:
            T = atom.get("T", 0) if isinstance(atom, dict) else getattr(atom, "T", 0)
            T = float(T)
            if T < 10.0:
                continue

            alpha  = min(220, int(T * 2.2))
            atom_id = atom.get("id", "?") if isinstance(atom, dict) else getattr(atom, "id", "?")
            atom_id = str(atom_id)
            phase   = (hash(atom_id) & 0x3F) / 63.0 * 6.28
            pulse   = 1.0 + 0.18 * _math.sin(
                __import__("time").monotonic() * 2.8 + phase)
            radius  = max(2, int(T / 18 * pulse))

            x, y = self._project(atom)

            if T >= 70.0:
                color = (*self._C_HOT,  alpha)
            elif T >= 30.0:
                color = (*self._C_WARM, alpha)
            else:
                random.seed(hash(atom_id) ^ int(T))
                x += random.randint(-2, 2)
                y += random.randint(-2, 2)
                color = (*self._C_COLD, max(40, alpha))

            x = max(0, min(self.width  - 1, x))
            y = max(0, min(self.height - 1, y))

            pygame.draw.circle(self.surface, color, (x, y), radius)

    def get_frame(self) -> pygame.Surface:
        return self.surface


# ─── EditorState ──────────────────────────────────────────────────────────────
class EditorState:
    INDENT = 4

    def __init__(self, label, content, content_type="py"):
        self.label        = label
        self.content_type = content_type
        self.lines        = content.split("\n")
        if not self.lines: self.lines = [""]
        self.cursor_row   = 0
        self.cursor_col   = 0
        self.scroll_top   = 0
        self.modified     = False
        self.status       = "Ctrl+S zapisz | Ctrl+Q wyjdz | F5 uruchom"
        self._key_queue: queue.Queue = queue.Queue()
        self._quit        = False
        self._save        = False
        self._run         = False

    def push_key(self, event):
        self._key_queue.put(event)

    def process_key(self):
        event = self._key_queue.get()
        key   = event.key
        mod   = event.mod
        ctrl  = bool(mod & pygame.KMOD_CTRL)

        if ctrl and key == pygame.K_q:
            self._quit = True; return "quit"
        if ctrl and key == pygame.K_s:
            self._save = True; return "save"
        if key == pygame.K_F5:
            self._run  = True; return "run"

        if key == pygame.K_UP:
            self.cursor_row = max(0, self.cursor_row - 1)
            self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))
        elif key == pygame.K_DOWN:
            self.cursor_row = min(len(self.lines)-1, self.cursor_row + 1)
            self.cursor_col = min(self.cursor_col, len(self.lines[self.cursor_row]))
        elif key == pygame.K_LEFT:
            if self.cursor_col > 0: self.cursor_col -= 1
            elif self.cursor_row > 0:
                self.cursor_row -= 1
                self.cursor_col  = len(self.lines[self.cursor_row])
        elif key == pygame.K_RIGHT:
            line = self.lines[self.cursor_row]
            if self.cursor_col < len(line): self.cursor_col += 1
            elif self.cursor_row < len(self.lines)-1:
                self.cursor_row += 1; self.cursor_col = 0
        elif key == pygame.K_HOME:  self.cursor_col = 0
        elif key == pygame.K_END:   self.cursor_col = len(self.lines[self.cursor_row])
        elif key == pygame.K_PAGEUP:
            self.cursor_row = max(0, self.cursor_row - 20)
        elif key == pygame.K_PAGEDOWN:
            self.cursor_row = min(len(self.lines)-1, self.cursor_row + 20)
        elif key == pygame.K_RETURN:
            line   = self.lines[self.cursor_row]
            indent = len(line) - len(line.lstrip())
            if line.rstrip().endswith(":"): indent += self.INDENT
            rest   = line[self.cursor_col:]
            self.lines[self.cursor_row] = line[:self.cursor_col]
            self.cursor_row += 1
            self.lines.insert(self.cursor_row, " " * indent + rest)
            self.cursor_col  = indent
            self.modified    = True
        elif key == pygame.K_BACKSPACE:
            if self.cursor_col > 0:
                line = self.lines[self.cursor_row]
                self.lines[self.cursor_row] = line[:self.cursor_col-1]+line[self.cursor_col:]
                self.cursor_col -= 1; self.modified = True
            elif self.cursor_row > 0:
                prev = self.lines[self.cursor_row-1]
                cur  = self.lines.pop(self.cursor_row)
                self.cursor_row -= 1; self.cursor_col = len(prev)
                self.lines[self.cursor_row] = prev + cur
                self.modified = True
        elif key == pygame.K_DELETE:
            line = self.lines[self.cursor_row]
            if self.cursor_col < len(line):
                self.lines[self.cursor_row] = line[:self.cursor_col]+line[self.cursor_col+1:]
                self.modified = True
            elif self.cursor_row < len(self.lines)-1:
                nxt = self.lines.pop(self.cursor_row+1)
                self.lines[self.cursor_row] += nxt; self.modified = True
        elif key == pygame.K_TAB:
            line = self.lines[self.cursor_row]
            self.lines[self.cursor_row] = line[:self.cursor_col]+" "*self.INDENT+line[self.cursor_col:]
            self.cursor_col += self.INDENT; self.modified = True
        elif event.unicode and event.unicode.isprintable():
            line = self.lines[self.cursor_row]
            self.lines[self.cursor_row] = line[:self.cursor_col]+event.unicode+line[self.cursor_col:]
            self.cursor_col += 1; self.modified = True

        self._clamp_scroll()
        return "continue"

    def _clamp_scroll(self, visible_lines=40):
        if self.cursor_row < self.scroll_top:
            self.scroll_top = self.cursor_row
        elif self.cursor_row >= self.scroll_top + visible_lines:
            self.scroll_top = self.cursor_row - visible_lines + 1

    def get_text(self): return "\n".join(self.lines)

    def snapshot(self):
        return (list(self.lines), self.cursor_row, self.cursor_col,
                self.scroll_top, self.modified, self.label,
                self.content_type, self.status)


# ─── ImmediateRenderer ────────────────────────────────────────────────────────
class ImmediateRenderer:
    HUD_H = 26

    def __init__(self, screen, font, term_state, logo_state):
        self.screen     = screen
        self.font       = font
        self.term_state = term_state
        self.logo_state = logo_state
        self.phi_ref    = None
        self._phi_atoms: List[Dict] = []
        self._highlight: Optional[str] = None
        self.browser_ref = None
        self._clock      = pygame.time.Clock()
        self._t0         = time.monotonic()
        self._tick_n     = 0
        self.CLOSE_BTN_RECT = pygame.Rect(W - 30, 1, 28, 20)
        self._tick_fn:   Optional[Callable] = None
        self._last_phys  = 0.0
        self._editor:    Optional[Any]  = None
        self._layout:      str              = "solo"
        self._left_draw:   Optional[Callable] = None
        self._left_label:  str              = ""
        self._show_phi:    bool             = False
        self._phi_buf:     Optional[PhiBuffer] = None
        self._panel_handler: Optional[Any] = None
        self._wm_handler: Optional[Any] = None
        self._wm_app_windows: Dict[str, Any] = {}
        self.app_name    = "KarmazynOS"
        self._t = 0.0

    def _make_ctx(self, rect):
        return DrawCtx(self.screen, self.font, rect)

    def _get_atoms(self) -> List[Any]:
        if self.phi_ref is not None:
            return self.phi_ref.matrix.atoms()
        return self._phi_atoms

    def claim_left(self, draw_fn, label="", handler=None):
        if not callable(draw_fn): return
        if self._wm_handler is not None:
            title = label or "Aplikacja"
            wins = getattr(self._wm_handler, "windows", [])
            existing = self._wm_app_windows.get(title)
            if existing is not None and existing in wins:
                existing.draw_fn = draw_fn
                if handler is not None and hasattr(existing, "key_handler"):
                    existing.key_handler = handler
                try: self._wm_handler.focus(existing)
                except Exception: pass
            else:
                try:
                    win = self._wm_handler.open(title, draw_fn, key_handler=handler)
                    self._wm_app_windows[title] = win
                except Exception:
                    self._left_draw = draw_fn; self._left_label = label
                    self._layout = "split"; self._panel_handler = handler
            return
        self._left_draw  = draw_fn
        self._left_label = label
        self._layout     = "split"
        self._panel_handler = handler

    def release_left(self):
        if self._wm_handler is not None and self._wm_app_windows:
            for _title, win in list(self._wm_app_windows.items()):
                try: self._wm_handler.close(win)
                except Exception: pass
            self._wm_app_windows.clear()
        self._left_draw   = None
        self._left_label  = ""
        self._layout      = "solo" if self._wm_handler is None else "wm"
        self._panel_handler = None
        self._editor      = None

    def claim_fullscreen(self, wm) -> None:
        self._wm_handler = wm
        self._layout = "wm"

    def release_fullscreen(self) -> None:
        self._wm_handler = None
        self._layout = "solo"

    def _work_viewport(self) -> Viewport:
        return Viewport(0, self.HUD_H, W, H - self.HUD_H, hud_height=self.HUD_H)

    def set_editor(self, state):
        self._editor = state

    def toggle_phi(self) -> bool:
        self._show_phi = not self._show_phi
        return self._show_phi

    def _phi_stats(self) -> Dict[str, int]:
        if self.phi_ref is not None:
            return self.phi_ref.matrix.stats()
        return {"HOT": 0, "WARM": 0, "COLD": 0, "TOMB": 0}

    def render_frame(self, t: float) -> None:
        self._t = t
        if self._phi_buf is None:
            self._phi_buf = PhiBuffer(W, H)
        if self._tick_fn and t - self._last_phys >= 1.0:
            self._last_phys = t
            try: self._tick_fn()
            except Exception: pass

        s = self.screen
        if self._phi_buf is not None and self.phi_ref is not None:
            self._phi_buf.sync_matrix(self.phi_ref.matrix)
            s.fill(C_BG)
            s.blit(self._phi_buf.get_frame(), (0, 0))
        else:
            s.fill(C_BG)

        draw_hud(s, self.font, self._phi_stats(), t, self.app_name)

        work = self._work_viewport()

        if self._wm_handler is not None:
            self._wm_handler._t = t
            ctx = self._make_ctx(work.to_rect())
            self._wm_handler._draw_all(ctx)
        elif self._layout == "split" and self._left_draw:
            left_w  = W // 2
            right_x = left_w
            right_w = W - left_w
            left_ctx = self._make_ctx(pygame.Rect(0, self.HUD_H, left_w, H - self.HUD_H))
            self._left_draw(left_ctx)

            if self._show_phi and self._get_atoms():
                phi_h  = int((H - self.HUD_H) * 0.25)
                term_y = self.HUD_H + phi_h
                term_h = H - self.HUD_H - phi_h
                draw_phi_map(
                    self._make_ctx(pygame.Rect(right_x, self.HUD_H, right_w, phi_h)),
                    self._get_atoms(), self._highlight)
            else:
                term_y = self.HUD_H
                term_h = H - self.HUD_H
            draw_terminal(
                self._make_ctx(pygame.Rect(right_x, term_y, right_w, term_h)),
                self.term_state, t)
        else:
            if self._show_phi and self._get_atoms():
                phi_h  = int((H - self.HUD_H) * 0.25)
                term_y = self.HUD_H + phi_h
                term_h = H - self.HUD_H - phi_h
                draw_phi_map(
                    self._make_ctx(pygame.Rect(0, self.HUD_H, W, phi_h)),
                    self._get_atoms(), self._highlight)
            else:
                term_y = self.HUD_H
                term_h = H - self.HUD_H
            draw_terminal(
                self._make_ctx(pygame.Rect(0, term_y, W, term_h)),
                self.term_state, t)

        pygame.display.flip()

    def _handle_event(self, event) -> bool:
        if event.type == pygame.QUIT:
            return False

        if self._wm_handler is not None:
            if event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION):
                consumed = self._wm_handler.on_mouse(event, self._work_viewport().to_rect())
                if consumed:
                    return True

        if self._panel_handler is not None and self._layout == "split":
            if event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION):
                panel_rect = pygame.Rect(0, self.HUD_H, W // 2, H - self.HUD_H)
                consumed = self._panel_handler.on_mouse(event, panel_rect)
                if consumed:
                    return True

        if event.type == pygame.KEYDOWN:
            # ESC nie zamyka juz aplikacji — przechodzi dalej do _wm_handler
            # (np. GraphicPageViewer.on_key), ktory robi powrot do konsoli
            # (close_callback -> release_fullscreen). Wyjscie z aplikacji:
            # Ctrl+Q albo czerwony przycisk X na HUD.
            ctrl = event.mod & pygame.KMOD_CTRL
            if ctrl and event.key == pygame.K_q and not self._editor:
                return False
            # F1 (zamknij-panel) oraz F2 (phi-map) wylaczone na zyczenie.

            if self._wm_handler is not None and self._wm_handler.wants_keys():
                self._wm_handler.on_key(event)
                return True

            if self._panel_handler is not None and self._panel_handler.wants_keys():
                self._panel_handler.on_key(event)
                return True

            if self._editor is not None:
                self._editor.push_key(event)
                return True

            self.term_state.push_key(event)
            return True

        if event.type == pygame.MOUSEBUTTONDOWN:
            if self.CLOSE_BTN_RECT.collidepoint(event.pos):
                return False
            if event.button == 1:
                self._handle_click(event.pos)
                self._try_click_link(event.pos)
            elif event.button == 3:
                self._handle_right_click(event.pos)
            elif event.button == 4:
                self.term_state.scroll(-3)
            elif event.button == 5:
                self.term_state.scroll(3)
            return True
        return True

    def _handle_click(self, pos):
        mx, my = pos
        show_phi = self._show_phi
        layout   = self._layout
        if not show_phi:
            return
        available_h = H - self.HUD_H
        phi_h   = int(available_h * 0.25)
        right_x = W//2 if layout == "split" else 0
        phi_r   = pygame.Rect(right_x, self.HUD_H, W - right_x, phi_h)
        if not phi_r.collidepoint(mx, my):
            return

        atoms  = self._get_atoms()
        visible = [a for a in atoms
                   if getattr(a, "state", a.get("state","") if isinstance(a,dict) else "") != "TOMB"]
        visible.sort(key=lambda a: -(getattr(a,"T",0) if not isinstance(a,dict) else a.get("T",0)))
        visible = visible[:120]

        cols   = max(1, (phi_r.w - 12) // 115)
        cell_w = (phi_r.w - 12) // cols
        cell_h = 30
        top    = phi_r.y + 22

        for idx, atom in enumerate(visible):
            col_i = idx % cols
            row_i = idx // cols
            ax    = phi_r.x + 6 + col_i * cell_w
            ay    = top + row_i * (cell_h + 3)
            if ay + cell_h > phi_r.bottom - 4:
                break
            cell = pygame.Rect(ax, ay, cell_w - 4, cell_h)
            if cell.collidepoint(mx, my):
                aid = str(getattr(atom,"id", atom.get("id","?") if isinstance(atom,dict) else "?"))
                T   = float(getattr(atom,"T", atom.get("T",50) if isinstance(atom,dict) else 50))
                st  = str(getattr(atom,"state", atom.get("state","?") if isinstance(atom,dict) else "?"))
                self._highlight = aid
                self.term_state.append(f"φ {aid}  T={T:.1f}  {st}", C_ACCENT)
                return

    def _try_click_link(self, pos):
        if not self.browser_ref or not getattr(self.browser_ref, '_current', False):
            return
        layout    = self._layout
        show_phi  = self._show_phi
        available_h = H - self.HUD_H
        phi_h   = int(available_h * 0.25) if show_phi else 0
        right_x = W//2 if layout == "split" else 0
        right_w = W - right_x
        term_y  = self.HUD_H + phi_h
        term_h  = available_h - phi_h
        term_r  = pygame.Rect(right_x, term_y, right_w, term_h)
        if not term_r.collidepoint(pos):
            return
        line_h  = self.font.get_height() + 2
        rel_y   = pos[1] - term_y
        line_idx = rel_y // line_h
        lines, _ = self.term_state.snapshot()
        visible_start = max(0, len(lines) - (term_h // line_h) - 1)
        abs_idx = visible_start + line_idx
        if abs_idx >= len(lines):
            return
        line_text, _ = lines[abs_idx]
        import re
        m = re.search(r'\[(\d+)\]', line_text)
        if m:
            n = int(m.group(1))
            _, msg = self.browser_ref.follow_link(n)
            self.term_state.append(msg)

    def _handle_right_click(self, pos):
        W2 = self.screen.get_width() // 2
        is_left_panel = (
            self._layout == "split"
            and self._left_draw is not None
            and pos[0] < W2
        )
        if is_left_panel:
            label = self._left_label or "panel"
            self.term_state.append(
                f"Panel lewy [{label}]: F1=zamknij  Ctrl+S=zapisz  Ctrl+Q=wyjdz",
                (160, 200, 255))
        else:
            self.term_state.append(
                "Skroty: F1=panel  F2=phi-map  j/k=scroll  b=wstecz (Luneta)",
                (160, 200, 255))

    def run(self,
            shell_main: Optional[Callable] = None,
            on_quit:    Optional[Callable] = None) -> None:
        if shell_main:
            t = threading.Thread(
                target=shell_main,
                args=(self.term_state,),
                daemon=True,
                name="karmazyn-shell",
            )
            t.start()

        running = True
        try:
            while running:
                t = time.monotonic() - self._t0
                for event in pygame.event.get():
                    if not self._handle_event(event):
                        running = False
                        break
                if running:
                    self.render_frame(t)
                try:
                    self._clock.tick(FPS)
                except KeyboardInterrupt:
                    running = False
        finally:
            self.term_state.shutdown()
            pygame.quit()
            if on_quit:
                on_quit()


# ─── KarmazynDisplay — fasada ─────────────────────────────────────────────────
class KarmazynDisplay:
    def __init__(self):
        self.available:  bool                        = False
        self.term_state: TerminalState               = TerminalState()
        self.logo_state: LogoState                   = LogoState()
        self._renderer:  Optional[ImmediateRenderer] = None
        self._font:      Optional[Any]               = None
        self._screen:    Optional[Any]               = None

    def init(self, w: int = W, h: int = H, title: str = "KarmazynOS", fullscreen: bool = False) -> bool:
        if getattr(self, "available", False): return True
        if not PYGAME_OK:
            return False
        try:
            pygame.init()
            flags  = pygame.FULLSCREEN if fullscreen else pygame.NOFRAME
            screen = pygame.display.set_mode((w, h), flags)
            pygame.display.set_caption(title)

            try:
                font = pygame.font.SysFont("monospace", FONT_SIZE, bold=True)
            except Exception:
                font = pygame.font.Font(None, FONT_SIZE)

            self.logo_state.init_canvas(w // 2, h)
            self._screen   = screen
            self._font     = font
            self._renderer = ImmediateRenderer(screen, font, self.term_state, self.logo_state)
            self._renderer.app_name = title
            self.available = True
            return True
        except Exception:
            self.available = False
            return False

    def bind_phi(self, phi_space: Any) -> None:
        if self._renderer:
            self._renderer.phi_ref = phi_space
            if phi_space is not None and hasattr(phi_space, 'tick'):
                self._renderer._tick_fn = phi_space.tick

    def set_demo_atoms(self, atoms: List[Dict]) -> None:
        if self._renderer:
            self._renderer._phi_atoms = atoms

    def bind_browser(self, browser: Any) -> None:
        if self._renderer:
            self._renderer.browser_ref = browser

    def run(self,
            shell_main: Optional[Callable] = None,
            on_quit:    Optional[Callable] = None) -> None:
        if not self.available or self._renderer is None:
            if shell_main:
                shell_main(self.term_state)
            return
        self._renderer.run(shell_main, on_quit)

    @property
    def renderer(self) -> Optional[ImmediateRenderer]:
        return self._renderer