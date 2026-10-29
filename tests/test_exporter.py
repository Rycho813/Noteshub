import re
import unittest

from markdown_it import MarkdownIt

from noteshub.exporter import export_filename, literal_text, render_book


class ExporterTests(unittest.TestCase):
    def test_literal_markdown_and_html_round_trip(self):
        parser = MarkdownIt("commonmark")
        for text in (
            '# 标题 *强调* **加粗** [链接](https://example.com) ![图](x)',
            '<script>alert("x")</script> &copy; <!-- 隐藏 -->',
            '1. 列表\n- 项目\n> 引用\n---\n===\n```python',
            '普通行\n第二行\n\n第三段',
            '    四个空格不是代码\n\t制表符也不是代码',
            r'反斜杠 \ 、[[双链]]、$公式$、==标记==、%%评述%%',
        ):
            with self.subTest(text=text):
                tokens = parser.parse(literal_text(text))
                self.assertTrue(all(token.type in {"paragraph_open", "inline", "paragraph_close"} for token in tokens))
                paragraphs = []
                for token in tokens:
                    if token.type != "inline":
                        continue
                    self.assertTrue(all(child.type in {"text", "hardbreak", "softbreak"} for child in token.children))
                    paragraphs.append("".join(child.content if child.type == "text" else "\n" for child in token.children))
                self.assertEqual("\n\n".join(paragraphs), text)

    def test_book_structure_empty_fields_and_order(self):
        book = {"title": "书名 # <测试>", "author": "作者 & 名"}
        notes = [
            {"original_text": "原文“内引号”\n第二行", "first_comment": "", "second_comment": "", "status": "pending"},
            {"original_text": "另一条", "first_comment": "一读", "second_comment": "二读", "status": "done"},
        ]
        result = render_book(book, notes)
        headings = [token.content for token in MarkdownIt().parse(result) if token.type == "inline" and token.map and result.splitlines()[token.map[0]].startswith("#")]
        self.assertEqual(headings, [r"书名 \# &lt;测试&gt;", "笔记 1", "原文", "一读评论", "二读评论", "笔记 2", "原文", "一读评论", "二读评论"])
        self.assertIn("> 原文“内引号”  \n> 第二行", result)
        self.assertIn("### 一读评论\n\n\n\n### 二读评论", result)
        self.assertLess(result.index("原文“内引号”"), result.index("另一条"))
        self.assertEqual(len(re.findall(r"^## 笔记 \d+$", result, re.MULTILINE)), 2)
        self.assertNotIn("zotero://", result)
        self.assertTrue(result.endswith("\n"))

    def test_no_author_and_windows_newline(self):
        result = render_book({"title": "书", "author": ""}, [{"original_text": "第一行\r\n第二行", "first_comment": "", "second_comment": ""}])
        self.assertNotIn("作者：", result)
        self.assertNotIn("\r", result)
        self.assertIn("> 第一行  \n> 第二行", result)

    def test_export_filenames_are_portable(self):
        cases = {"论爱欲": "论爱欲.md", 'A/B:C*D?E"F<G>H|I\\J': "A_B_C_D_E_F_G_H_I_J.md", "...": "未命名书籍.md",
                 "CON": "_CON.md", "nul.txt": "_nul.txt.md", " 书名. ": "书名.md"}
        for title, expected in cases.items():
            with self.subTest(title=title):
                self.assertEqual(export_filename(title), expected)
        long_name = export_filename("📚" * 200)
        self.assertLessEqual(len(long_name.encode("utf-8")), 183)
        self.assertTrue(long_name.endswith(".md"))


if __name__ == "__main__":
    unittest.main()
