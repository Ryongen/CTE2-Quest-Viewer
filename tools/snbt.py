"""Minimal SNBT parser for FTB Quests files (FTB-style: newlines separate entries, commas optional)."""
import re

_NUM = re.compile(r'^[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?([bBsSlLfFdD]?)$')
_BARE_END = set(' \t\r\n,:]}[{')


class SNBTError(ValueError):
    pass


class _Parser:
    def __init__(self, text):
        self.s = text
        self.i = 0
        self.n = len(text)

    def ws(self):
        s, n = self.s, self.n
        while self.i < n:
            c = s[self.i]
            if c in ' \t\r\n,':
                self.i += 1
            elif c == '/' and s.startswith('//', self.i):
                j = s.find('\n', self.i)
                self.i = n if j < 0 else j
            else:
                break

    def peek(self):
        self.ws()
        return self.s[self.i] if self.i < self.n else ''

    def err(self, msg):
        line = self.s.count('\n', 0, self.i) + 1
        raise SNBTError(f'{msg} at line {line}')

    def value(self):
        c = self.peek()
        if c == '{':
            return self.compound()
        if c == '[':
            return self.list()
        if c in '"\'':
            return self.string()
        return self.bare()

    def compound(self):
        self.i += 1
        out = {}
        while True:
            c = self.peek()
            if c == '}':
                self.i += 1
                return out
            if not c:
                self.err('unterminated compound')
            key = self.string() if c in '"\'' else self.bare_token()
            if self.peek() != ':':
                self.err(f'expected ":" after key {key!r}')
            self.i += 1
            out[key] = self.value()

    def list(self):
        self.i += 1
        # typed arrays: [I; 1, 2]
        m = re.match(r'([BIL])\s*;', self.s[self.i:self.i + 4])
        if m:
            self.i += m.end()
        out = []
        while True:
            c = self.peek()
            if c == ']':
                self.i += 1
                return out
            if not c:
                self.err('unterminated list')
            out.append(self.value())

    def string(self):
        q = self.s[self.i]
        self.i += 1
        buf = []
        s = self.s
        while True:
            if self.i >= self.n:
                self.err('unterminated string')
            c = s[self.i]
            if c == chr(92):  # backslash
                nxt = s[self.i + 1]
                buf.append({'n': '\n', 't': '\t'}.get(nxt, nxt))
                self.i += 2
            elif c == q:
                self.i += 1
                return ''.join(buf)
            else:
                buf.append(c)
                self.i += 1

    def bare_token(self):
        j = self.i
        while j < self.n and self.s[j] not in _BARE_END:
            j += 1
        if j == self.i:
            self.err(f'unexpected character {self.s[self.i]!r}')
        tok = self.s[self.i:j]
        self.i = j
        return tok

    def bare(self):
        tok = self.bare_token()
        if tok == 'true':
            return True
        if tok == 'false':
            return False
        m = _NUM.match(tok)
        if m:
            body = tok[:-1] if m.group(1) else tok
            suf = m.group(1).lower()
            if suf in ('f', 'd') or '.' in body or 'e' in body.lower():
                return float(body)
            return int(body)
        return tok


def loads(text):
    p = _Parser(text)
    v = p.value()
    if p.peek():
        p.err('trailing data')
    return v


def load(path):
    with open(path, encoding='utf-8') as f:
        return loads(f.read())
