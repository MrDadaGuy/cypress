"""Heading-aware Markdown chunking.

Splits on Markdown headings so each chunk carries its section path ("Bylaws > Article IV >
Section 3"), which the agent cites. Long sections are packed paragraph by paragraph up to the
set's chunk size, with overlap between consecutive chunks of the same section.
"""

import re
from dataclasses import dataclass

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# Bylaws and court records often use plain-text headings instead of Markdown ones.
PLAIN_HEADING = re.compile(r"^\s*\**\s*((ARTICLE|Article|SECTION|Section)\s+[IVXLC\d]+[.:]?.{0,80})\**\s*$")
PAGE_MARK = re.compile(r"^<!-- page \d+ -->$")


def tokens(text: str) -> int:
    return max(1, len(text) // 4)  # ~4 chars per token is close enough for sizing


@dataclass
class Chunk:
    section_path: str
    content: str
    tokens: int


def _sections(md: str) -> list[tuple[list[str], str]]:
    path: list[tuple[int, str]] = []
    out: list[tuple[list[str], list[str]]] = [([], [])]
    for line in md.splitlines():
        m = HEADING.match(line)
        level, title = (len(m.group(1)), m.group(2)) if m else (None, None)
        if not m and (p := PLAIN_HEADING.match(line)):
            title = p.group(1).strip(" *")
            level = 2 if p.group(2).lower() == "article" else 3
        if level:
            path = [(lvl, t) for lvl, t in path if lvl < level] + [(level, title)]
            out.append(([t for _, t in path], [line]))
        elif not PAGE_MARK.match(line.strip()):
            out[-1][1].append(line)
    return [(p, "\n".join(lines).strip()) for p, lines in out if "\n".join(lines).strip()]


def _paragraphs(text: str, limit: int) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out = []
    for p in paras:  # hard-split a single paragraph that is bigger than a chunk
        while tokens(p) > limit:
            cut = p.rfind(" ", 0, limit * 4) or limit * 4
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        out.append(p)
    return out


def chunk_markdown(md: str, title: str, chunk_tokens: int = 800, overlap: int = 100) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path, body in _sections(md):
        section = " > ".join(path) if path else title
        buf: list[str] = []
        for para in _paragraphs(body, chunk_tokens):
            if buf and tokens("\n\n".join(buf + [para])) > chunk_tokens:
                text = "\n\n".join(buf)
                chunks.append(Chunk(section, text, tokens(text)))
                tail, kept = [], 0  # carry the last paragraphs forward as overlap
                for b in reversed(buf):
                    if kept + tokens(b) > overlap:
                        break
                    tail.insert(0, b)
                    kept += tokens(b)
                buf = tail
            buf.append(para)
        if buf:
            text = "\n\n".join(buf)
            chunks.append(Chunk(section, text, tokens(text)))
    return [c for c in chunks if c.content.strip()]


def embed_text(c: Chunk, title: str) -> str:
    """What gets embedded: title and section give short chunks their context."""
    return f"{title}\n{c.section_path}\n\n{c.content}" if c.section_path != title else f"{title}\n\n{c.content}"
