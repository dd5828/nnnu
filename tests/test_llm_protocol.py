"""LLM 协议与 ScriptedLLM：流式块序、审计、耗尽防假绿、YAML 载入。"""

import pytest

from nnnu.services.llm.protocol import LLMRequest, LLMToolCall
from nnnu.services.llm.scripted import SCRIPTED_CHUNK_CHARS, ScriptedLLM, ScriptedStep


def _request() -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": "hi"}], model="m")


async def test_scripted_stream_chunk_order():
    llm = ScriptedLLM(
        [
            ScriptedStep(
                thinking=["先想"],
                chunks=["答案"],
                tool_calls=[],
                usage={"prompt_tokens": 10, "completion_tokens": 5},
            )
        ]
    )
    chunks = [c async for c in llm.stream(_request())]
    kinds = [
        "thinking"
        if c.thinking
        else "tool"
        if c.tool_call_delta
        else "text"
        if c.text
        else "usage"
        if c.usage
        else "finish"
        for c in chunks
    ]
    # 思考先行，正文其后，usage 与 finish_reason 收尾
    assert kinds[0] == "thinking"
    assert kinds[-2:] == ["usage", "finish"]


async def test_scripted_calls_audit():
    llm = ScriptedLLM([ScriptedStep(chunks=["a"]), ScriptedStep(chunks=["b"])])
    await llm.complete(_request())
    await llm.complete(_request())
    assert len(llm.calls) == 2
    assert llm.calls[0].model == "m"
    assert llm.calls[0].messages == [{"role": "user", "content": "hi"}]


async def test_scripted_exhausted_raises():
    llm = ScriptedLLM([ScriptedStep(chunks=["a"])])
    await llm.complete(_request())
    with pytest.raises(RuntimeError, match="脚本耗尽"):
        await llm.complete(_request())


async def test_scripted_complete_aggregates():
    llm = ScriptedLLM(
        [
            ScriptedStep(
                thinking=["t1", "t2"],
                chunks=["你好世界"],
                tool_calls=[LLMToolCall(id="c1", name="add", arguments='{"a":1}')],
                finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 4},
            )
        ]
    )
    response = await llm.complete(_request())
    assert response.thinking == "t1t2"
    assert response.text == "你好世界"
    assert response.finish_reason == "stop"
    assert response.usage == {"prompt_tokens": 3, "completion_tokens": 4}
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "add"
    assert response.tool_calls[0].arguments == '{"a":1}'


async def test_scripted_chunk_coalescing():
    long_text = "字" * (SCRIPTED_CHUNK_CHARS * 2 + 10)
    llm = ScriptedLLM([ScriptedStep(chunks=[long_text])])
    chunks = [c async for c in llm.stream(_request())]
    text_chunks = [c.text for c in chunks if c.text]
    assert text_chunks[0] == "字" * SCRIPTED_CHUNK_CHARS
    assert text_chunks[1] == "字" * SCRIPTED_CHUNK_CHARS
    assert text_chunks[2] == "字" * 10


async def test_scripted_from_yaml(tmp_path):
    yaml_file = tmp_path / "script.yaml"
    yaml_file.write_text(
        """
steps:
  - thinking: ["想"]
    chunks: ["答"]
    finish_reason: stop
    usage: {prompt_tokens: 2, completion_tokens: 1}
  - tool_calls:
      - {id: call-1, name: add, arguments: '{"a": 2}'}
    finish_reason: tool_calls
""".strip(),
        encoding="utf-8",
    )
    llm = ScriptedLLM.from_yaml(yaml_file)
    first = await llm.complete(_request())
    assert first.thinking == "想"
    assert first.usage == {"prompt_tokens": 2, "completion_tokens": 1}
    second = await llm.complete(_request())
    assert second.finish_reason == "tool_calls"
    assert second.tool_calls[0].name == "add"
