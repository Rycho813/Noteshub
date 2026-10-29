from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

from noteshub.parser import confirm_preview, correct_record, parse_markdown


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "tests" / "fixtures" / "论爱欲.md"


class ParserTests(unittest.TestCase):
    def note(self, source):
        preview = parse_markdown(source)
        self.assertTrue(preview.can_confirm)
        self.assertEqual(len(preview.records), 1)
        return confirm_preview(preview)[0]

    def test_normal_quote_and_comment(self):
        note = self.note("“原文。” 一读评述。")
        self.assertEqual(note.original_text, "原文。")
        self.assertEqual(note.first_comment, "一读评述。")

    def test_comment_without_separator(self):
        self.assertEqual(self.note("“原文”紧接评述").first_comment, "紧接评述")

    def test_no_comment(self):
        self.assertEqual(self.note("“原文”").first_comment, "")

    def test_nested_quotes(self):
        note = self.note("“他说：“你好。”然后离开。”我的评述")
        self.assertEqual(note.original_text, "他说：“你好。”然后离开。")
        self.assertEqual(note.first_comment, "我的评述")

    def test_many_nested_levels_without_recursion(self):
        for depth in (1, 2, 3, 10, 1000):
            with self.subTest(depth=depth):
                text = "“" * depth + "内容" + "”" * depth
                note = self.note(text + "评述")
                self.assertEqual(note.original_text, text[1:-1])
                self.assertEqual(note.first_comment, "评述")

    def test_comment_quotes_are_not_parsed(self):
        note = self.note("“原文”我说“你好”，也可以留下未闭合的“引号。")
        self.assertEqual(note.original_text, "原文")
        self.assertEqual(note.first_comment, "我说“你好”，也可以留下未闭合的“引号。")

    def test_preserve_inner_spaces_punctuation_and_newlines(self):
        note = self.note('“ 内文（括号） "英文"\n下一行 ”评述  \n继续')
        self.assertEqual(note.original_text, ' 内文（括号） "英文"\n下一行 ')
        self.assertEqual(note.first_comment, "评述  \n继续")

    def test_continuation_paragraphs_belong_to_previous_note(self):
        preview = parse_markdown("“第一条”第一段\n\n第二段\n\n第三段\n\n“第二条”")
        self.assertEqual(len(preview.records), 2)
        self.assertEqual(preview.records[0].first_comment, "第一段\n\n第二段\n\n第三段")
        self.assertEqual(preview.records[1].original_text, "第二条")

    def test_continuation_when_initial_comment_empty(self):
        self.assertEqual(self.note("“原文”\n\n评述").first_comment, "评述")

    def test_preserve_multiple_comment_paragraph_separators(self):
        self.assertEqual(self.note("“原文”评述\n \n\n另一段").first_comment,
                         "评述\n \n\n另一段")

    def test_leading_space_before_opening_quote(self):
        note = self.note("  “原文”评述")
        self.assertEqual(note.original_text, "原文")
        self.assertEqual(note.raw_source, "  “原文”评述")

    def test_windows_line_endings_and_bom(self):
        source = "\ufeff# 注释  \r\n(2026/10/28 20:00:29)\r\n\r\n### 书名\r\n\r\n“第一行\r\n第二行”评述\r\n\r\n补充\r\n"
        preview = parse_markdown(source)
        note = confirm_preview(preview)[0]
        self.assertEqual(note.original_text, "第一行\r\n第二行")
        self.assertEqual(note.first_comment, "评述\r\n\r\n补充")
        self.assertEqual((note.start_line, note.end_line), (6, 9))
        self.assertEqual(preview.sha256, hashlib.sha256(source.encode()).hexdigest())

    def test_bom_without_header(self):
        self.assertEqual(self.note("\ufeff“原文”评述").original_text, "原文")

    def test_observed_headers_only(self):
        preview = parse_markdown("# 注释  \n(2026/10/28 20:00:29)\n\n### 书名\n\n“原文”")
        self.assertEqual(len(preview.headers), 2)
        self.assertEqual(len(preview.records), 1)
        self.assertTrue(preview.can_confirm)

    def test_unknown_prefix_not_silently_dropped(self):
        preview = parse_markdown("# 不认识的标题\n\n“原文”")
        self.assertFalse(preview.can_confirm)
        self.assertEqual(preview.records[0].issue.code, "orphan_comment")
        self.assertEqual(preview.records[0].raw_source, "# 不认识的标题")

    def test_header_like_text_after_note_is_comment(self):
        note = self.note("“原文”\n\n### 这是我的评述")
        self.assertEqual(note.first_comment, "### 这是我的评述")

    def test_unmatched_quotes_do_not_consume_next_record(self):
        preview = parse_markdown("“缺结束\n\n“下一条”评述")
        self.assertFalse(preview.can_confirm)
        self.assertEqual(len(preview.records), 2)
        self.assertEqual(preview.records[0].raw_source, "“缺结束")
        self.assertIsNone(preview.records[0].original_text)
        self.assertEqual(preview.records[1].original_text, "下一条")

    def test_unmatched_nested_quote(self):
        preview = parse_markdown("““嵌套摘录”测试评述")
        self.assertEqual(preview.records[0].issue.code, "unclosed_quote")
        self.assertFalse(preview.can_confirm)

    def test_original_cannot_cross_blank_line(self):
        preview = parse_markdown("“第一段\n\n第二段”\n\n“下一条”")
        self.assertEqual(len(preview.records), 2)
        self.assertTrue(preview.records[0].needs_correction)
        self.assertEqual(preview.records[0].raw_source, "“第一段\n\n第二段”")
        self.assertIsNone(preview.records[0].first_comment)
        self.assertEqual(preview.records[1].original_text, "下一条")

    def test_orphan_comment_and_extra_closing_quote(self):
        preview = parse_markdown("没有所属原文”\n\n“正常”")
        self.assertEqual(preview.records[0].issue.code, "orphan_comment")
        self.assertFalse(preview.can_confirm)

    def test_empty_original_is_error(self):
        for source in ("“”评述", "“  ”"):
            with self.subTest(source=source):
                preview = parse_markdown(source)
                self.assertEqual(preview.records[0].issue.code, "empty_original")

    def test_empty_or_header_only_cannot_confirm(self):
        for source in ("", " \n\n", "# 注释\n\n### 书名"):
            with self.subTest(source=source):
                preview = parse_markdown(source)
                self.assertFalse(preview.can_confirm)
                with self.assertRaises(ValueError):
                    confirm_preview(preview)

    def test_correction_is_explicit_and_keeps_raw_evidence(self):
        preview = parse_markdown("““嵌套摘录”测试评述")
        fixed = correct_record(preview, 1, original_text="“嵌套摘录”", first_comment="测试评述")
        self.assertTrue(fixed.can_confirm)
        self.assertFalse(preview.can_confirm)
        self.assertEqual(fixed.source_text, preview.source_text)
        self.assertEqual(fixed.sha256, preview.sha256)
        self.assertEqual(fixed.records[0].raw_source, preview.records[0].raw_source)
        self.assertEqual(fixed.records[0].issue, preview.records[0].issue)
        note = confirm_preview(fixed)[0]
        self.assertTrue(note.manually_corrected)
        self.assertEqual(note.original_text, "“嵌套摘录”")
        self.assertEqual(note.first_comment, "测试评述")

    def test_partial_correction_still_blocks_confirmation(self):
        preview = parse_markdown("“坏一\n\n“正常”\n\n“坏二")
        with self.assertRaises(ValueError):
            confirm_preview(preview)
        partial = correct_record(preview, 1, original_text="坏一", first_comment="")
        with self.assertRaises(ValueError):
            confirm_preview(partial)
        fixed = correct_record(partial, 3, original_text="坏二", first_comment="")
        self.assertEqual([note.original_text for note in confirm_preview(fixed)],
                         ["坏一", "正常", "坏二"])

    def test_invalid_correction_rejected(self):
        preview = parse_markdown("“坏")
        with self.assertRaises(ValueError):
            correct_record(preview, 1, original_text=" \n", first_comment="")
        with self.assertRaises(ValueError):
            correct_record(preview, 99, original_text="原文", first_comment="")
        with self.assertRaises(ValueError):
            correct_record(preview, 1, original_text="原文", first_comment=None)

    def test_json_serializable_preview(self):
        preview = parse_markdown("“坏\n\n“正常”")
        decoded = json.loads(json.dumps(asdict(preview), ensure_ascii=False))
        self.assertEqual(decoded["records"][0]["raw_source"], "“坏")
        self.assertEqual(decoded["records"][0]["issue"]["line"], 1)


@unittest.skipUnless(SAMPLE.is_file(), "Private Zotero fixture is not included in Git")
class RealSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = SAMPLE.read_bytes()
        cls.source = cls.raw.decode("utf-8")
        cls.preview = parse_markdown(cls.source)

    def test_fixture_integrity(self):
        self.assertEqual(len(self.raw), 57569)
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(),
                         "d41a3ac70b1f2d559f08aa51079bc114994a8c8c9530be9bbd86b47b466eb0f4")
        self.assertEqual(self.preview.sha256, hashlib.sha256(self.raw).hexdigest())

    def test_record_counts_and_known_anomalies(self):
        self.assertEqual(len(self.preview.headers), 2)
        self.assertEqual(len(self.preview.records), 154)
        issues = [(r.record_id, r.start_line, r.end_line, r.issue.code)
                  for r in self.preview.records if r.needs_correction]
        self.assertEqual(issues, [
            (3, 10, 10, "unclosed_quote"), (4, 12, 12, "unclosed_quote"),
            (15, 35, 35, "unclosed_quote"), (16, 37, 37, "unclosed_quote"),
            (32, 70, 71, "unclosed_quote"), (42, 94, 94, "unclosed_quote"),
            (56, 122, 122, "unclosed_quote"),
        ])
        with self.assertRaises(ValueError):
            confirm_preview(self.preview)

    def test_selected_fields_against_manually_read_sample(self):
        records = self.preview.records
        # Fixed digests retain the original regression assertions without
        # publishing the user's excerpts or personal comments in the test code.
        expected = (
            (0, "original_text", "fc2b754d3c8e8eae6d420235adcf9eae01b8bcc912233d9c7fefed2f1a65586d"),
            (4, "first_comment", "c263abdba9f791b9ae8590368528200f9d33e98f3668de54b76e871ecf2f2f76"),
            (5, "first_comment", "67cc2ba57331dea03256beff3efb9335b43acef994cefc6be0b7ccb8c2a5497e"),
            (7, "original_text", "167f4bae6b14420f4db3739debbfe401ea65de26a91f4dbf62a9382972157a24"),
            (117, "original_text", "d2ac30401b71e36d0365d5de7da71d2aed4baa82f3b299a17bafb262edb6aa85"),
        )
        for index, field, digest in expected:
            with self.subTest(record=index + 1, field=field):
                self.assertEqual(hashlib.sha256(getattr(records[index], field).encode()).hexdigest(), digest)
        self.assertEqual(records[0].first_comment, "")
        # The final standalone paragraph is a continuation under the approved rule.
        self.assertEqual(hashlib.sha256(records[-1].first_comment[-33:].encode()).hexdigest(),
                         "57af5a4932f5dbdb3435214bdc67e5d864852bfbbe0e6051f562ce0d55d554e6")

    def test_every_nonblank_source_character_is_retained(self):
        spans = sorted([*self.preview.headers, *self.preview.records],
                       key=lambda item: item.source_start)
        cursor = 0
        for item in spans:
            self.assertGreaterEqual(item.source_start, cursor)
            self.assertFalse(self.source[cursor:item.source_start].strip())
            self.assertEqual(item.raw_source, self.source[item.source_start:item.source_end])
            self.assertEqual(item.start_line, self.source.count("\n", 0, item.source_start) + 1)
            self.assertEqual(item.end_line, self.source.count("\n", 0, item.source_end) + 1)
            cursor = item.source_end
        self.assertFalse(self.source[cursor:].strip())


@unittest.skipUnless(SAMPLE.is_file(), "Private Zotero fixture is not included in Git")
class CommandLineTests(unittest.TestCase):
    def test_real_sample_returns_blocked_exit_status(self):
        result = subprocess.run([sys.executable, "-m", "noteshub.parser", str(SAMPLE)],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("记录 154；正常 147；异常 7", result.stdout)

    def test_json_includes_raw_anomaly_and_confirm_gate(self):
        result = subprocess.run([sys.executable, "-m", "noteshub.parser", str(SAMPLE), "--json"],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["can_confirm"])
        self.assertEqual(hashlib.sha256(payload["records"][3]["raw_source"].encode()).hexdigest(),
                         "2fcbd33d2d473782dcfdfe867fb4e71366d6ed52519b42b2388a4321ded4975a")


if __name__ == "__main__":
    unittest.main()
