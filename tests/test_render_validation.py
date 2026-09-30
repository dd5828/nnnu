"""可视化产出校验（§7.7）：确定性校验器、围栏抠取与兜底路由。"""

from nnnu.services.render import validation as V

SAMPLE_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
    '<circle cx="5" cy="5" r="4" /></svg>'
)
SAMPLE_ECHARTS = '{"xAxis": {"type": "category"}, "series": [{"type": "line", "data": [1, 2]}]}'


# ---- SVG ----


def test_svg_accepts_valid_markup():
    ok, reason = V.validate_visualization(SAMPLE_SVG, "svg")
    assert ok, reason


def test_svg_rejects_broken_xml():
    ok, reason = V.validate_visualization("<svg><circle></svg>", "svg")
    assert not ok and "XML" in reason


def test_svg_rejects_wrong_root():
    ok, reason = V.validate_visualization("<div>hi</div>", "svg")
    assert not ok and "svg" in reason


# ---- ECharts ----


def test_echarts_accepts_option_with_series():
    ok, reason = V.validate_visualization(SAMPLE_ECHARTS, "echarts")
    assert ok, reason


def test_echarts_rejects_invalid_json():
    ok, reason = V.validate_visualization("{series: [1,2]}", "echarts")
    assert not ok and "JSON" in reason


def test_echarts_rejects_missing_series():
    ok, reason = V.validate_visualization('{"title": {"text": "空"}}', "echarts")
    assert not ok and "series" in reason


# ---- Mermaid ----


def test_mermaid_accepts_flowchart_and_skips_comment_lines():
    ok, reason = V.validate_visualization("%% 说明\nflowchart TD\n  A --> B", "mermaid")
    assert ok, reason


def test_mermaid_accepts_state_diagram_v2_suffix():
    ok, reason = V.validate_visualization("stateDiagram-v2\n  [*] --> A", "mermaid")
    assert ok, reason


def test_mermaid_rejects_unknown_first_line():
    ok, reason = V.validate_visualization("画个图：\nflowchart TD", "mermaid")
    assert not ok and "关键字" in reason


# 标签禁忌字符：每组用例都在真 mermaid 解析器上验证过（通过/报错），别凭直觉改
_MERMAID_SHAPE_LABELS_OK = (
    "  A([开始]) --> B([结束])",  # 体育场
    "  A[[开始]] --> B[ok]",  # 双边方框
    "  A[(子过程)] --> B[ok]",  # 子程序
    "  A[/输入数组/] --> B[ok]",  # 斜切
    "  A{{六边形}} --> B[ok]",
    "  A((圆)) --> B[ok]",
    "  A>不对称] --> B[ok]",
    "  A[交换 a 与 b<br/>swapped = true] --> B[ok]",
    '  A["swap arr[j]"] --> B[ok]',  # 引号里的方括号没事
    "  A -- 是[1] --> B[ok]",  # 边标签的方括号没事
    "  A[x > y] --> B[ok]",  # 尖括号在标签里没事
    "  classDef term fill:#e8f0fe,stroke:#2f6fd0;",  # classDef 行不该被当节点
)


def test_mermaid_accepts_shape_syntax_and_quoted_labels():
    for line in _MERMAID_SHAPE_LABELS_OK:
        ok, reason = V.validate_visualization(f"flowchart TD\n{line}\n", "mermaid")
        assert ok, f"{line} 应通过：{reason}"


def test_mermaid_rejects_unquoted_brackets_in_label():
    # 真机验收抓到的原样失败片段：`arr[j]` 让解析器在 H{...} 处直接报错
    code = "flowchart TD\n  H{arr[j] > arr[j+1] ?}\n"
    ok, reason = V.validate_visualization(code, "mermaid")
    assert not ok and "标签" in reason and "'[]'" in reason


def test_mermaid_rejects_pipe_and_parens_in_label():
    for body in ("  A[a | b] --> B[ok]", "  H{arr(j) > 1 ?}", "  A[f(x) = 2x] --> B[ok]"):
        ok, reason = V.validate_visualization(f"flowchart TD\n{body}\n", "mermaid")
        assert not ok, f"{body} 应被拦下"


def test_mermaid_label_check_scoped_to_flowcharts():
    # classDiagram 的类体括号是语法本体，不做标签禁忌检查
    ok, reason = V.validate_visualization("classDiagram\n  class Animal {\n  }", "mermaid")
    assert ok, reason


# ---- HTML ----


def test_html_accepts_structural_tag():
    ok, reason = V.validate_visualization("<div><button>+</button></div>", "html")
    assert ok, reason


def test_html_rejects_plain_text():
    ok, reason = V.validate_visualization("这里是一段说明文字", "html")
    assert not ok and "结构标签" in reason


def test_unknown_render_type_rejected():
    ok, reason = V.validate_visualization(SAMPLE_SVG, "manim_video")
    assert not ok and "未知渲染类型" in reason


# ---- 围栏抠取 ----


def test_extract_fenced_tolerates_unclosed_block():
    text = "这是图：\n```svg\n<svg></svg>"
    assert V.extract_fenced(text, ("svg",)) == "<svg></svg>"


def test_extract_fenced_returns_none_when_lang_absent():
    assert V.extract_fenced("```mermaid\ngraph TD\n```", ("svg",)) is None


def test_extract_render_code_accepts_json_alias_for_echarts():
    text = '说明\n```json\n{"series": []}\n```\n'
    assert V.extract_render_code(text, "echarts") == '{"series": []}'


def test_extract_artifact_json_reads_payload():
    text = 'done\n```nnnu-artifact\n{"url": "/api/v1/renders/rnd-00112233", "kind": "video"}\n```'
    payload = V.extract_artifact_json(text)
    assert payload is not None and payload["kind"] == "video"


def test_extract_artifact_json_rejects_garbage():
    assert V.extract_artifact_json("```nnnu-artifact\n不是 JSON\n```") is None


# ---- 分析段解析与兜底路由 ----


def test_parse_analysis_finds_json_inside_fence_and_prose():
    text = '先看需求\n```json\n{"render_type": "mermaid", "title": "流程"}\n```\n以上'
    data = V.parse_analysis(text)
    assert data["render_type"] == "mermaid"


def test_parse_analysis_returns_empty_on_garbage():
    assert V.parse_analysis("模型今天不想输出 JSON") == {}


def test_guess_render_type_routes_by_keywords():
    assert V.guess_render_type("画一个流程图") == "mermaid"
    assert V.guess_render_type("把 sin 函数画成图表") == "echarts"
    assert V.guess_render_type("做一个可交互的演示") == "html"
    assert V.guess_render_type("给我看看这个动画") == "manim_video"
    assert V.guess_render_type("画个结构示意") == "svg"
    assert V.guess_render_type("随便来点东西") == "svg"  # 兜底


# ---- manim 场景类与兜底 HTML ----


def test_extract_scene_class_supports_qualified_base():
    code = "class Demo(ThreeDScene):\n    pass\n"
    assert V.extract_scene_class(code) == "Demo"
    assert V.extract_scene_class("import manim\n") is None


def test_build_fallback_html_escapes_and_keeps_note():
    page = V.build_fallback_html("<脚本>", "降级说明 & 尾巴")
    assert "&lt;脚本&gt;" in page and "降级说明 &amp; 尾巴" in page
    assert page.startswith("<!doctype html>")
