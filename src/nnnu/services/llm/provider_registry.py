"""LLM provider 静态注册表（§6.10）：顺序即匹配优先级。

- 每个 spec 声明接入方式（base_url/密钥环境变量/探测关键词/本地标记）；
- 模型列表是内置快照（动态 /models 探测 P2 接入）；
- resolve_provider 解析优先级：显式 id > base_url 关键词 > key 前缀 > 无匹配 None。
"""

from pydantic import BaseModel, Field


class ModelInfo(BaseModel):
    id: str
    label: str = ""
    context_window: int | None = None
    capabilities: set[str] = Field(default_factory=lambda: {"chat", "tool_calling"})
    temperature_max: float | None = None
    reasoning_effort: str | None = None


class ProviderSpec(BaseModel):
    id: str
    label: str
    base_url: str | None = None  # OpenAI 兼容端点的 /v1 根
    api_key_env: str | None = None  # 密钥只存环境变量名，key 本身永不落配置
    models: list[ModelInfo] = Field(default_factory=list)
    capabilities: set[str] = Field(default_factory=lambda: {"chat", "tool_calling"})
    detect_base_keyword: str | None = None  # base_url 关键词探测（如 "11434"→ollama）
    key_prefix: str | None = None  # API key 前缀探测
    is_local: bool = False  # 本地端点免密钥


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        api_key_env="DEEPSEEK_API_KEY",
        detect_base_keyword="deepseek",
        models=[
            ModelInfo(id="deepseek-chat", context_window=131072, temperature_max=1.5),
            ModelInfo(id="deepseek-reasoner", context_window=131072),
        ],
    ),
    ProviderSpec(
        id="kimi",
        label="Kimi (Moonshot)",
        base_url="https://api.moonshot.cn/v1",
        api_key_env="MOONSHOT_API_KEY",
        detect_base_keyword="moonshot",
        models=[
            ModelInfo(id="kimi-k2", context_window=131072),
            ModelInfo(id="moonshot-v1-8k", context_window=8192, temperature_max=1.0),
        ],
    ),
    ProviderSpec(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
        detect_base_keyword="openai",
        key_prefix="sk-",
        models=[
            ModelInfo(id="gpt-4o", context_window=131072),
            ModelInfo(id="gpt-4o-mini", context_window=131072),
        ],
    ),
    ProviderSpec(
        id="ollama",
        label="Ollama (本地)",
        base_url="http://localhost:11434/v1",
        detect_base_keyword="11434",
        is_local=True,
        models=[ModelInfo(id="qwen3")],
    ),
    ProviderSpec(
        id="lmstudio",
        label="LM Studio (本地)",
        base_url="http://localhost:1234/v1",
        detect_base_keyword="lmstudio",
        is_local=True,
        models=[],
    ),
    ProviderSpec(
        id="vllm",
        label="vLLM (本地)",
        base_url="http://localhost:8000/v1",
        detect_base_keyword="vllm",
        is_local=True,
        models=[],
    ),
)


def build_registry() -> tuple[ProviderSpec, ...]:
    return PROVIDERS


def find_by_id(specs: tuple[ProviderSpec, ...], provider_id: str) -> ProviderSpec | None:
    for spec in specs:
        if spec.id == provider_id:
            return spec
    return None


def find_model(spec: ProviderSpec, model: str) -> ModelInfo | None:
    for info in spec.models:
        if info.id == model:
            return info
    return None


def resolve_provider(
    specs: tuple[ProviderSpec, ...],
    *,
    provider_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> ProviderSpec | None:
    """显式 id > base_url 关键词 > key 前缀；全不中返回 None（调用方报配置错）。"""
    if provider_id is not None:
        return find_by_id(specs, provider_id)
    if base_url:
        lowered = base_url.lower()
        for spec in specs:
            if spec.detect_base_keyword and spec.detect_base_keyword in lowered:
                return spec
    if api_key:
        for spec in specs:
            if spec.key_prefix and api_key.startswith(spec.key_prefix):
                return spec
    return None


def default_model(spec: ProviderSpec) -> str | None:
    return spec.models[0].id if spec.models else None
