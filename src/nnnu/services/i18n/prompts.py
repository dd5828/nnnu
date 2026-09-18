"""提示词加载器（§10.1）：prompts/{en,zh}/{name}.yaml。

- 语言回退链：zh → en，未知语言最终落到 en；全部缺失返回空、只告警不致命；
- 占位符 {name} + str.format；渲染失败静默返回原文；
- 启动时校验中英文件名与键集合（递归）一致，差异仅告警。
"""

import logging
from pathlib import Path
from typing import Any

import yaml

from nnnu.runtime import home

logger = logging.getLogger(__name__)

LANGUAGE_FALLBACKS: dict[str, tuple[str, ...]] = {"zh": ("zh", "en"), "en": ("en",)}


def _all_keys(data: dict[str, Any], prefix: str = "") -> set[str]:
    """递归展开 dict 的键路径集合（如 stage_templates.planning）。"""
    keys: set[str] = set()
    for key, value in data.items():
        full = f"{prefix}{key}"
        keys.add(full)
        if isinstance(value, dict):
            keys |= _all_keys(value, full + ".")
    return keys


class PromptManager:
    """提示词管理器：按语言加载 YAML 并渲染模板。

    单例经 get_prompt_manager() 获取；测试可传显式 prompts 根目录。
    """

    def __init__(self, prompts_root: Path | None = None) -> None:
        self._root = prompts_root
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}

    @property
    def root(self) -> Path:
        return self._root if self._root is not None else home.get_prompts_root()

    def _fallback_chain(self, lang: str) -> list[str]:
        chain = list(LANGUAGE_FALLBACKS.get(lang, (lang, "en")))
        seen: list[str] = []
        for code in chain:
            if code not in seen:
                seen.append(code)
        return seen

    def load(self, name: str, lang: str) -> dict[str, Any]:
        """按回退链加载整文件；全部缺失返回 {} + 告警，不致命。"""
        for code in self._fallback_chain(lang):
            data = self._load_file(name, code)
            if data:
                return data
        logger.warning("提示词 %s 在 %s 中均缺失", name, self._fallback_chain(lang))
        return {}

    def _load_file(self, name: str, lang: str) -> dict[str, Any]:
        key = (name, lang)
        if key in self._cache:
            return self._cache[key]
        path = self.root / lang / f"{name}.yaml"
        if not path.is_file():
            return {}  # 空结果不缓存，便于文件补齐后重试
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            logger.warning("提示词 YAML 解析失败: %s", path)
            return {}
        data = raw if isinstance(raw, dict) else {}
        self._cache[key] = data
        return data

    def render(self, prompt: str, lang: str, key: str, **vars: Any) -> str:
        """按 dot-path 取模板并 str.format；键缺失逐语言回退，渲染失败返回原文。

        参数名取 prompt 而非 name：提示词占位符常用 {name}，避免关键字冲突。
        """
        for code in self._fallback_chain(lang):
            text = self._lookup(prompt, code, key)
            if text is None:
                continue
            try:
                return text.format(**vars)
            except (KeyError, IndexError, ValueError):
                logger.warning("提示词渲染失败（占位符与参数不匹配）: %s/%s %s", prompt, code, key)
                return text
        logger.warning("提示词键缺失: %s/%s %s", prompt, lang, key)
        return ""

    def _lookup(self, name: str, lang: str, key: str) -> str | None:
        node: Any = self._load_file(name, lang)
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node if isinstance(node, str) else None

    def check_parity(self) -> list[str]:
        """校验 prompts/{en,zh} 文件名与键集合一致，返回差异描述列表（仅告警）。"""
        issues: list[str] = []
        if not self.root.is_dir():
            return issues
        en_names = self._yaml_names("en")
        zh_names = self._yaml_names("zh")
        for name in sorted(en_names - zh_names):
            issues.append(f"zh 缺少提示词文件 {name}")
        for name in sorted(zh_names - en_names):
            issues.append(f"en 缺少提示词文件 {name}")
        for name in sorted(en_names & zh_names):
            en_keys = _all_keys(self._load_file(name, "en"))
            zh_keys = _all_keys(self._load_file(name, "zh"))
            for missing in sorted(en_keys - zh_keys):
                issues.append(f"{name}: zh 缺键 {missing}")
            for extra in sorted(zh_keys - en_keys):
                issues.append(f"{name}: en 缺键 {extra}")
        return issues

    def _yaml_names(self, lang: str) -> set[str]:
        lang_dir = self.root / lang
        if not lang_dir.is_dir():
            return set()
        return {path.stem for path in lang_dir.glob("*.yaml")}


_prompt_manager: PromptManager | None = None


def get_prompt_manager() -> PromptManager:
    global _prompt_manager
    if _prompt_manager is None:
        _prompt_manager = PromptManager()
    return _prompt_manager
