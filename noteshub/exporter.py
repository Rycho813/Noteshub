"""Whole-book UTF-8 Markdown export with literal user text."""

import html
import re
import string
from collections.abc import Mapping, Sequence


_PUNCTUATION = re.compile("([" + re.escape(string.punctuation.replace("<", "").replace(">", "").replace("&", "")) + "])")
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.IGNORECASE)


def export_filename(title: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", title).strip(" .")
    # A conservative UTF-8 byte budget also fits Windows' UTF-16 name limit.
    stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    stem = stem or "未命名书籍"
    if _RESERVED.match(stem):
        stem = "_" + stem
    return stem + ".md"


def _literal_line(line: str) -> str:
    # Protect Markdown/Obsidian punctuation first, then encode HTML characters.
    # Doing this in the opposite order would escape the generated entities.
    escaped = html.escape(_PUNCTUATION.sub(r"\\\1", line), quote=False)
    indent = re.match(r"[ \t]*", escaped)[0]
    return indent.replace(" ", "&#32;").replace("\t", "&#9;") + escaped[len(indent):]


def literal_text(text: str) -> str:
    """Preserve line breaks without interpreting prose as Markdown or HTML."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(
        _literal_line(line) + ("  " if line and index < len(lines) - 1 else "")
        for index, line in enumerate(lines)
    )


def render_book(book: Mapping, notes: Sequence[Mapping]) -> str:
    """Render every supplied note in order, regardless of its processing status."""
    # A title/author occupies a single structural line. The UI creates these
    # using single-line inputs; line breaks from other clients stay plain text.
    title = literal_text(book["title"]).replace("\n", " ")
    parts = [f"# {title}", ""]
    if book["author"]:
        parts.extend(["作者：" + literal_text(book["author"]), ""])
    for index, note in enumerate(notes, start=1):
        original = "\n".join(
            "> " + line for line in literal_text(note["original_text"]).split("\n")
        )
        parts.extend([
            f"## 笔记 {index}", "", "### 原文", "", original, "",
            "### 一读评论", "", literal_text(note["first_comment"]), "",
            "### 二读评论", "", literal_text(note["second_comment"]), "", "---", "",
        ])
    return "\n".join(parts)
