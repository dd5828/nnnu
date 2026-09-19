"""能力协议：manifest 校验与空 stages 的自由循环语义。"""

import pytest
from pydantic import ValidationError

from nnnu.core.capability_protocol import CapabilityManifest, Stage


def test_stage_defaults():
    stage = Stage(key="planning", label_i18n="stage.planning")
    assert stage.max_rounds == 20
    assert stage.max_tokens == 4096


def test_manifest_empty_stages_means_free_loop():
    manifest = CapabilityManifest(name="chat", version="1.0.0")
    assert manifest.stages == []
    assert manifest.default_model_role == "chat"
    assert manifest.config_schema == {}


def test_manifest_with_stages():
    manifest = CapabilityManifest(
        name="deep_solve",
        version="1.0.0",
        stages=[
            Stage(key="planning", label_i18n="stage.planning", max_rounds=5),
            Stage(key="reasoning", label_i18n="stage.reasoning", max_rounds=10),
        ],
    )
    assert [s.key for s in manifest.stages] == ["planning", "reasoning"]


def test_manifest_name_required():
    with pytest.raises(ValidationError):
        CapabilityManifest()  # type: ignore[call-arg]
