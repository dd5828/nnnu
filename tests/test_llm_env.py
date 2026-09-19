"""手写 .env 加载：不覆盖已有环境变量、引号与注释。"""

from nnnu.services.llm.env import load_dotenv


def test_dotenv_loads_without_override(tmp_path, monkeypatch):
    monkeypatch.setenv("EXISTING_KEY", "from-env")
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        """
# 注释行
EXISTING_KEY=from-file
NEW_KEY="带引号的值"
SINGLE='单引号'
EMPTY=
""",
        encoding="utf-8",
    )
    loaded = load_dotenv(dotenv)
    import os

    assert loaded["NEW_KEY"] == "带引号的值"
    assert loaded["SINGLE"] == "单引号"
    assert "EMPTY" not in loaded  # 空值不写入
    assert os.environ["EXISTING_KEY"] == "from-env"  # 已存在的环境变量不被覆盖
    assert "EXISTING_KEY" not in loaded


def test_dotenv_missing_file_returns_empty(tmp_path):
    assert load_dotenv(tmp_path / "nope.env") == {}


def test_dotenv_skips_malformed_lines(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("NO_EQUALS_LINE\nA=1 # 行尾注释按字面保留\n", encoding="utf-8")
    loaded = load_dotenv(dotenv)
    assert loaded == {"A": "1 # 行尾注释按字面保留"}
