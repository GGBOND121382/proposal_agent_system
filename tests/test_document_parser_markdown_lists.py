from __future__ import annotations

from app.documents import parse_document


def test_markdown_ordered_list_stays_in_section_body():
    parsed = parse_document(
        "guide.md",
        (
            "# 测试说明\n"
            "本材料仅用于运行基线验证。\n\n"
            "## 输出要求\n"
            "1. 输出必须结构化、可追溯。\n"
            "2. 不得补造未提供的信息。\n"
            "3. 每个结论应绑定输入来源。\n"
        ).encode("utf-8"),
        "APPLICATION_GUIDE",
        "INTERNAL",
    )

    titles = [section["title"] for section in parsed["sections"]]
    assert titles == ["全文", "测试说明", "输出要求"]
    output_section = parsed["sections"][-1]
    assert output_section["text"].splitlines() == [
        "1. 输出必须结构化、可追溯。",
        "2. 不得补造未提供的信息。",
        "3. 每个结论应绑定输入来源。",
    ]
    assert output_section["text_hash"] != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_plain_text_numbered_headings_remain_supported():
    parsed = parse_document(
        "outline.txt",
        "一、总体要求\n正文甲。\n1.1 技术要求\n正文乙。".encode("utf-8"),
        "APPLICATION_GUIDE",
        "INTERNAL",
    )
    assert [section["title"] for section in parsed["sections"]] == [
        "全文",
        "一、总体要求",
        "1.1 技术要求",
    ]
    assert parsed["sections"][1]["text"] == "正文甲。"
    assert parsed["sections"][2]["text"] == "正文乙。"
