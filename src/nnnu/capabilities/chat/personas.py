"""persona 预设与解析（§7.1）：teacher / peer / research-assistant + 自定义。

- 预设行为文本在 prompts/{en,zh}/chat.yaml 的 `personas.<id>.text`（双语，随输出语言加载）；
- 自定义 persona（id=custom）的行为描述由用户提供，粘性存于 session.persona_description（§6.7）；
- 前端显示名归 web/locales（§10.2 界面文案），本模块不承载 UI 文案。
"""

from typing import Any

PRESET_PERSONAS = ("teacher", "peer", "research_assistant")
CUSTOM_PERSONA = "custom"


def is_valid_persona(persona_id: str) -> bool:
    """persona id 是否合法：预设之一或 custom。"""
    return persona_id in PRESET_PERSONAS or persona_id == CUSTOM_PERSONA


def resolve_persona_text(
    *, persona_id: str, description: str | None, prompts: Any, lang: str
) -> str:
    """把 persona 引用解析为注入 system prompt 的行为文本。

    - 预设：从提示词 YAML 按语言渲染（键缺失回退英文后返回空串，不致命）；
    - custom：直接用用户描述；描述缺失时返回空串（无行为约束）。
    """
    if persona_id == CUSTOM_PERSONA:
        return (description or "").strip()
    return prompts.render("chat", lang, f"personas.{persona_id}.text").strip()
