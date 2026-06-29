"""
karmazyn_js_parser.py — JS Parser KarmazynOS v2.1 (Tier 2 - Semantic Fixes)
===========================================================================
KarmazynOS — Maciej Mazur, Warsaw 2026

Wejście:  tekst JavaScript (ES5 + ES6 Class + Destructuring + Computed Props)
Wyjście:  lista tuple AST dla KarmazynJSCore

Wersja 2.1 naprawia utraty semantyki:
  - '??', '>>>', '>>>=' dodane do lexera i Pratta.
  - switch zachowuje strukturę cases (wsparcie dla fallthrough do obsługi w VM).
  - delete, void, await są zachowywane w AST.
  - get/set oraz computed properties [expr] zachowane w literałach obiektów.
  - for..of wspiera destrukturyzację (for (let [a,b] of x)).
  - precyzyjne odróżnianie modyfikatora `static` od metody `static()`.
  - domyślne parametry (x = 10) zachowane w strukturze argumentów.
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
TT_NEWLINE = "NL"
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

MULTICHAR_OPS = [
    "===", "!==", "**=", ">>>=", ">>>", "**",
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

        if c == "\n":
            self.pos += 1
            self.line += 1
            return Token(TT_NEWLINE, "\n", self.line - 1)

        if c.isdigit() or (c == "." and self.pos + 1 < len(self.src)
                           and self.src[self.pos + 1].isdigit()):
            return self._read_number()

        if c in ('"', "'"):
            return self._read_string(c)

        if c == "`":
            return self._read_template()

        if c.isalpha() or c in ("_", "$"):
            return self._read_ident()

        for op in MULTICHAR_OPS:
            if self.src[self.pos:self.pos + len(op)] == op:
                self.pos += len(op)
                return Token(TT_OP, op, self.line)

        if c in SINGLE_OPS:
            self.pos += 1
            return Token(TT_OP, c, self.line)

        if c in "(){}[];,":
            self.pos += 1
            return Token(TT_PUNCT, c, self.line)

        if c == ".":
            self.pos += 1
            return Token(TT_OP, ".", self.line)

        self.pos += 1
        return None

    def _skip_whitespace_and_comments(self) -> None:
        while self.pos < len(self.src):
            c = self.src[self.pos]
            if c in (" ", "\t", "\r"):
                self.pos += 1
            elif (self.src[self.pos:self.pos + 2] == "//"):
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
        if self.src[self.pos:self.pos + 2] in ("0x", "0X"):
            self.pos += 2
            while self.pos < len(self.src) and self.src[self.pos] in "0123456789abcdefABCDEF":
                self.pos += 1
            return Token(TT_NUMBER, int(self.src[start:self.pos], 16), self.line)
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
        self.pos += 1
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
        self.pos += 1
        parts = []
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

        str_parts = [p for t, p in parts if t == "str"]
        expr_parts = [p for t, p in parts if t == "expr"]
        if not expr_parts:
            return Token(TT_STRING, "".join(str_parts), self.line)
        return Token("TEMPLATE", parts, self.line)

    def _read_ident(self) -> Token:
        start = self.pos
        while self.pos < len(self.src) and (
            self.src[self.pos].isalnum() or self.src[self.pos] in ("_", "$")
        ):
            self.pos += 1
        word = self.src[start:self.pos]
        tt   = TT_KW if word in KEYWORDS else TT_IDENT
        if word == "true":   return Token(TT_NUMBER, True,  self.line)
        if word == "false":  return Token(TT_NUMBER, False, self.line)
        if word in ("null", "undefined"):
            return Token(TT_NUMBER, None, self.line)
        return Token(tt, word, self.line)

# ─── Pratt — priorytety operatorów ───────────────────────────────────────────

INFIX_BP = {
    "??":  (3, 4),
    "||":  (4, 5),
    "&&":  (6, 7),
    "|":   (8, 9),
    "^":   (10, 11),
    "&":   (12, 13),
    "==":  (14, 15),  "!=":  (14, 15),
    "===": (14, 15),  "!==": (14, 15),
    "<":   (16, 17),  ">":   (16, 17),
    "<=":  (16, 17),  ">=":  (16, 17),
    "in":  (16, 17),  "instanceof": (16, 17),
    "<<":  (18, 19),  ">>":  (18, 19),  ">>>": (18, 19),
    "+":   (20, 21),  "-":   (20, 21),
    "*":   (22, 23),  "/":   (22, 23),  "%":  (22, 23),
    "**":  (25, 24),
}

ASSIGN_OPS = {"=", "+=", "-=", "*=", "/=", "%=", "**=",
              "&=", "|=", "^=", "<<=", ">>=", ">>>="}

# ─── Parser ───────────────────────────────────────────────────────────────────

class JSParser:
    def __init__(self, tokens: List[Token]):
        self._toks  = tokens
        self._pos   = 0
        self._nl    = False

    def _peek(self, offset: int = 0) -> Token:
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
        if self._pos < len(self._toks):
            return self._toks[self._pos]
        return Token(TT_EOF, None)

    def _advance(self) -> Token:
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
            raise SyntaxError(f"L{t.line}: oczekiwano {tt!r} got {t.tt!r}({t.val!r})")
        if val is not None and t.val != val:
            raise SyntaxError(f"L{t.line}: oczekiwano {val!r} got {t.val!r}")
        return t

    def _eat(self, tt: str, val: Any = None) -> bool:
        t = self._peek()
        if t.tt == tt and (val is None or t.val == val):
            self._advance()
            return True
        return False

    def _eat_semicolon(self) -> None:
        t = self._peek_raw()
        if t.tt == TT_NEWLINE or t.val == ";" or t.val == "}" or t.tt == TT_EOF:
            if t.val == ";":
                self._advance()
            elif t.tt == TT_NEWLINE:
                while self._pos < len(self._toks) and self._toks[self._pos].tt == TT_NEWLINE:
                    self._pos += 1
        else:
            self._eat(TT_PUNCT, ";")

    def parse(self) -> List[tuple]:
        stmts = []
        while self._peek().tt != TT_EOF:
            s = self._stmt()
            if s is not None:
                stmts.append(s)
        return stmts

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
            if kw == "class":
                return self._class_stmt()

        if t.tt == TT_PUNCT and t.val == "{":
            return ("block", self._block())
        if t.tt == TT_PUNCT and t.val == ";":
            self._advance()
            return None
        if t.tt == TT_NEWLINE:
            self._advance()
            return None

        return self._expr_stmt()

    def _destructuring_pattern(self) -> tuple:
        t = self._advance()
        targets = []
        if t.val == "[":
            pos = 0
            while self._peek().val != "]" and self._peek().tt != TT_EOF:
                if self._peek().val == ",":
                    self._advance(); pos += 1; continue
                nm = self._advance().val
                targets.append((nm, ("idx", pos)))
                if self._eat(TT_PUNCT, ","): pos += 1
                else: break
            self._expect(TT_PUNCT, "]")
            return ("array_pat", targets)
        else:
            while self._peek().val != "}" and self._peek().tt != TT_EOF:
                key = self._advance().val
                alias = key
                if self._eat(TT_OP, ":"):
                    alias = self._advance().val
                targets.append((alias, ("prop", key)))
                if not self._eat(TT_PUNCT, ","): break
            self._expect(TT_PUNCT, "}")
            return ("obj_pat", targets)

    def _var_decl(self) -> tuple:
        kind = self._advance().val
        name_tok = self._peek()

        if name_tok.tt == TT_PUNCT and name_tok.val in ("[", "{"):
            pattern = self._destructuring_pattern()
            rhs = ("lit", None)
            if self._eat(TT_OP, "="):
                rhs = self._expr()
            self._eat_semicolon()
            
            tmp = f"__destr_{id(pattern)}"
            out = [("let", tmp, rhs)]
            if pattern[0] == "array_pat":
                for nm, acc in pattern[1]:
                    out.append(("let", nm, ("idx", ("var", tmp), ("lit", acc[1]))))
            else:
                for nm, acc in pattern[1]:
                    out.append(("let", nm, ("prop", ("var", tmp), acc[1])))
            return ("begin", out)

        name = self._advance().val
        if self._eat(TT_OP, "="):
            val = self._expr()
        else:
            val = ("lit", None)

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
        return ("begin", result)

    def _fn_decl(self) -> tuple:
        self._advance()
        name_tok = self._peek()
        if name_tok.tt in (TT_IDENT, TT_KW):
            name = self._advance().val
        else:
            name = "<anonymous>"
        params = self._params()
        body   = self._block()
        return ("def", name, params, body)

    def _params(self) -> List[tuple]:
        self._expect(TT_PUNCT, "(")
        params = []
        while self._peek().val != ")":
            if self._peek().val == "...":
                self._advance()
                p = self._advance().val
                params.append(("spread", p))
                break
            p = self._advance().val
            if self._eat(TT_OP, "="):
                default_val = self._expr()
                params.append(("default", p, default_val))
            else:
                params.append(("param", p))
            if not self._eat(TT_PUNCT, ","):
                break
        self._expect(TT_PUNCT, ")")
        return params

    def _if_stmt(self) -> tuple:
        self._advance()
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
        self._advance()
        self._expect(TT_PUNCT, "(")
        cond = self._expr()
        self._expect(TT_PUNCT, ")")
        body = self._stmt_or_block()
        return ("while", cond, body if isinstance(body, list) else [body])

    def _do_while_stmt(self) -> tuple:
        self._advance()
        body = self._stmt_or_block()
        self._expect(TT_KW, "while")
        self._expect(TT_PUNCT, "(")
        cond = self._expr()
        self._expect(TT_PUNCT, ")")
        self._eat_semicolon()
        return ("block", [
            ("block", body if isinstance(body, list) else [body]),
            ("while", cond, body if isinstance(body, list) else [body]),
        ])

    def _for_stmt(self) -> tuple:
        self._advance()
        self._expect(TT_PUNCT, "(")
        saved_pos = self._pos
        kind = None
        var_name = None
        
        var_tok = self._peek()
        if var_tok.val in ("var","let","const"):
            self._advance()
            next_tok = self._peek()
            if next_tok.tt in (TT_IDENT, TT_KW):
                var_name = self._advance().val
            elif next_tok.val in ("[", "{"):
                var_name = self._destructuring_pattern()
            
            if self._peek().val in ("of", "in"):
                kind = self._advance().val

        if kind in ("of", "in"):
            iter_expr = self._expr()
            self._expect(TT_PUNCT, ")")
            body = self._stmt_or_block()
            stmt_type = "for_of" if kind == "of" else "for_in"
            return (stmt_type, var_name, iter_expr,
                    body if isinstance(body, list) else [body])

        self._pos = saved_pos
        init_stmt = None
        if self._peek().val != ";":
            if self._peek().val in ("var","let","const"):
                init_stmt = self._var_decl_no_semi()
            else:
                init_stmt = ("expr", self._expr())
        self._eat(TT_PUNCT, ";")

        cond_expr = ("lit", True)
        if self._peek().val != ";":
            cond_expr = self._expr()
        self._eat(TT_PUNCT, ";")

        update_stmt = None
        if self._peek().val != ")":
            update_stmt = ("expr", self._expr())
        self._expect(TT_PUNCT, ")")

        body = self._stmt_or_block()
        return ("for", init_stmt, cond_expr, update_stmt,
                body if isinstance(body, list) else [body])

    def _var_decl_no_semi(self) -> tuple:
        kind = self._advance().val
        name = self._advance().val
        if self._eat(TT_OP, "="):
            val = self._expr()
        else:
            val = ("lit", None)
        return ("let", name, val)

    def _return_stmt(self) -> tuple:
        self._advance()
        t = self._peek_raw()
        if t.tt in (TT_NEWLINE, TT_EOF) or t.val in (";", "}"):
            self._eat_semicolon()
            return ("return", ("lit", None))
        val = self._expr()
        self._eat_semicolon()
        return ("return", val)

    def _throw_stmt(self) -> tuple:
        self._advance()
        val = self._expr()
        self._eat_semicolon()
        return ("throw", val)

    def _try_stmt(self) -> tuple:
        self._advance()
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
        self._advance()
        self._expect(TT_PUNCT, "(")
        disc = self._expr()
        self._expect(TT_PUNCT, ")")
        self._expect(TT_PUNCT, "{")

        cases  = []
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
        
        # Natywny węzeł AST dla pełnej semantyki (w tym fallthrough)
        return ("switch", disc, cases, default)

    def _class_stmt(self) -> tuple:
        self._advance()
        name = "<anonymous>"
        if self._peek().tt in (TT_IDENT, TT_KW) and self._peek().val not in ("extends", "{"):
            name = self._advance().val
            
        parent = None
        if self._eat(TT_KW, "extends"):
            parent = self._expr()
            
        self._expect(TT_PUNCT, "{")
        
        ctor_params = []
        ctor_body = []
        methods = []

        while self._peek().val != "}" and self._peek().tt != TT_EOF:
            t = self._peek()
            
            is_static = False
            if t.val == "static":
                next_t = self._peek(1)
                if next_t.tt in (TT_IDENT, TT_KW, TT_STRING) or next_t.val == "[":
                    self._advance()
                    is_static = True
                    t = self._peek()

            method_name = "?"
            if t.tt in (TT_IDENT, TT_KW, TT_STRING):
                method_name = self._advance().val
            elif t.val == "[":
                self._advance()
                key_expr = self._expr()
                self._expect(TT_PUNCT, "]")
                method_name = ("computed", key_expr)
            elif t.val == ";":
                self._advance()
                continue
            else:
                self._advance()
                continue

            params = self._params()
            body   = self._block()
            
            if method_name == "constructor" and not is_static:
                ctor_params = params
                ctor_body = body
            else:
                methods.append(("method", method_name, params, body, is_static))

        self._expect(TT_PUNCT, "}")
        ctor_fn = ("fn", ctor_params, ctor_body)
        return ("class", name, parent, ctor_fn, methods)

    def _expr_stmt(self) -> tuple:
        e = self._expr()
        self._eat_semicolon()
        if (isinstance(e, tuple) and len(e) >= 3 and e[0] == "_assign"):
            return e[1]
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
        left = self._prefix()

        while True:
            t = self._peek()
            op = t.val if t.tt in (TT_OP, TT_KW) else None

            if op in ASSIGN_OPS and min_bp == 0:
                self._advance()
                right = self._expr(0)
                left  = self._make_assign(left, op, right)
                continue

            if (op == "=>" and isinstance(left, tuple)
                    and left[0] == "var" and min_bp == 0):
                self._advance()
                if self._peek().val == "{": 
                    body = self._block()
                else:
                    e = self._expr(0)
                    body = [("return", e)]
                left = ("fn", [("param", left[1])], body)
                continue

            if op == "?" and min_bp < 1:
                self._advance()
                then = self._expr(0)
                self._expect(TT_OP, ":")
                else_ = self._expr(0)
                left  = ("ternary", left, then, else_)
                continue

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
        if op != "=":
            base_op = op[:-1]
            value = ("op", target, base_op, value)

        if isinstance(target, tuple):
            if target[0] == "var":
                stmt = ("assign", target[1], value)
                return ("_assign", stmt)
                
            if target[0] == "prop":
                _, obj, key = target
                return ("_assign", ("setprop", obj, key, value))
                
            if target[0] == "idx":
                _, obj, idx = target
                return ("_assign", ("setidx", obj, idx, value))
                
            if target[0] == "array":
                tmp = f"__da_{id(target)}"
                stmts = [("let", tmp, value)]
                for i, item in enumerate(target[1]):
                    if item[0] == "lit" and item[1] is None: continue
                    sub_assign = self._make_assign(item, "=", ("idx", ("var", tmp), ("lit", i)))
                    if sub_assign[0] == "_assign":
                        stmts.append(sub_assign[1])
                    else:
                        stmts.append(("expr", sub_assign))
                return ("_assign", ("begin", stmts))
                
            if target[0] == "obj":
                tmp = f"__do_{id(target)}"
                stmts = [("let", tmp, value)]
                for prop_node in target[1]:
                    if prop_node[0] == "prop":
                        k, v = prop_node[1], prop_node[2]
                        sub_assign = self._make_assign(v, "=", ("prop", ("var", tmp), k))
                        if sub_assign[0] == "_assign":
                            stmts.append(sub_assign[1])
                        else:
                            stmts.append(("expr", sub_assign))
                return ("_assign", ("begin", stmts))

        return ("_assign", ("expr", ("lit", None)))

    def _prefix(self) -> tuple:
        t = self._peek()

        if t.tt == TT_OP and t.val == "!":
            self._advance()
            return ("not", self._prefix())

        if t.tt == TT_OP and t.val == "-":
            self._advance()
            operand = self._prefix()
            if isinstance(operand, tuple) and operand[0] == "lit":
                return ("lit", -operand[1])
            return ("neg", operand)

        if t.tt == TT_OP and t.val == "+":
            self._advance()
            return self._prefix()

        if t.tt == TT_OP and t.val == "~":
            self._advance()
            return ("op", ("lit", -1), "^", self._prefix())

        if t.tt == TT_KW and t.val == "typeof":
            self._advance()
            return ("typeof", self._prefix())

        if t.tt == TT_KW and t.val == "void":
            self._advance()
            return ("void", self._prefix())
            
        if t.tt == TT_KW and t.val == "delete":
            self._advance()
            return ("delete", self._prefix())

        if t.tt == TT_OP and t.val == "++":
            self._advance()
            name = self._advance().val
            return ("_assign", ("assign", name, ("op", ("var", name), "+", ("lit", 1))))

        if t.tt == TT_OP and t.val == "--":
            self._advance()
            name = self._advance().val
            return ("_assign", ("assign", name, ("op", ("var", name), "-", ("lit", 1))))

        if t.tt == TT_KW and t.val == "new":
            self._advance()
            fn = self._primary_no_call()
            args = []
            if self._peek().val == "(":
                args = self._call_args()
            return ("new", fn, args)

        if t.tt == TT_KW and t.val == "await":
            self._advance()
            return ("await", self._prefix())

        expr = self._primary()
        return self._postfix(expr)

    def _postfix(self, expr: tuple) -> tuple:
        while True:
            t = self._peek()
            if t.tt == TT_PUNCT and t.val == "(":
                args  = self._call_args()
                expr  = ("call", expr, args)
                continue

            if t.tt == TT_OP and t.val == ".":
                self._advance()
                key_tok = self._advance()
                key     = key_tok.val
                expr    = ("prop", expr, key)
                continue

            if t.tt == TT_PUNCT and t.val == "[":
                self._advance()
                idx  = self._expr()
                self._expect(TT_PUNCT, "]")
                expr = ("idx", expr, idx)
                continue

            if t.tt == TT_OP and t.val == "++" and not self._nl:
                self._advance()
                if isinstance(expr, tuple) and expr[0] == "var":
                    name = expr[1]
                    return ("_assign", ("assign", name, ("op", ("var", name), "+", ("lit", 1))))

            if t.tt == TT_OP and t.val == "--" and not self._nl:
                self._advance()
                if isinstance(expr, tuple) and expr[0] == "var":
                    name = expr[1]
                    return ("_assign", ("assign", name, ("op", ("var", name), "-", ("lit", 1))))

            break
        return expr

    def _call_args(self) -> List[tuple]:
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
        expr = self._primary_no_call()
        return self._postfix(expr)

    def _primary_no_call(self) -> tuple:
        t = self._peek()

        if t.tt == TT_NUMBER:
            self._advance()
            return ("lit", t.val)

        if t.tt == TT_STRING:
            self._advance()
            return ("lit", t.val)

        if t.tt == "TEMPLATE":
            self._advance()
            return self._build_template(t.val)

        if t.tt == TT_IDENT:
            self._advance()
            return ("var", t.val)

        if t.tt == TT_KW and t.val == "this":
            self._advance()
            return ("var", "this")

        if t.tt == TT_KW and t.val == "super":
            self._advance()
            return ("var", "super")

        if t.tt == TT_KW and t.val == "class":
            return self._class_stmt()

        if t.tt == TT_KW and t.val == "function":
            self._advance()
            name = ""
            if self._peek().tt in (TT_IDENT, TT_KW) and self._peek().val != "(":
                name = self._advance().val
            params = self._params()
            body   = self._block()
            fn = ("fn", params, body)
            if name:
                body_with_name = [("let", name, fn)] + body
                return ("fn", params, body_with_name)
            return fn

        if t.tt == TT_PUNCT and t.val == "(":
            return self._paren_or_arrow()

        if t.tt == TT_PUNCT and t.val == "[":
            self._advance()
            items = []
            while self._peek().val != "]":
                if self._peek().val == ",":
                    items.append(("lit", None))
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

        if t.tt == TT_PUNCT and t.val == "{":
            return self._object_literal()

        if t.tt == TT_KW and t.val == "new":
            pass

        raise SyntaxError(f"L{t.line}: nieoczekiwany token {t.tt!r}({t.val!r})")

    def _paren_or_arrow(self) -> tuple:
        saved = self._pos
        is_arrow = False
        try:
            self._expect(TT_PUNCT, "(")
            params = []
            while self._peek().val != ")":
                t = self._peek()
                if t.tt in (TT_IDENT, TT_KW) and t.val not in KEYWORDS:
                    params.append(("param", self._advance().val))
                elif t.val == "...":
                    self._advance()
                    params.append(("spread", self._advance().val))
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
            self._advance()
            if self._peek().val == "{":
                body = self._block()
            else:
                e = self._expr()
                body = [("return", e)]
            return ("fn", params, body)

        self._pos = saved
        self._advance()
        e = self._expr()
        self._expect(TT_PUNCT, ")")
        return e

    def _object_literal(self) -> tuple:
        self._advance()
        props = []

        while self._peek().val != "}" and self._peek().tt != TT_EOF:
            t = self._peek()

            if t.val == "...":
                self._advance()
                self._expr()
                self._eat(TT_PUNCT, ",")
                continue

            accessor = None
            if t.val in ("get", "set") and self._peek_raw().val != "(":
                next_t = self._peek(1)
                if next_t.tt in (TT_IDENT, TT_KW, TT_STRING, TT_NUMBER) or next_t.val == "[":
                    accessor = self._advance().val
                    t = self._peek()

            key = "?"
            if t.tt in (TT_IDENT, TT_KW, TT_STRING, TT_NUMBER):
                key = self._advance().val
            elif t.val == "[":
                self._advance()
                key_expr = self._expr()
                self._expect(TT_PUNCT, "]")
                key = ("computed", key_expr)
            else:
                break

            if self._peek().val == "(":
                params = self._params()
                body   = self._block()
                fn = ("fn", params, body)
                if accessor:
                    props.append((accessor, key, fn))
                else:
                    props.append(("prop", key, fn))
            elif self._peek().val in (",", "}"):
                props.append(("prop", key, ("var", str(key))))
            elif self._eat(TT_OP, ":"):
                props.append(("prop", key, self._expr()))
            else:
                break

            if not self._eat(TT_PUNCT, ","):
                break

        self._expect(TT_PUNCT, "}")
        return ("obj", props)

    def _build_template(self, parts: list) -> tuple:
        exprs = []
        for typ, content in parts:
            if typ == "str":
                if content:
                    exprs.append(("lit", content))
            else:
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
    tokens = JSLexer(source).tokenize()
    parser = JSParser(tokens)
    stmts  = parser.parse()
    return [_unwrap(s) for s in stmts if s is not None]

def _unwrap(node: Any) -> Any:
    if not isinstance(node, tuple):
        return node
    op = node[0]
    if op == "_assign":
        return _unwrap(node[1])
    return tuple(_unwrap_element(i, v) for i, v in enumerate(node))

def _unwrap_element(idx: int, val: Any) -> Any:
    if isinstance(val, tuple):
        return _unwrap(val)
    if isinstance(val, list):
        return [_unwrap(v) for v in val if v is not None]
    if isinstance(val, dict):
        return {k: _unwrap(v) for k, v in val.items()}
    return val