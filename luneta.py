"""
luneta.py — Punkt wejścia Windows CLI dla Lunety v2.2
======================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Wersja 2.2: Automatyczny start w trybie płótna graficznego. 
Przeglądarka natychmiast przejmuje ekran, a terminal REPL 
pozostaje w tle jako fallback pod klawiszem ESC.
"""

import os
import sys
import re
import urllib.parse

if os.name == 'nt':
    os.system("")

try:
    import pygame
except ImportError:
    pass

try:
    from luneta_runtime import LunetaRuntime
    from karmazyn_browser import LunetaBrowser, NodeType
    from karmazyn_dom import attach_to_browser, cmd_dom
    from karmazyn_display import KarmazynDisplay, DrawCtx, PYGAME_OK
except ImportError as e:
    print(f"Błąd importu środowiska KarmazynOS: {e}")
    sys.exit(1)

try:
    from luneta_async_bridge import install_async_engine_on_browser, cmd_async
    _HAS_ASYNC = True
except ImportError:
    _HAS_ASYNC = False
    def cmd_async(args, bridge):
        return None

ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
def strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', str(text))


# ─── 1. STRUKTURY DANYCH (Visual Box Tree) ──────────────────────────────────
class LayoutBox:
    __slots__ = ['rect', 'text', 'color', 'node', 'box_type']
    def __init__(self, rect, text, color, node=None, box_type="text"):
        self.rect = rect
        self.text = text
        self.color = color
        self.node = node
        self.box_type = box_type


# ─── 2. SILNIK UKŁADU (Iteracyjny DFS z przepływem Inline) ──────────────────
class LayoutEngine:
    def __init__(self, max_w: int, char_w: int, line_h: int):
        self.max_w = max_w
        self.char_w = char_w
        self.line_h = line_h

    def build(self, root_node, start_x: int, start_y: int):
        boxes = []
        stack = [(root_node, 0, False)]
        current_y = start_y
        current_x = start_x

        def flush_line(indent):
            nonlocal current_x, current_y
            if current_x > start_x + indent:
                current_x = start_x + indent
                current_y += self.line_h

        def add_text_chunk(text, color, node, btype, indent):
            nonlocal current_x, current_y
            words = text.replace('\n', ' ').split(' ')
            for w in words:
                if not w:
                    continue
                w_len = len(w) * self.char_w
                # Łamanie wiersza, gdy słowo nie mieści się w dokumencie
                if current_x + w_len > start_x + self.max_w - indent and current_x > start_x + indent:
                    flush_line(indent)
                r = pygame.Rect(current_x, current_y, w_len, self.line_h)
                boxes.append(LayoutBox(r, w, color, node, btype))
                current_x += w_len + self.char_w

        while stack:
            node, indent, is_post = stack.pop()
            
            is_block = node.typ in (NodeType.BLOCK, NodeType.LIST, NodeType.PRE, NodeType.TABLE, NodeType.HR)
            
            # Post-processing bloków
            if is_post:
                if is_block:
                    flush_line(indent)
                    current_y += 6
                continue
                
            # Pre-processing bloków
            if is_block:
                flush_line(indent)
                
            typ = node.typ
            
            if typ == NodeType.TEXT:
                text = (node.text or "").strip()
                if text:
                    add_text_chunk(text, (230, 230, 230), node, "text", indent)
                    
            elif typ == NodeType.HEADING:
                flush_line(indent)
                current_y += self.line_h // 2
                text = node.get_plain_text().strip()
                if text:
                    add_text_chunk(text, (255, 195, 90), node, "heading", indent)
                flush_line(indent)
                current_y += self.line_h // 4
                
            elif typ == NodeType.LINK:
                text = node.get_plain_text().strip()
                if text:
                    add_text_chunk(text, (90, 165, 255), node, "link", indent)
                    
            elif typ == NodeType.HR:
                flush_line(indent)
                current_y += self.line_h // 2
                r = pygame.Rect(start_x + indent, current_y, self.max_w, 2)
                boxes.append(LayoutBox(r, "", (80, 80, 95), node, "hr"))
                current_y += self.line_h // 2
                
            next_indent = indent + (20 if typ == NodeType.LIST else 0)
            
            stack.append((node, indent, True))
            for child in reversed(node.children):
                if typ not in (NodeType.LINK, NodeType.HEADING):
                    stack.append((child, next_indent, False))
                    
        flush_line(0)
        return boxes, current_y


# ─── 3. RENDERER DOM Z CACHE FONTÓW I CULLINGIEM ───────────────────────────
class DOMRenderer:
    def __init__(self, font):
        self.font = font
        self.font_cache = {}

    def _get_text_surface(self, text: str, color: tuple):
        key = (text, color)
        if key not in self.font_cache:
            self.font_cache[key] = self.font.render(text, True, color)
        return self.font_cache[key]

    def draw(self, ctx: DrawCtx, boxes: list, scroll_y: int, clip_rect):
        import pygame
        old_clip = ctx.surface.get_clip()
        ctx.surface.set_clip(clip_rect)
        
        for box in boxes:
            box_y = box.rect.y - scroll_y
            
            if box_y + box.rect.h < clip_rect.y:
                continue
            if box_y > clip_rect.bottom:
                break
                
            if box.box_type in ("text", "heading", "link"):
                surf = self._get_text_surface(box.text, box.color)
                ctx.surface.blit(surf, (box.rect.x, box_y))
                
                if box.box_type == "link":
                    pygame.draw.line(
                        ctx.surface, box.color,
                        (box.rect.x, box_y + self.font.get_height()),
                        (box.rect.x + surf.get_width(), box_y + self.font.get_height())
                    )

            elif box.box_type == "hr":
                pygame.draw.line(
                    ctx.surface, box.color, 
                    (box.rect.x, box_y), 
                    (box.rect.x + box.rect.w, box_y)
                )
                                 
        ctx.surface.set_clip(old_clip)


# ─── 4. ADAPTER WIDOKU (GraphicPageViewer) ──────────────────────────────────
class GraphicPageViewer:
    def __init__(self, browser, close_callback):
        self.browser = browser
        self.close_callback = close_callback
        
        self.scroll_y = 0
        self.max_scroll = 0
        self.nav_h = 45
        
        self.layout_boxes = []
        self.last_page_url = None
        self.last_dom_version = -1
        self.dom_renderer = None
        
        self.url_active = False
        self.url_text = ""
        self.btn_back_rect = None
        self.input_url_rect = None

    def wants_keys(self):
        return True

    def on_key(self, event):
        import pygame
        if self.url_active:
            if event.key == pygame.K_RETURN:
                if self.url_text.strip():
                    self.browser.go(self.url_text.strip())
                self.url_active = False
            elif event.key == pygame.K_BACKSPACE:
                self.url_text = self.url_text[:-1]
            elif event.key == pygame.K_ESCAPE:
                self.url_active = False
            elif event.unicode and event.unicode.isprintable():
                self.url_text += event.unicode
            return True

        if event.key == pygame.K_UP:
            self.scroll_y = max(0, self.scroll_y - 40)
        elif event.key == pygame.K_DOWN:
            self.scroll_y = min(self.max_scroll, self.scroll_y + 40)
        elif event.key == pygame.K_PAGEUP:
            self.scroll_y = max(0, self.scroll_y - 300)
        elif event.key == pygame.K_PAGEDOWN:
            self.scroll_y = min(self.max_scroll, self.scroll_y + 300)
        elif event.key == pygame.K_ESCAPE:
            self.close_callback()
            
        return True

    def on_mouse(self, event, rect):
        import pygame
        if event.type == pygame.MOUSEBUTTONDOWN:
            if event.button == 4:
                self.scroll_y = max(0, self.scroll_y - 60)
                return True
            elif event.button == 5:
                self.scroll_y = min(self.max_scroll, self.scroll_y + 60)
                return True
            elif event.button == 1:
                mx, my = event.pos
                
                if self.btn_back_rect and self.btn_back_rect.collidepoint(mx, my):
                    self.browser.back()
                    self.url_active = False
                    return True
                    
                if self.input_url_rect and self.input_url_rect.collidepoint(mx, my):
                    self.url_active = True
                    page = getattr(self.browser, "_current", None)
                    if not self.url_text and page:
                        self.url_text = page.url
                    return True
                else:
                    self.url_active = False

                current_page = getattr(self.browser, "_current", None)
                if not current_page:
                    return False
                    
                for box in self.layout_boxes:
                    hit_rect = pygame.Rect(box.rect.x, box.rect.y - self.scroll_y, box.rect.w, box.rect.h)
                    
                    if hit_rect.bottom < rect.y:
                        continue
                    if hit_rect.top > rect.bottom:
                        break
                        
                    if box.box_type == "link" and hit_rect.collidepoint(mx, my):
                        href = box.node.attrs.get('href') if box.node else None
                        if href:
                            target_url = urllib.parse.urljoin(current_page.url, href)
                            self.browser.go(target_url)
                        return True
        return False

    def _rebuild_layout_if_needed(self, ctx: DrawCtx, page):
        version = getattr(self.browser, '_dom_version', 0)
        if page.url == self.last_page_url and version == self.last_dom_version:
            return

        if self.dom_renderer is None:
            self.dom_renderer = DOMRenderer(ctx.font)

        self.last_page_url = page.url
        self.last_dom_version = version
        self.scroll_y = 0

        tree = getattr(page, "semantic_tree", None)
        if not tree:
            self.layout_boxes = []
            self.max_scroll = 0
            return

        cx = ctx.rect.x + 40
        start_y = ctx.rect.y + self.nav_h + 20
        max_w = ctx.rect.w - 80
        char_w = max(1, ctx.font.size("A")[0])
        
        engine = LayoutEngine(max_w, char_w, ctx._line_h)
        self.layout_boxes, max_y = engine.build(tree, cx, start_y)
        
        view_h = ctx.rect.h - self.nav_h
        content_h = max_y - start_y
        self.max_scroll = max(0, content_h - view_h + 50)

    def _draw_all(self, ctx: DrawCtx):
        import pygame
        ctx.clear((12, 12, 18), alpha=215)
        
        page = getattr(self.browser, "_current", None)
        
        nav_rect = pygame.Rect(ctx.rect.x, ctx.rect.y, ctx.rect.w, self.nav_h)
        ctx.box(nav_rect, fill=(24, 24, 38), outline=(50, 50, 75))
        
        self.btn_back_rect = pygame.Rect(ctx.rect.x + 10, ctx.rect.y + 8, 30, 28)
        ctx.box(self.btn_back_rect, fill=(40, 40, 60), outline=(80, 80, 100), radius=4)
        ctx.text("<", (200, 200, 200), x=self.btn_back_rect.x + 10, y=self.btn_back_rect.y + 6)
        
        self.input_url_rect = pygame.Rect(ctx.rect.x + 50, ctx.rect.y + 8, ctx.rect.w - 200, 28)
        input_color = (35, 35, 50) if not self.url_active else (20, 20, 30)
        input_outline = (60, 60, 80) if not self.url_active else (100, 180, 255)
        ctx.box(self.input_url_rect, fill=input_color, outline=input_outline, radius=4)
        
        old_clip = ctx.surface.get_clip()
        ctx.surface.set_clip(self.input_url_rect)
        
        if self.url_active:
            display_text = self.url_text + ("_" if (pygame.time.get_ticks() // 500) % 2 == 0 else "")
            ctx.text(display_text, (255, 255, 255), x=self.input_url_rect.x + 10, y=self.input_url_rect.y + 6)
        else:
            if page:
                ctx.text(f"{page.url}", (180, 180, 180), x=self.input_url_rect.x + 10, y=self.input_url_rect.y + 6)
            else:
                ctx.text("Wpisz adres i wciśnij Enter...", (100, 100, 100), x=self.input_url_rect.x + 10, y=self.input_url_rect.y + 6)

        ctx.surface.set_clip(old_clip)
        ctx.text("ESC=Konsola", (140, 140, 160), x=ctx.rect.right - 130, y=ctx.rect.y + 12)

        if not page:
            ctx.text("Luneta (Karmin-engine) — Płótno gotowe.", (220, 220, 220), x=ctx.rect.x + 40, y=ctx.rect.y + self.nav_h + 40)
            ctx.text("Kliknij pasek adresu na górze i wpisz URL, aby rozpocząć nawigację.", (140, 140, 140), x=ctx.rect.x + 40, y=ctx.rect.y + self.nav_h + 70)
            ctx.text("Wciskając ESC, w każdej chwili powrócisz do konsoli systemowej.", (100, 100, 100), x=ctx.rect.x + 40, y=ctx.rect.y + self.nav_h + 95)
            return

        self._rebuild_layout_if_needed(ctx, page)
        
        if not self.layout_boxes:
            ctx.text(f"Brak drzewa DOM dla: {page.title}", (255, 100, 100), x=ctx.rect.x + 40, y=ctx.rect.y + self.nav_h + 30)
            return

        clip_rect = pygame.Rect(ctx.rect.x, ctx.rect.y + self.nav_h, ctx.rect.w, ctx.rect.h - self.nav_h)
        self.dom_renderer.draw(ctx, self.layout_boxes, self.scroll_y, clip_rect)


def show_help(term_state):
    help_text = """=== LUNETA — Przeglądarka phi-space v2.2 ===
Wpisz URL, aby przejść do strony.
Komenda 'canvas' lub 'view' otwiera pełnoekranowe płótno graficzne.
W trybie płótna naciśnij ESC lub F1, aby powrócić do konsoli."""
    term_state.append(help_text, (200, 200, 200))


def main():
    if not PYGAME_OK:
        print("Brak PyGame. Instalacja: pip install pygame")
        sys.exit(1)

    display = KarmazynDisplay()
    if not display.init(title="Luneta (Karmin-engine)"):
        print("Nie udało się zainicjalizować KarmazynDisplay.")
        sys.exit(1)

    runtime = LunetaRuntime()
    browser = LunetaBrowser(runtime)
    
    browser.gui_mode = True
    mapper = attach_to_browser(browser, runtime)
    
    if _HAS_ASYNC and install_async_engine_on_browser(browser):
        print("Silnik async aktywny...")

    display.bind_phi(runtime)
    display.bind_browser(browser)

    viewer = GraphicPageViewer(browser, display.renderer.release_fullscreen)

    # NATYCHMIASTOWE WYMUSZENIE WIDOKU GRAFICZNEGO PRZY STARCIE
    display.renderer.claim_fullscreen(viewer)

    def thread_shell_main(term_state):
        term_state.prompt = "LUNETA> "
        term_state.append("LUNETA gotowa. Wpisz URL lub 'canvas'.", (255, 255, 255))
        
        while True:
            try:
                cmd_in = term_state.get_input_blocking().strip()
                if not cmd_in:
                    continue
                
                parts = cmd_in.split()
                cmd_upper = parts[0].upper()

                if cmd_upper in ("HELP", "H", "?"):
                    show_help(term_state)
                elif cmd_upper in ("EXIT", "QUIT", "Q"):
                    break
                elif cmd_upper in ("CANVAS", "VIEW"):
                    display.renderer.claim_fullscreen(viewer)
                elif cmd_upper == "DOM":
                    msg = cmd_dom(parts[1:], browser, mapper)
                    term_state.append(strip_ansi(msg), (200, 200, 200))
                elif cmd_upper == "JS":
                    if getattr(browser, "_has_js", False) and browser.js_bridge:
                        async_out = cmd_async(parts[1:], browser.js_bridge)
                        if async_out is not None:
                            term_state.append(strip_ansi(async_out), (200, 200, 200))
                        else:
                            from karmazyn_js_web import cmd_js_bridge
                            msg = cmd_js_bridge(parts[1:], browser.js_bridge)
                            term_state.append(strip_ansi(msg), (200, 200, 200))
                    else:
                        term_state.append("Silnik JS niedostępny.", (255, 50, 50))
                else:
                    ok, msg = browser.go(cmd_in)
                    if msg:
                        term_state.append(strip_ansi(msg), (200, 200, 200))
                    if ok:
                        display.renderer.claim_fullscreen(viewer)

            except Exception as e:
                term_state.append(f"Błąd krytyczny REPL: {e}", (255, 50, 50))

        term_state.shutdown()

    display.run(shell_main=thread_shell_main)


if __name__ == "__main__":
    main()