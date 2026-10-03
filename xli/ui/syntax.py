#!/usr/bin/env python3
"""XLI syntax highlighting — a small, dependency-free tokenizer.

Code fences are most of what an agent prints, and a wall of single-colour text
is the hardest thing on screen to scan. This module turns one line of code into
``(text, style)`` spans using a per-language keyword set and a shared scanner
for strings, comments and numbers.

Deliberately not a parser: it never raises, never needs the language to be
installed, and unknown languages get a conservative rule set (strings,
numbers, comments) rather than being left plain. A wrong colour is a cosmetic
bug; an exception in the middle of printing an answer is not.

Styles produced: ``code`` (default), ``kw``, ``str``, ``num``, ``com``,
``fn``, ``op``. The ANSI painter and the curses palette both know them.
"""

from __future__ import annotations

import re

Span = tuple[str, str]

#: Languages we have keyword tables for. Everything else falls back to
#: ``generic``, which still highlights strings, numbers and comments.
KEYWORDS: dict[str, frozenset[str]] = {
    "python": frozenset("""
        and as assert async await break class continue def del elif else except finally for
        from global if import in is lambda nonlocal not or pass raise return try while with
        yield match case True False None self cls
    """.split()),
    "javascript": frozenset("""
        async await break case catch class const continue debugger default delete do else
        export extends finally for function if import in instanceof let new of return static
        super switch this throw try typeof var void while with yield true false null undefined
    """.split()),
    "typescript": frozenset("""
        abstract any as async await boolean break case catch class const constructor continue
        declare default delete do else enum export extends finally for from function if
        implements import in instanceof interface keyof let namespace never new number object
        of private protected public readonly return satisfies static string super switch this
        throw try type typeof undefined union unknown var void while yield true false null
    """.split()),
    "bash": frozenset("""
        if then else elif fi for while do done case esac function in return exit export local
        readonly declare source alias echo printf cd set unset shift trap
    """.split()),
    "sql": frozenset("""
        SELECT FROM WHERE INSERT INTO VALUES UPDATE SET DELETE CREATE TABLE ALTER DROP INDEX
        JOIN LEFT RIGHT INNER OUTER ON GROUP BY ORDER HAVING LIMIT OFFSET AS AND OR NOT NULL
        PRIMARY KEY FOREIGN REFERENCES DISTINCT UNION ALL COUNT SUM AVG MIN MAX
    """.split()),
    "go": frozenset("""
        break case chan const continue default defer else fallthrough for func go goto if
        import interface map package range return select struct switch type var nil true false
    """.split()),
    "rust": frozenset("""
        as async await break const continue crate dyn else enum extern false fn for if impl in
        let loop match mod move mut pub ref return self Self static struct super trait true type
        unsafe use where while
    """.split()),
    "c": frozenset("""
        auto break case char const continue default do double else enum extern float for goto if
        int long register return short signed sizeof static struct switch typedef union unsigned
        void volatile while NULL true false
    """.split()),
    "json": frozenset({"true", "false", "null"}),
    "yaml": frozenset({"true", "false", "null", "yes", "no"}),
    "toml": frozenset({"true", "false"}),
}

#: Human-ish aliases that map onto a table above.
_ALIASES = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "console": "bash",
    "postgres": "sql",
    "mysql": "sql",
    "sqlite": "sql",
    "golang": "go",
    "rs": "rust",
    "c++": "c",
    "cpp": "c",
    "h": "c",
    "yml": "yaml",
    "jsonc": "json",
}

_COMMENT_LINE = {
    "python": "#",
    "bash": "#",
    "yaml": "#",
    "toml": "#",
    "sql": "--",
}
_COMMENT_BLOCK = {
    "python": ('"""', '"""'),
    "javascript": ("/*", "*/"),
    "typescript": ("/*", "*/"),
    "go": ("/*", "*/"),
    "rust": ("/*", "*/"),
    "c": ("/*", "*/"),
    "sql": ("/*", "*/"),
}

def language_key(language: str) -> str:
    """Normalise a fence tag to a key in :data:`KEYWORDS` (or ``"generic"``)."""
    name = (language or "").strip().lower().lstrip(".")
    name = _ALIASES.get(name, name)
    return name if name in KEYWORDS else "generic"


def highlight_line(line: str, language: str) -> list[Span]:
    """One line of code as styled spans. Never raises."""
    lang = language_key(language)
    if lang == "generic" and (language or "").strip().lower() in ("diff", "patch"):
        return _highlight_diff(line, language)
    if lang in ("json", "yaml", "toml"):
        return _highlight_data(line, lang)
    return _highlight_code(line, lang)


def highlight(code: str, language: str) -> list[list[Span]]:
    return [highlight_line(line, language) for line in (code or "").split("\n")]


# ------------------------------------------------------------------ internals
def _highlight_data(line: str, lang: str) -> list[Span]:
    """Keys, values and punctuation for the structured formats."""
    out: list[Span] = []
    index = 0
    for match in re.finditer(r'("(?:\\.|[^"\\])*")\s*(:)?|\b(true|false|null|yes|no)\b|(-?\d[\d_.]*)', line):
        if match.start() > index:
            out.append((line[index:match.start()], "code"))
        text = match.group(0)
        if match.group(1):
            out.append((match.group(1), "str"))
            if match.group(2):
                out.append((line[match.end(1):match.end()], "op"))
        elif match.group(3):
            out.append((text, "kw"))
        else:
            out.append((text, "num"))
        index = match.end()
    if index < len(line):
        out.append((line[index:], "code"))
    return out or [(line, "code")]


def _highlight_diff(line: str, language: str) -> list[Span]:
    if line.startswith("+++") or line.startswith("---"):
        return [(line, "kw")]
    if line.startswith("@@"):
        return [(line, "com")]
    if line.startswith("+"):
        return [(line, "str")]
    if line.startswith("-"):
        return [(line, "bad")]
    return [(line, "code")]


def _highlight_code(line: str, lang: str) -> list[Span]:
    keywords = KEYWORDS.get(lang, frozenset())
    comment_prefix = _COMMENT_LINE.get(lang)
    block = _COMMENT_BLOCK.get(lang)

    # A comment runs to end of line; inside a string it would be wrong, so the
    # string scanner below wins for anything before it.
    comment_at = len(line)
    if comment_prefix:
        position = _find_outside_strings(line, comment_prefix)
        if position >= 0:
            comment_at = position
    if block and block[0] in line:
        position = _find_outside_strings(line, block[0])
        if position >= 0:
            comment_at = min(comment_at, position)
    if lang == "python" and "#" in line:
        position = _find_outside_strings(line, "#")
        if position >= 0:
            comment_at = min(comment_at, position)

    body, comment = line[:comment_at], line[comment_at:]

    spans: list[Span] = []
    index = 0
    token = re.compile(
        r"(?P<str>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)"
        r"|(?P<word>[A-Za-z_][A-Za-z0-9_]*)"
        r"|(?P<num>\b\d[\d_]*(?:\.\d[\d_]*)?\b)"
        r"|(?P<op>[+\-*/%=<>!&|^~@?:.,;(){}\[\]])"
    )
    for match in token.finditer(body):
        if match.start() > index:
            spans.append((body[index:match.start()], "code"))
        if match.group("str"):
            spans.append((match.group("str"), "str"))
        elif match.group("word"):
            word = match.group("word")
            after = body[match.end():match.end() + 1]
            if word in keywords:
                # A keyword followed by "(" is a call (print, len…) in most
                # languages — colouring it as a keyword reads as noise.
                spans.append((word, "fn" if after == "(" and word not in _CONTROL else "kw"))
            elif after == "(":
                spans.append((word, "fn"))
            else:
                spans.append((word, "code"))
        elif match.group("num"):
            spans.append((match.group("num"), "num"))
        else:
            spans.append((match.group("op"), "op"))
        index = match.end()
    if index < len(body):
        spans.append((body[index:], "code"))
    if comment:
        spans.append((comment, "com"))
    return [span for span in spans if span[0]] or [(line, "code")]


_CONTROL = frozenset({"if", "for", "while", "return", "try", "except", "with", "else", "elif"})


def _find_outside_strings(line: str, marker: str) -> int:
    """Index of ``marker`` that is not inside a quoted string, or -1."""
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "\"'`":
            quote = char
            index += 1
            continue
        if line.startswith(marker, index):
            return index
        index += 1
    return -1


__all__ = ["KEYWORDS", "highlight", "highlight_line", "language_key"]
