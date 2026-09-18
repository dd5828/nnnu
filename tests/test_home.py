"""home 解析：NNNU_HOME 优先级与 data/prompts 根目录。"""

from pathlib import Path

from nnnu.runtime.home import get_data_root, get_prompts_root, get_runtime_home


def test_env_home_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("NNNU_HOME", str(tmp_path))
    assert get_runtime_home() == tmp_path.resolve()


def test_explicit_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NNNU_HOME", str(tmp_path / "env"))
    explicit = tmp_path / "explicit"
    assert get_runtime_home(explicit) == explicit.resolve()


def test_cwd_fallback(monkeypatch):
    monkeypatch.delenv("NNNU_HOME", raising=False)
    assert get_runtime_home() == Path.cwd()


def test_data_root(tmp_path):
    assert get_data_root(tmp_path) == tmp_path / "data"


def test_prompts_root_prefers_home_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NNNU_HOME", str(tmp_path))
    (tmp_path / "prompts").mkdir()
    assert get_prompts_root() == tmp_path / "prompts"


def test_prompts_root_missing_home_dir_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("NNNU_HOME", str(tmp_path))
    # home 无 prompts 目录时不抛异常（加载器对缺失目录降级处理）
    assert isinstance(get_prompts_root(), Path)
