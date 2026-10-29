"""Deterministic parser for the agreed, paragraph-based Zotero export format.

This is deliberately not a general Markdown renderer. The accepted dialect is
documented in README.md. Parsing, corrections and confirmation perform no I/O.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import re
from typing import Iterator


@dataclass(frozen=True)
class ParseIssue:
    code: str
    line: int
    message: str


@dataclass(frozen=True)
class PreviewRecord:
    record_id: int
    start_line: int
    end_line: int
    source_start: int
    source_end: int
    raw_source: str
    original_text: str | None
    first_comment: str | None
    issue: ParseIssue | None = None
    corrected: bool = False

    @property
    def needs_correction(self) -> bool:
        return self.issue is not None and not self.corrected


@dataclass(frozen=True)
class SourceHeader:
    start_line: int
    end_line: int
    raw_source: str
    source_start: int
    source_end: int


@dataclass(frozen=True)
class Preview:
    source_text: str
    sha256: str
    headers: tuple[SourceHeader, ...]
    records: tuple[PreviewRecord, ...]

    @property
    def can_confirm(self) -> bool:
        return bool(self.records) and not any(
            record.needs_correction for record in self.records
        )


@dataclass(frozen=True)
class ParsedNote:
    original_text: str
    first_comment: str
    raw_source: str
    start_line: int
    end_line: int
    manually_corrected: bool


@dataclass(frozen=True)
class _Paragraph:
    raw: str
    start: int
    end: int
    start_line: int
    end_line: int


def _paragraphs(source: str) -> Iterator[_Paragraph]:
    """Split on empty/space-only lines, keeping exact source spans and lines."""
    offset = 0
    start: int | None = None
    start_line = 0
    end = 0
    end_line = 0
    for number, line in enumerate(source.splitlines(keepends=True), start=1):
        content = line.removesuffix("\n").removesuffix("\r")
        if not content.strip():
            if start is not None:
                yield _Paragraph(source[start:end], start, end, start_line, end_line)
                start = None
        else:
            if start is None:
                start, start_line = offset, number
            end, end_line = offset + len(content), number
        offset += len(line)
    if start is not None:
        yield _Paragraph(source[start:end], start, end, start_line, end_line)


_TIMESTAMP = r"\(\d{4}/\d{1,2}/\d{1,2} \d{1,2}:\d{2}:\d{2}\)"


def _is_header(paragraph: _Paragraph, *, first: bool) -> bool:
    # Only the observed export header is recognized, and only before records.
    # Unknown prose/headings must not silently disappear.
    text = paragraph.raw
    if first:
        text = text.removeprefix("\ufeff")
    lines = [line.strip(" \t\r") for line in text.split("\n")]
    if lines == ["# 注释"] or (
        len(lines) == 2
        and lines[0] == "# 注释"
        and re.fullmatch(_TIMESTAMP, lines[1]) is not None
    ):
        return True
    return len(lines) == 1 and lines[0].startswith("### ") and bool(lines[0][4:].strip())


def _extract(raw: str, line: int) -> tuple[str | None, str | None, ParseIssue | None]:
    depth = 0
    for index, char in enumerate(raw):
        if char == "“":
            depth += 1
        elif char == "”":
            depth -= 1
            if depth == 0:
                original = raw[1:index]
                if not original.strip():
                    return None, None, ParseIssue(
                        "empty_original", line, "原文为空，请手动填写原文。"
                    )
                # Only strip the conventional separator spaces, not the text
                # or line breaks inside the original/comment.
                return original, raw[index + 1 :].lstrip(" \t"), None
    return None, None, ParseIssue(
        "unclosed_quote", line,
        "本段中文双引号未闭合；未跨空行继续匹配，请手动划分原文与评述。",
    )


def parse_markdown(source: str) -> Preview:
    """Parse UTF-8 text; never guess a split for an unmatched quote paragraph.

    Offsets are Python character offsets in source_text; lines are one-based.
    Only Chinese curly quotes are structural. Once the outer quote closes,
    all subsequent characters are comment text, regardless of their quotes.
    """
    records: list[PreviewRecord] = []
    headers: list[SourceHeader] = []
    for paragraph in _paragraphs(source):
        if not records and _is_header(paragraph, first=paragraph.start == 0):
            headers.append(SourceHeader(
                paragraph.start_line, paragraph.end_line, paragraph.raw,
                paragraph.start, paragraph.end,
            ))
            continue
        candidate = paragraph.raw
        if paragraph.start == 0:
            candidate = candidate.removeprefix("\ufeff")
        candidate = candidate.lstrip(" \t")
        if candidate.startswith("“"):
            original, comment, issue = _extract(candidate, paragraph.start_line)
        elif records:
            previous = records[-1]
            comment = previous.first_comment
            if not previous.needs_correction:
                comment = (
                    comment + source[previous.source_end:paragraph.end]
                    if comment else paragraph.raw
                )
            records[-1] = replace(
                previous,
                end_line=paragraph.end_line,
                source_end=paragraph.end,
                raw_source=source[previous.source_start:paragraph.end],
                first_comment=comment,
            )
            continue
        else:
            original, comment = None, None
            issue = ParseIssue(
                "orphan_comment", paragraph.start_line,
                "这段文字之前没有原文，无法确定归属；请手动整理为一条笔记。",
            )
        records.append(PreviewRecord(
            record_id=len(records) + 1,
            start_line=paragraph.start_line,
            end_line=paragraph.end_line,
            source_start=paragraph.start,
            source_end=paragraph.end,
            raw_source=paragraph.raw,
            original_text=original,
            first_comment=comment,
            issue=issue,
        ))
    return Preview(
        source, hashlib.sha256(source.encode("utf-8")).hexdigest(),
        tuple(headers), tuple(records),
    )


def correct_record(
    preview: Preview, record_id: int, *, original_text: str, first_comment: str
) -> Preview:
    """Return an edited preview, preserving the source and original diagnostic.

    The fields are explicit user input; their quotes are not parsed again.
    The future UI must keep this preview on the server, rather than trusting
    a client-supplied can_confirm/corrected flag.
    """
    if not isinstance(original_text, str) or not original_text.strip():
        raise ValueError("修正后的原文不能为空。")
    if not isinstance(first_comment, str):
        raise ValueError("评述必须为纯文本，可以为空。")
    if not any(record.record_id == record_id for record in preview.records):
        raise ValueError(f"记录不存在：{record_id}")
    return replace(preview, records=tuple(
        replace(record, original_text=original_text, first_comment=first_comment,
                corrected=True)
        if record.record_id == record_id else record
        for record in preview.records
    ))


def confirm_preview(preview: Preview) -> tuple[ParsedNote, ...]:
    """Produce all notes or raise; does not write files or partially import."""
    if not preview.can_confirm:
        raise ValueError("没有可导入的笔记，或仍有未修正的解析异常。")
    return tuple(ParsedNote(
        record.original_text, record.first_comment, record.raw_source,
        record.start_line, record.end_line, record.corrected,
    ) for record in preview.records)


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 Zotero Markdown 原文/评述解析")
    parser.add_argument("file", type=Path, help="UTF-8 Markdown 文件")
    parser.add_argument("--json", action="store_true", help="输出含原始内容的完整预览 JSON")
    args = parser.parse_args()
    try:
        preview = parse_markdown(args.file.read_bytes().decode("utf-8"))
    except (OSError, UnicodeError) as exc:
        parser.error(f"无法读取 UTF-8 文件：{exc}")
    issues = [record for record in preview.records if record.needs_correction]
    if args.json:
        payload = asdict(preview)
        payload["can_confirm"] = preview.can_confirm
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"记录 {len(preview.records)}；正常 {len(preview.records) - len(issues)}；异常 {len(issues)}")
        for record in issues:
            print(f"记录 {record.record_id}，行 {record.start_line}–{record.end_line}：{record.issue.message}")
        if not preview.records:
            print("未发现笔记。")
        print("可确认导入。" if preview.can_confirm else "不能确认导入；请检查并修正预览。")
    return 0 if preview.can_confirm else 2


if __name__ == "__main__":
    raise SystemExit(main())
