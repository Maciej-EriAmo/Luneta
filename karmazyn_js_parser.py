"""
karmazyn_js_parser.py — JS Parser KarmazynOS v1.0
==================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Wejście:  tekst JavaScript (ES5 + arrow functions + template literals)
Wyjście:  lista tuple AST dla KarmazynJSCore

Architektura:
  JSLexer    → tokeny
  JSParser   → rekurencyjne zejście + Pratt dla wyrażeń
  parse_js() → punkt wejścia

Obsługiwany podzbiór JS:
  Tier 1 (inline scripts na ~80% stron):
    var/let/const, function, arrow functions
    if/else, while, do..while, for, for..of, for..in
    return, break, continue, throw
    try/catch/finally
    object/array literals, property access, method calls
    wszystkie operatory binarne z poprawnym priorytetem
    typeof, instanceof, new, delete, void
    template literals (→ konkatenacja)
    x++/x--/++x/--x
    +=/-=/*= itd.
    // i /* */ komentarze
    ASI (Automatic Semicolon Insertion)

  Nie obsługuje:
    class (ES6+) — TODO Tier 2
    destructuring — TODO Tier 2
    generators, async/await — TODO Tier 3
    import/export — nie potrzebne dla inline scripts
    regex literals — zastąpione stringiem

Integracja z Lunetą (karmazyn_js_web.py):
  Odkomentuj w JSBridge._run_inline():
    from karmazyn_js_parser import parse_js
    ast = parse_js(source)
    self._vm.run(ast)
"""

import re
from typing import Any, Iterator, List, Optional, Tuple


# ─── Tokeny ───────────────────────────────────────────────────────────────────

TT_NUMBER  = "NUMBER"
TT_STRING  = "STRING"
TT_IDENT   = "IDENT"
TT_KW      = "KW"
TT_OP      = "OP"
TT_PUNCT   = "PUNCT"
TT_NEWLINE = "NL"    # tylko dla ASI
TT_EOF     = "EOF"

KEYWORDS = frozenset({
    "var", "let", "const", "function", "return", "if", "else",
    "while", "do", "for", "of", "in", "break", "continue",
    "new", "delete", "typeof", "instanceof", "void",
    "throw", "try", "catch", "finally",
    "true", "false", "null", "undefined",
    "this", "class", "extends", "super",
    "import", "export", "async", "await",
    "switch", "case", "default",
})

# Operatory wieloznakowe — kolejność ważna (dłuższe pierwsze)
MULTICHAR_OPS = [
    "===", "!==", "**=", "**",
    "<<=", ">>=",
    "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
    "<<", ">>",
    "=>",
    "<=", ">=", "==", "!=",
    "&&", "||", "??",
    "++", "--",
    "...",
]

SINGLE_OPS = set("+-*/%<>&|^~!=?:")


class Token:
    __slots__ = ("tt", "val", "line")

    def __init__(self, tt: str, val: Any, line: int = 0):
        self.tt   = tt
        self.val  = val
        self.line = line

    def __repr__(self) -> str:
        return f"Token({self.tt}, {self.val!r})"


# ─── Lexer ────────────────────────────────────────────────────────────────────

class JSLexer:
    """
    Tokenizer. Produkuje listę Token.
    ASI: emituje TT_NEWLINE gdy po '\n' następuje token
    który może być początkiem wyrażenia (do obsługi przez parser).
    """

    def __init__(self, source: str):
        self.src  = source
        self.pos  = 0
        self.line = 1

    def tokenize(self) -> List[Token]:
        tokens = []
        while self.pos < len(self.src):
            tok = self._next()
            if tok is not None:
                tokens.append(tok)
        tokens.append(Token(TT_EOF, None, self.line))
        return tokens

    def _next(self) -> Optional[Token]:
        self._skip_whitespace_and_comments()
        if self.pos >= len(self.src):
            return None
        c = self.src[self.pos]

        # Nowa linia (ASI)
        if c == "\n":
            self.pos += 1
            self.line += 1
            return Token(TT_NEWLINE, "\n", self.line - 1)

        # Liczby
        if c.isdigit() or (c == "." and self.pos + 1 < len(self.src)
                           and self.src[self.pos + 1].isdigit()):
            return self._read_number()

        # Stringi
        if c in ('"', "'"):
            return self._read_string(c)

        # Template literal
        if c == "`":
            return self._read_template()

        # Identyfikator lub słowo kluczowe
        if c.isalpha() or c in ("_", "$"):
            return self._read_ident()

        # Operator wieloznakowy
        for op in MULTICHAR_OPS:
            if self.src[self.pos:self.pos + len(op)] == op:
                self.pos += len(op)
                return Token(TT_OP, op, self.line)

        # Operator jednoznakowy
        if c in SINGLE_OPS:
            self.pos += 1
            return Token(TT_OP, c, self.line)

        # Interpunkcja
        if c in "(){}[];,":
            self.pos += 1
            return Token(TT_PUNCT, c, self.line)

        # Dostęp przez kropkę — traktuj jako OP
        if c == ".":
            self.pos += 1
            return Token(TT_OP, ".", self.line)

        # Nieznany znak — pomiń
        self.pos += 1
        return None

    def _skip_whitespace_and_comments(self) -> None:
        while self.pos < len(self.src):
            c = self.src[self.pos]
            if c in (" ", "\t", "\r"):
                self.pos += 1
            elif (self.src[self.pos:self.pos + 2] == "//"):
                # Komentarz liniowy — nie konsumuj \n (do ASI)
                while self.pos < len(self.src) and self.src[self.pos] != "\n":
                    self.pos += 1
            elif self.src[self.pos:self.pos + 2] == "/*":
                self.pos += 2
                while self.pos < len(self.src):
                    if self.src[self.pos:self.pos + 2] == "*/":
                        self.pos += 2
                        break
                    if self.src[self.pos] == "\n":
                        self.line += 1
                    self.pos += 1
            else:
                break

    def _read_number(self) -> Token:
        start = self.pos
        # Hex
        if self.src[self.pos:self.pos + 2] in ("0x", "0X"):
            self.pos += 2
            while self.pos < len(self.src) and self.src[self.pos] in "0123456789abcdefABCDEF":
                self.pos += 1
            return Token(TT_NUMBER, int(self.src[start:self.pos], 16), self.line)
        # Dziesiętny
        while self.pos < len(self.src) and self.src[self.pos].isdigit():
            self.pos += 1
        if self.pos < len(self.src) and self.src[self.pos] == ".":
            self.pos += 1
            while self.pos < len(self.src) and self.src[self.pos].isdigit():
                self.pos += 1
        if self.pos < len(self.src) and self.src[self.pos] in ("e", "E"):
            self.pos += 1
            if self.pos < len(self.src) and self.src[self.pos] in ("+", "-"):
                self.pos += 1
            while self.pos < len(self.src) and self.src[self.pos].isdigit():
                self.pos += 1
        raw = self.src[start:self.pos]
        val = float(raw) if "." in raw or "e" in raw.lower() else int(raw)
        return Token(TT_NUMBER, val, self.line)

    def _read_string(self, quote: str) -> Token:
        self.pos += 1  # pomiń cudzysłów otwierający
        buf = []
        while self.pos < len(self.src):
            c = self.src[self.pos]
            if c == quote:
                self.pos += 1
                break
            if c == "\\":
                self.pos += 1
                esc = self.src[self.pos] if self.pos < len(self.src) else ""
                buf.append({
                    "n": "\n", "t": "\t", "r": "\r",
                    "\\": "\\", "'": "'", '"': '"', "`": "`",
                    "0": "\0",
                }.get(esc, esc))
                self.pos += 1
            elif c == "\n":
                self.line += 1
                buf.append(c)
                self.pos += 1
            else:
                buf.append(c)
                self.pos += 1
        return Token(TT_STRING, "".join(buf), self.line)

    def _read_template(self) -> Token:
        """
        Template literal → string z interpolacją jako Python string.
        `Hello ${name}!` → konkatenacja ("Hello " + name + "!")
        Uproszczone: zwraca Token STRING z tekstem (bez interpolacji)
        lub Token OP "TEMPLATE" z częściami do złożenia przez parser.
        """
        self.pos += 1  # pomiń `
        parts = []    # naprzemiennie: str, expr_src, str, ...
        buf   = []
        depth = 0

        while self.pos < len(self.src):
            c = self.src[self.pos]
            if c == "`" and depth == 0:
                self.pos += 1
                parts.append(("str", "".join(buf)))
                buf = []
                break
            if c == "$" and self.pos + 1 < len(self.src) and self.src[self.pos + 1] == "{":
                parts.append(("str", "".join(buf)))
                buf = []
                self.pos += 2
                # Zbierz wyrażenie do }
                expr_buf = []
                depth = 1
                while self.pos < len(self.src) and depth > 0:
                    ch = self.src[self.pos]
                    if ch == "{": depth += 1
                    elif ch == "}": depth -= 1
                    if depth > 0:
                        expr_buf.append(ch)
                    self.pos += 1
                parts.append(("expr", "".join(expr_buf)))
            elif c == "\\":
                self.pos += 1
                esc = self.src[self.pos] if self.pos < len(self.src) else ""
                buf.append({"n":"\n","t":"\t","r":"\r","\\":"\\","$":"$","`":"`"}.get(esc, esc))
                self.pos += 1
            elif c == "\n":
                self.line += 1
                buf.append(c)
                self.pos += 1
            else:
                buf.append(c)
                self.pos += 1

        # Jeśli brak interpolacji — zwykły string
        str_parts = [p for t, p in parts if t == "str"]
        expr_parts = [p for t, p in parts if t == "expr"]
        if not expr_parts:
            return Token(TT_STRING, "".join(str_parts), self.line)
        # Z interpolacją — specjalny token
        return Token("TEMPLATE", parts, self.line)

    def _read_ident(self) -> Token:
        start = self.pos
        while self.pos < len(self.src) and (
            self.src[self.pos].isalnum() or self.src[self.pos] in ("_", "$")
        ):
            self.pos += 1
        word = self.src[start:self.pos]
        tt   = TT_KW if word in KEYWORDS else TT_IDENT
        # Konwertuj literały
        if word == "true":   return Token(TT_NUMBER, True,  self.line)
        if word == "false":  return Token(TT_NUMBER, False, self.line)
        if word in ("null", "undefined"):
            return Token(TT_NUMBER, None, self.line)
        return Token(tt, word, self.line)


# ─── Pratt — priorytety operatorów ───────────────────────────────────────────

# (left_bp, right_bp) — wyższy right_bp = prawostronna asocjatywność
INFIX_BP = {
    "||":  (3, 4),
    "&&":  (5, 6),
    "|":   (7, 8),
    "^":   (9, 10),
    "&":   (11, 12),
    "==":  (13, 14),  "!=":  (13, 14),
    "===": (13, 14),  "!==": (13, 14),
    "<":   (15, 16),  ">":   (15, 16),
    "<=":  (15, 16),  ">=":  (15, 16),
    "in":  (15, 16),  "instanceof": (15, 16),
    "<<":  (17, 18),  ">>":  (17, 18),
    "+":   (19, 20),  "-":   (19, 20),
    "*":   (21, 22),  "/":   (21, 22),  "%":  (21, 22),
    "**":  (24, 23),  # prawostronna
}

ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "**=",
              "&=", "|=", "^=", "<<=", ">>="}


# ─── Parser ───────────────────────────────────────────────────────────────────

class JSParser:
    """
    Rekurencyjne zejście z Pratt parserem dla wyrażeń.
    Wyjście: lista tuple AST dla KarmazynJSCore.
    """

    def __init__(self, tokens: List[Token]):
        # Filtruj newline — zostawiamy je tylko tam gdzie ASI potrzebne
        self._toks  = tokens
        self._pos   = 0
        self._nl    = False   # czy ostatni skip_nl napotkał newline

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _peek(self, offset: int = 0) -> Token:
        """Podgląd tokenu (pomijając newline)."""
        i = self._pos
        skipped = 0
        while i < len(self._toks):
            t = self._toks[i]
            if t.tt == TT_NEWLINE:
                i += 1
                continue
            if skipped == offset:
                return t
            skipped += 1
            i += 1
        return Token(TT_EOF, None)

    def _peek_raw(self) -> Token:
        """Podgląd bez pomijania newline."""
        if self._pos < len(self._toks):
            return self._toks[self._pos]
        return Token(TT_EOF, None)

    def _advance(self) -> Token:
        """Pobierz następny token (pomijając newline, zapamiętując czy był)."""
        self._nl = False
        while self._pos < len(self._toks):
            t = self._toks[self._pos]
            self._pos += 1
            if t.tt == TT_NEWLINE:
                self._nl = True
                continue
            return t
        return Token(TT_EOF, None)

    def _expect(self, tt: str, val: Any = None) -> Token:
        t = self._advance()
        if t.tt != tt:
            raise SyntaxError(
                f"L{t.line}: oczekiwano {tt!r} got {t.tt!r}({t.val!r})"
            )
        if val is not None and t.val != val:
            raise SyntaxError(
                f"L{t.line}: oczekiwano {val!r} got {t.val!r}"
            )
        return t

    def _eat(self, tt: str, val: Any = None) -> bool:
        """Opcjonalne spożycie tokenu. Zwraca True jeśli zjedzony."""
        t = self._peek()
        if t.tt == tt and (val is None or t.val == val):
            self._advance()
            return True
        return False

    def _eat_semicolon(self) -> None:
        """Spożyj ';' lub zaakceptuj ASI (newline lub '}')."""
        t = self._peek_raw()
        if t.tt == TT_NEWLINE or t.val == ";" or t.val == "}" or t.tt == TT_EOF:
            if t.val == ";":
                self._advance()
            elif t.tt == TT_NEWLINE:
                # Spożyj wszystkie newline
                while self._pos < len(self._toks) and self._toks[self._pos].tt == TT_NEWLINE:
                    self._pos += 1
        else:
            self._eat(TT_PUNCT, ";")

    # ── Program ───────────────────────────────────────────────────────────────

    def parse(self) -> List[tuple]:
        stmts = []
        while self._peek().tt != TT_EOF:
            s = self._stmt()
            if s is not None:
                stmts.append(s)
        return stmts

    # ── Instrukcje ────────────────────────────────────────────────────────────

    def _stmt(self) -> Optional[tuple]:
        t = self._peek()

        if t.tt == TT_KW:
            kw = t.val

            if kw in ("var", "let", "const"):
                return self._var_decl()

            if kw == "function":
                return self._fn_decl()

            if kw == "if":
                return self._if_stmt()

            if kw == "while":
                return self._while_stmt()

            if kw == "do":
                return self._do_while_stmt()

            if kw == "for":
                return self._for_stmt()

            if kw == "return":
                return self._return_stmt()

            if kw == "throw":
                return self._throw_stmt()

            if kw == "try":
                return self._try_stmt()

            if kw in ("break", "continue"):
                self._advance()
                self._eat_semicolon()
                return (kw,)

            if kw == "switch":
                return self._switch_stmt()

            # class — uproszczone do function
            if kw == "class":
                return self._class_stmt()

        # Blok { ... }
        if t.tt == TT_PUNCT and t.val == "{":
            return ("block", self._block())

        # Średnik (pusta instrukcja)
        if t.tt == TT_PUNCT and t.val == ";":
            self._advance()
            return None

        # Newline — pomiń
        if t.tt == TT_NEWLINE:
            self._advance()
            return None

        # Wyrażenie jako instrukcja
        return self._expr_stmt()

    def _var_decl(self) -> tuple:
        kind = self._advance().val   # var/let/const
        name_tok = self._advance()
        name = name_tok.val

        # Destrukturyzacja tablicy lub obiektu — uproszczone: pomiń
        if name_tok.tt == TT_PUNCT and name_tok.val in ("[", "{"):
            # Pomiń do '=' lub ';'
            depth = 1
            while depth > 0:
                t = self._advance()
                if t.val in ("[", "{"): depth += 1
                elif t.val in ("]", "}"): depth -= 1
            if self._peek().val == "=":
                self._advance()
                self._expr()
            self._eat_semicolon()
            return ("expr", ("lit", None))   # placeholder

        # Wyrażenie przypisania
        if self._eat(TT_OP, "="):
            val = self._expr()
        else:
            val = ("lit", None)   # undefined

        # Wiele deklaracji: let a = 1, b = 2
        result = [("let", name, val)]
        while self._eat(TT_PUNCT, ","):
            n2 = self._advance().val
            if self._eat(TT_OP, "="):
                v2 = self._expr()
            else:
                v2 = ("lit", None)
            result.append(("let", n2, v2))

        self._eat_semicolon()
        if len(result) == 1:
            return result[0]
        return ("block", result)

    def _fn_decl(self) -> tuple:
        self._advance()  # "function"
        name_tok = self._peek()
        if name_tok.tt in (TT_IDENT, TT_KW):
            name = self._advance().val
        else:
            name = "<anonymous>"
        params = self._params()
        body   = self._block()
        return ("def", name, params, body)

    def _params(self) -> List[str]:
        """Parsuje listę parametrów (a, b, c)."""
        self._expect(TT_PUNCT, "(")
        params = []
        while self._peek().val != ")":
            if self._peek().val == "...":
                self._advance()
                p = self._advance().val
                params.append(f"...{p}")
                break
            p = self._advance().val
            # default param: p = val — pomiń wartość domyślną
            if self._eat(TT_OP, "="):
                self._expr()   # pomiń — domyślna wartość
            params.append(p)
            if not self._eat(TT_PUNCT, ","):
                break
        self._expect(TT_PUNCT, ")")
        return [p.lstrip("...") for p in params]

    def _if_stmt(self) -> tuple:
        self._advance()  # "if"
        self._expect(TT_PUNCT, "(")
        cond = self._expr()
        self._expect(TT_PUNCT, ")")
        then = self._stmt_or_block()
        else_ = []
        if self._eat(TT_KW, "else"):
            else_ = self._stmt_or_block()
        return ("if", cond, then if isinstance(then, list) else [then],
                else_ if isinstance(else_, list) else [else_] if else_ else [])

    def _stmt_or_block(self) -> Any:
        if self._peek().val == "{":
            return self._block()
        s = self._stmt()
        return [s] if s else []

    def _while_stmt(self) -> tuple:
        self._advance()  # "while"
        self._expect(TT_PUNCT, "(")
        cond = self._expr()
        self._expect(TT_PUNCT, ")")
        body = self._stmt_or_block()
        return ("while", cond, body if isinstance(body, list) else [body])

    def _do_while_stmt(self) -> tuple:
        self._advance()  # "do"
        body = self._stmt_or_block()
        self._expect(TT_KW, "while")
        self._expect(TT_PUNCT, "(")
        cond = self._expr()
        self._expect(TT_PUNCT, ")")
        self._eat_semicolon()
        # do..while → while z ciałem wykonanym raz na początku
        return ("block", [
            ("block", body if isinstance(body, list) else [body]),
            ("while", cond, body if isinstance(body, list) else [body]),
        ])

    def _for_stmt(self) -> tuple:
        self._advance()  # "for"
        self._expect(TT_PUNCT, "(")

        # Sprawdź for..of / for..in
        # Heurystyka: var/let/const IDENT of/in EXPR
        saved_pos = self._pos
        kind = None
        var_name = None
        if self._peek().val in ("var","let","const"):
            self._advance()
            if self._peek().tt in (TT_IDENT, TT_KW):
                var_name = self._advance().val
                if self._peek().val == "of":
                    kind = "of"
                elif self._peek().val == "in":
                    kind = "in"

        if kind in ("of", "in"):
            self._advance()  # "of" lub "in"
            iter_expr = self._expr()
            self._expect(TT_PUNCT, ")")
            body = self._stmt_or_block()
            stmt_type = "for_of" if kind == "of" else "for_in"
            return (stmt_type, var_name, iter_expr,
                    body if isinstance(body, list) else [body])

        # Klasyczny for(init; cond; update)
        self._pos = saved_pos

        # Init
        init_stmt = None
        if self._peek().val != ";":
            if self._peek().val in ("var","let","const"):
                init_stmt = self._var_decl_no_semi()
            else:
                init_stmt = ("expr", self._expr())
        self._eat(TT_PUNCT, ";")

        # Cond
        cond_expr = ("lit", True)
        if self._peek().val != ";":
            cond_expr = self._expr()
        self._eat(TT_PUNCT, ";")

        # Update
        update_stmt = None
        if self._peek().val != ")":
            update_stmt = ("expr", self._expr())
        self._expect(TT_PUNCT, ")")

        body = self._stmt_or_block()
        return ("for", init_stmt, cond_expr, update_stmt,
                body if isinstance(body, list) else [body])

    def _var_decl_no_semi(self) -> tuple:
        """Deklaracja zmiennej bez trailing semicolon (dla for init)."""
        kind = self._advance().val
        name = self._advance().val
        if self._eat(TT_OP, "="):
            val = self._expr()
        else:
            val = ("lit", None)
        return ("let", name, val)

    def _return_stmt(self) -> tuple:
        self._advance()  # "return"
        # ASI: jeśli zaraz newline lub ;, return undefined
        t = self._peek_raw()
        if t.tt in (TT_NEWLINE, TT_EOF) or t.val in (";", "}"):
            self._eat_semicolon()
            return ("return", ("lit", None))
        val = self._expr()
        self._eat_semicolon()
        return ("return", val)

    def _throw_stmt(self) -> tuple:
        self._advance()  # "throw"
        val = self._expr()
        self._eat_semicolon()
        return ("throw", val)

    def _try_stmt(self) -> tuple:
        self._advance()  # "try"
        try_body = self._block()

        catch_name = None
        catch_body = None
        if self._eat(TT_KW, "catch"):
            if self._eat(TT_PUNCT, "("):
                catch_name = self._advance().val
                self._expect(TT_PUNCT, ")")
            catch_body = self._block()

        finally_body = None
        if self._eat(TT_KW, "finally"):
            finally_body = self._block()

        return ("try", try_body, catch_name, catch_body, finally_body)

    def _switch_stmt(self) -> tuple:
        """switch → if/else chain."""
        self._advance()  # "switch"
        self._expect(TT_PUNCT, "(")
        disc = self._expr()
        self._expect(TT_PUNCT, ")")
        self._expect(TT_PUNCT, "{")

        # Zbierz case'y
        cases  = []  # [(test_expr, [stmts]), ...]
        default = None
        while self._peek().val != "}":
            if self._peek().tt == TT_EOF:
                break
            if self._eat(TT_KW, "case"):
                test  = self._expr()
                self._expect(TT_PUNCT, ":")
                stmts = []
                while (self._peek().val not in ("case","default","}")
                       and self._peek().tt != TT_EOF):
                    s = self._stmt()
                    if s: stmts.append(s)
                cases.append((test, stmts))
            elif self._eat(TT_KW, "default"):
                self._expect(TT_PUNCT, ":")
                stmts = []
                while (self._peek().val not in ("case","}")
                       and self._peek().tt != TT_EOF):
                    s = self._stmt()
                    if s: stmts.append(s)
                default = stmts
            else:
                break
        self._expect(TT_PUNCT, "}")

        # Konwertuj na if/else chain
        if not cases and default is None:
            return ("expr", ("lit", None))

        tmp = f"__sw_{id(cases)}"
        result = [("let", tmp, disc)]
        if_chain = None

        # Buduj od tyłu
        for test, stmts in reversed(cases):
            cond = ("op", ("var", tmp), "===", test)
            if if_chain is None and default is not None:
                if_chain = ("if", cond, stmts, default)
            elif if_chain is not None:
                if_chain = ("if", cond, stmts, [if_chain])
            else:
                if_chain = ("if", cond, stmts, [])

        if if_chain:
            result.append(if_chain)
        elif default:
            result.extend(default)
        return ("block", result)

    def _class_stmt(self) -> tuple:
        """class Foo extends Bar { ... } → uproszczona funkcja konstruktora."""
        self._advance()  # "class"
        name = self._advance().val
        if self._eat(TT_KW, "extends"):
            self._expr()   # pomiń super class
        self._expect(TT_PUNCT, "{")
        body_stmts = []
        while self._peek().val != "}" and self._peek().tt != TT_EOF:
            t = self._peek()
            if t.val == "constructor":
                self._advance()
                params = self._params()
                ctor_body = self._block()
                body_stmts = ctor_body
            elif t.tt in (TT_IDENT, TT_KW):
                method_name = self._advance().val
                params = self._params()
                mbody  = self._block()
                # Metody ignorowane w tej wersji
            else:
                self._advance()
        self._expect(TT_PUNCT, "}")
        return ("def", name, [], body_stmts)

    def _expr_stmt(self) -> tuple:
        e = self._expr()
        self._eat_semicolon()

        # Rozróżnij przypisanie od wyrażenia
        # Przypisanie do prostej zmiennej → ("assign", ...)
        # Przypisanie do właściwości → ("expr", ("setprop",...))
        # Reszta → ("expr", ...)
        if (isinstance(e, tuple) and len(e) >= 3
                and e[0] == "_assign"):
            return e[1]   # już rozkodowane przez _expr

        return ("expr", e)

    def _block(self) -> List[tuple]:
        self._expect(TT_PUNCT, "{")
        stmts = []
        while self._peek().val != "}" and self._peek().tt != TT_EOF:
            s = self._stmt()
            if s is not None:
                stmts.append(s)
        self._expect(TT_PUNCT, "}")
        return stmts

    # ── Wyrażenia (Pratt) ─────────────────────────────────────────────────────

    def _expr(self, min_bp: int = 0) -> tuple:
        """
        Pratt parser wyrażeń.
        Obsługuje wszystkie operatory JS z prawidłowym priorytetem.
        """
        left = self._prefix()

        while True:
            t = self._peek()
            op = t.val if t.tt in (TT_OP, TT_KW) else None

            # Operator przypisania (=, +=, ...)
            if op in ASSIGN_OPS and min_bp == 0:
                self._advance()
                right = self._expr(0)
                left  = self._make_assign(left, op, right)
                continue

            # Arrow function bez nawiasów: x => expr | x => { body }
            if (op == "=>" and isinstance(left, tuple)
                    and left[0] == "var" and min_bp == 0):
                self._advance()  # "=>"
                if self._peek().val == "{": 
                    body = self._block()
                else:
                    e = self._expr(0)
                    body = [("return", e)]
                left = ("fn", [left[1]], body)
                continue

            # Ternary ?
            if op == "?" and min_bp < 1:
                self._advance()
                then = self._expr(0)
                self._expect(TT_OP, ":")
                else_ = self._expr(0)
                left  = ("ternary", left, then, else_)
                continue

            # Binarny operator
            bp = INFIX_BP.get(op)
            if bp is None:
                break
            l_bp, r_bp = bp
            if l_bp < min_bp:
                break
            self._advance()
            right = self._expr(r_bp)
            left  = ("op", left, op, right)

        return left

    def _make_assign(self, target: tuple, op: str, value: tuple) -> tuple:
        """
        Konwertuje przypisanie na właściwą krotkę AST.
        Wynik opakowany w ("_assign", stmt) — rozpakowywany w _expr_stmt.
        """
        if op != "=":
            # x += y  →  x = x + y
            base_op = op[:-1]  # += → +, -= → -
            value = ("op", target, base_op, value)

        # Prosta zmienna
        if isinstance(target, tuple) and target[0] == "var":
            stmt = ("assign", target[1], value)
            return ("_assign", stmt)

        # Właściwość: obj.prop = val
        if isinstance(target, tuple) and target[0] == "prop":
            _, obj, key = target
            return ("setprop", obj, key, value)

        # Indeks: arr[i] = val
        if isinstance(target, tuple) and target[0] == "idx":
            _, obj, idx = target
            return ("setidx", obj, idx, value)

        # Fallback
        return ("_assign", ("expr", ("lit", None)))

    def _prefix(self) -> tuple:
        """Prefix — jednoargumentowe + primary + postfix."""
        t = self._peek()

        # Jednoargumentowe
        if t.tt == TT_OP and t.val == "!":
            self._advance()
            return ("not", self._prefix())

        if t.tt == TT_OP and t.val == "-":
            self._advance()
            operand = self._prefix()
            # Optymalizacja: -3 → lit(-3)
            if isinstance(operand, tuple) and operand[0] == "lit":
                return ("lit", -operand[1])
            return ("neg", operand)

        if t.tt == TT_OP and t.val == "+":
            self._advance()
            return self._prefix()   # unary + = no-op dla liczb

        if t.tt == TT_OP and t.val == "~":
            self._advance()
            return ("op", ("lit", -1), "^", self._prefix())

        if t.tt == TT_KW and t.val == "typeof":
            self._advance()
            return ("typeof", self._prefix())

        if t.tt == TT_KW and t.val in ("void", "delete"):
            self._advance()
            self._prefix()
            return ("lit", None)

        if t.tt == TT_OP and t.val == "++":
            self._advance()
            name = self._advance().val
            return ("_assign", ("assign", name,
                    ("op", ("var", name), "+", ("lit", 1))))

        if t.tt == TT_OP and t.val == "--":
            self._advance()
            name = self._advance().val
            return ("_assign", ("assign", name,
                    ("op", ("var", name), "-", ("lit", 1))))

        if t.tt == TT_KW and t.val == "new":
            self._advance()
            fn = self._primary_no_call()
            args = []
            if self._peek().val == "(":
                args = self._call_args()
            return ("new", fn, args)

        if t.tt == TT_KW and t.val == "await":
            self._advance()
            return self._prefix()   # uproszczone — synchroniczne

        expr = self._primary()
        return self._postfix(expr)

    def _postfix(self, expr: tuple) -> tuple:
        """Postfix: call(), .prop, [idx], ++, --"""
        while True:
            t = self._peek()

            # Wywołanie ()
            if t.tt == TT_PUNCT and t.val == "(":
                args  = self._call_args()
                expr  = ("call", expr, args)
                continue

            # Dostęp .prop
            if t.tt == TT_OP and t.val == ".":
                self._advance()
                key_tok = self._advance()
                key     = key_tok.val
                expr    = ("prop", expr, key)
                continue

            # Dostęp [idx]
            if t.tt == TT_PUNCT and t.val == "[":
                self._advance()
                idx  = self._expr()
                self._expect(TT_PUNCT, "]")
                expr = ("idx", expr, idx)
                continue

            # Postfix ++/-- (wartość przed inkrementacją — uproszczone do assign)
            if t.tt == TT_OP and t.val == "++" and not self._nl:
                self._advance()
                if isinstance(expr, tuple) and expr[0] == "var":
                    name = expr[1]
                    return ("_assign", ("assign", name,
                            ("op", ("var", name), "+", ("lit", 1))))

            if t.tt == TT_OP and t.val == "--" and not self._nl:
                self._advance()
                if isinstance(expr, tuple) and expr[0] == "var":
                    name = expr[1]
                    return ("_assign", ("assign", name,
                            ("op", ("var", name), "-", ("lit", 1))))

            break
        return expr

    def _call_args(self) -> List[tuple]:
        """Parsuje argumenty wywołania (a, b, ...c)."""
        self._expect(TT_PUNCT, "(")
        args = []
        while self._peek().val != ")":
            if self._peek().val == "...":
                self._advance()
                args.append(("spread", self._expr()))
            else:
                args.append(self._expr())
            if not self._eat(TT_PUNCT, ","):
                break
        self._expect(TT_PUNCT, ")")
        return args

    def _primary(self) -> tuple:
        """Primary expression + postfix."""
        expr = self._primary_no_call()
        return self._postfix(expr)

    def _primary_no_call(self) -> tuple:
        """Primary expression bez postfix (dla `new`)."""
        t = self._peek()

        # Literały
        if t.tt == TT_NUMBER:
            self._advance()
            return ("lit", t.val)

        if t.tt == TT_STRING:
            self._advance()
            return ("lit", t.val)

        # Template literal z interpolacją
        if t.tt == "TEMPLATE":
            self._advance()
            return self._build_template(t.val)

        # Identyfikator
        if t.tt == TT_IDENT:
            self._advance()
            return ("var", t.val)

        # this
        if t.tt == TT_KW and t.val == "this":
            self._advance()
            return ("var", "this")

        # function expression
        if t.tt == TT_KW and t.val == "function":
            self._advance()
            name = ""
            if self._peek().tt in (TT_IDENT, TT_KW) and self._peek().val != "(":
                name = self._advance().val
            params = self._params()
            body   = self._block()
            fn = ("fn", params, body)
            if name:
                # Named function expression — wstrzyknij nazwę do własnego scope
                body_with_name = [("let", name, fn)] + body
                return ("fn", params, body_with_name)
            return fn

        # Arrow function: (a, b) => ... lub a => ...
        if t.tt == TT_PUNCT and t.val == "(":
            return self._paren_or_arrow()

        # Tablice [a, b, c]
        if t.tt == TT_PUNCT and t.val == "[":
            self._advance()
            items = []
            while self._peek().val != "]":
                if self._peek().val == ",":
                    items.append(("lit", None))  # hole
                    self._advance()
                    continue
                if self._peek().val == "...":
                    self._advance()
                    items.append(("spread", self._expr()))
                else:
                    items.append(self._expr())
                if not self._eat(TT_PUNCT, ","):
                    break
            self._expect(TT_PUNCT, "]")
            return ("array", items)

        # Obiekty {key: val}
        if t.tt == TT_PUNCT and t.val == "{":
            return self._object_literal()

        # Słowa kluczowe jako wyrażenia
        if t.tt == TT_KW and t.val == "new":
            pass   # obsługiwane w _prefix

        raise SyntaxError(f"L{t.line}: nieoczekiwany token {t.tt!r}({t.val!r})")

    def _paren_or_arrow(self) -> tuple:
        """
        Rozróżnia (expr) od (a, b) => ... przez lookahead.
        """
        # Zapisz pozycję
        saved = self._pos
        is_arrow = False

        try:
            self._expect(TT_PUNCT, "(")
            # Parsuj potencjalne parametry
            params = []
            while self._peek().val != ")":
                t = self._peek()
                if t.tt in (TT_IDENT, TT_KW) and t.val not in KEYWORDS:
                    params.append(self._advance().val)
                elif t.val == "...":
                    self._advance()
                    params.append(self._advance().val)
                elif t.val == ",":
                    self._advance()
                    continue
                else:
                    raise ValueError("not arrow")
            self._expect(TT_PUNCT, ")")
            if self._peek().val == "=>":
                is_arrow = True
        except Exception:
            is_arrow = False
            self._pos = saved

        if is_arrow:
            self._advance()  # "=>"
            if self._peek().val == "{":
                body = self._block()
            else:
                e = self._expr()
                body = [("return", e)]
            return ("fn", params, body)

        # Zwykłe grupowanie
        self._pos = saved
        self._advance()  # "("
        e = self._expr()
        self._expect(TT_PUNCT, ")")
        return e

    def _object_literal(self) -> tuple:
        """{ key: val, method() {...}, shorthand, ...spread }"""
        self._advance()  # "{"
        obj: dict = {}
        methods: list = []

        while self._peek().val != "}" and self._peek().tt != TT_EOF:
            t = self._peek()

            # Spread: ...obj
            if t.val == "...":
                self._advance()
                # Uproszczone: pomiń spread w object literal
                self._expr()
                self._eat(TT_PUNCT, ",")
                continue

            # get/set — traktuj jako metodę
            if t.val in ("get", "set") and self._peek_raw() != "(":
                accessor = self._advance().val
                t = self._peek()

            # Klucz
            if t.tt in (TT_IDENT, TT_KW, TT_STRING, TT_NUMBER):
                key = self._advance().val
            elif t.val == "[":
                self._advance()
                key_expr = self._expr()
                self._expect(TT_PUNCT, "]")
                # Dynamic key — uproszczone jako string "?"
                key = "?"
                if self._eat(TT_PUNCT, ":"):
                    obj[key] = self._expr()
                self._eat(TT_PUNCT, ",")
                continue
            else:
                break

            # Metoda: key(params) { body }
            if self._peek().val == "(":
                params = self._params()
                body   = self._block()
                obj[str(key)] = ("fn", params, body)
            # Shorthand: { x } → { x: x }
            elif self._peek().val in (",", "}"):
                obj[str(key)] = ("var", str(key))
            # Klucz: wartość
            elif self._eat(TT_OP, ":"):
                obj[str(key)] = self._expr()
            else:
                break

            if not self._eat(TT_PUNCT, ","):
                break

        self._expect(TT_PUNCT, "}")
        return ("obj", obj)

    def _build_template(self, parts: list) -> tuple:
        """Konwertuje template literal na konkatenację stringów."""
        exprs = []
        for typ, content in parts:
            if typ == "str":
                if content:
                    exprs.append(("lit", content))
            else:
                # Parsuj wyrażenie interpolacji
                try:
                    sub_tokens = JSLexer(content).tokenize()
                    sub_parser = JSParser(sub_tokens)
                    e = sub_parser._expr()
                    exprs.append(e)
                except Exception:
                    exprs.append(("lit", content))

        if not exprs:
            return ("lit", "")
        result = exprs[0]
        for e in exprs[1:]:
            result = ("op", result, "+", e)
        return result


# ─── Punkt wejścia ────────────────────────────────────────────────────────────

def parse_js(source: str) -> List[tuple]:
    """
    Parsuje tekst JavaScript i zwraca listę tuple AST
    gotową do wykonania przez KarmazynJSCore.run().

    Punkt integracji z JSBridge._run_inline():
        from karmazyn_js_parser import parse_js
        ast = parse_js(source)
        self._vm.run(ast)
    """
    tokens = JSLexer(source).tokenize()
    parser = JSParser(tokens)
    stmts  = parser.parse()

    # Odfiltruj None i rozkoduj _assign
    return [_unwrap(s) for s in stmts if s is not None]


def _unwrap(node: Any) -> Any:
    """
    Głęboki unwrap — rozkodowuje ("_assign", real_stmt) na wszystkich poziomach.
    Chodzi przez całe drzewo AST.
    """
    if not isinstance(node, tuple):
        return node

    op = node[0]

    # Rozkoduj _assign
    if op == "_assign":
        return _unwrap(node[1])

    # Rekurencja przez wszystkie elementy krotki
    return tuple(_unwrap_element(i, v) for i, v in enumerate(node))


def _unwrap_element(idx: int, val: Any) -> Any:
    """Recurse przez element krotki — obsługa list i krotek."""
    if isinstance(val, tuple):
        return _unwrap(val)
    if isinstance(val, list):
        return [_unwrap(v) for v in val if v is not None]
    if isinstance(val, dict):
        return {k: _unwrap(v) for k, v in val.items()}
    return val