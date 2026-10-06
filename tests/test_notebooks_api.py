"""笔记本 REST（§7.15 正式版）：笔记本与记录 CRUD、编辑/移动/复制/导出、错误信封。"""

import pytest


async def _new_notebook(client, name: str = "我的笔记本", description: str | None = None) -> dict:
    response = await client.post(
        "/api/v1/notebooks", json={"name": name, "description": description}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_notebook_crud_round_trip(client):
    notebook = await _new_notebook(client, "信号与系统", "课本例题")
    assert notebook["id"].startswith("nb-")
    assert notebook["name"] == "信号与系统"
    assert notebook["description"] == "课本例题"

    listed = (await client.get("/api/v1/notebooks")).json()["notebooks"]
    assert [item["id"] for item in listed] == [notebook["id"]]
    assert listed[0]["record_count"] == 0

    detail = (await client.get(f"/api/v1/notebooks/{notebook['id']}")).json()
    assert detail["records"] == []

    assert (await client.delete(f"/api/v1/notebooks/{notebook['id']}")).json() == {
        "deleted": notebook["id"]
    }
    assert (await client.get(f"/api/v1/notebooks/{notebook['id']}")).status_code == 404
    assert (await client.delete(f"/api/v1/notebooks/{notebook['id']}")).status_code == 404
    assert (await client.get("/api/v1/notebooks")).json()["notebooks"] == []


@pytest.mark.parametrize("name", ["", "   ", "a" * 61, "带\x01控制字符"])
async def test_create_notebook_rejects_bad_name(client, name):
    response = await client.post("/api/v1/notebooks", json={"name": name})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "invalid_name"
    assert body["error"]["recoverable"] is False


async def test_record_add_list_delete(client):
    notebook = await _new_notebook(client)
    created = await client.post(
        f"/api/v1/notebooks/{notebook['id']}/records",
        json={
            "type": "solve",
            "content_md": "# 求导\n\n$d/dx\\sin(x^2) = 2x\\cos(x^2)$",
            "source_ref": "sess-abc",
        },
    )
    assert created.status_code == 200, created.text
    record = created.json()
    assert record["id"].startswith("nbr-")
    assert record["type"] == "solve"
    assert record["source_ref"] == "sess-abc"
    # 没给标题 → 取正文首个非空行，且剥掉 markdown 标题符号
    assert record["title"] == "求导"

    listed = (await client.get(f"/api/v1/notebooks/{notebook['id']}")).json()
    assert [item["id"] for item in listed["records"]] == [record["id"]]
    assert (await client.get("/api/v1/notebooks")).json()["notebooks"][0]["record_count"] == 1

    assert (
        await client.delete(f"/api/v1/notebooks/{notebook['id']}/records/{record['id']}")
    ).json() == {"deleted": record["id"]}
    assert (await client.get(f"/api/v1/notebooks/{notebook['id']}")).json()["records"] == []


async def test_record_explicit_title_wins_and_is_truncated(client):
    notebook = await _new_notebook(client)
    record = (
        await client.post(
            f"/api/v1/notebooks/{notebook['id']}/records",
            json={"type": "note", "title": "手写标题", "content_md": "正文第一行\n正文第二行"},
        )
    ).json()
    assert record["title"] == "手写标题"

    long_title = "长" * 200
    record = (
        await client.post(
            f"/api/v1/notebooks/{notebook['id']}/records",
            json={"type": "note", "title": long_title, "content_md": "x"},
        )
    ).json()
    assert len(record["title"]) == 120


async def test_record_accepts_question_type(client):
    """批二把「题目」记录类型补上了（批一预留的坑）：出题回合可以存进笔记本。"""
    notebook = await _new_notebook(client)
    created = await client.post(
        f"/api/v1/notebooks/{notebook['id']}/records",
        json={
            "type": "question",
            "content_md": "### 第 1 题\n\n求 $x^2$ 的导数",
            "source_ref": "sess-q",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["type"] == "question"


async def test_record_accepts_research_type(client):
    """批三把「研究」记录类型补上了（P6 §7.6）：调研报告能一键存进笔记本。"""
    notebook = await _new_notebook(client)
    created = await client.post(
        f"/api/v1/notebooks/{notebook['id']}/records",
        json={
            "type": "research",
            "content_md": "## 研究报告\n\n综合结论：…\n\n## 参考资料\n\n1. [甲](https://a.example)",
            "source_ref": "sess-r",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["type"] == "research"


@pytest.mark.parametrize(
    "body,code",
    [
        ({"type": "quiz", "content_md": "题"}, "invalid_record"),  # 白名单外的类型
        ({"type": "note", "content_md": "   "}, "invalid_record"),  # 空内容
    ],
)
async def test_record_rejections(client, body, code):
    notebook = await _new_notebook(client)
    response = await client.post(f"/api/v1/notebooks/{notebook['id']}/records", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


async def test_record_on_missing_notebook_404(client):
    response = await client.post(
        "/api/v1/notebooks/nb-nope/records", json={"type": "note", "content_md": "x"}
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_delete_notebook_cascades_records(client):
    notebook = await _new_notebook(client)
    await client.post(
        f"/api/v1/notebooks/{notebook['id']}/records",
        json={"type": "chat", "content_md": "一次问答"},
    )
    assert (await client.delete(f"/api/v1/notebooks/{notebook['id']}")).status_code == 200
    # 记录随笔记本一起没了：再查同一个记录 id 也是 404（外键级联）
    assert (
        await client.delete(f"/api/v1/notebooks/{notebook['id']}/records/nbr-x")
    ).status_code == 404
    assert (await client.get("/api/v1/notebooks")).json()["notebooks"] == []


async def test_record_delete_scoped_to_notebook(client):
    first = await _new_notebook(client, "甲")
    second = await _new_notebook(client, "乙")
    record = (
        await client.post(
            f"/api/v1/notebooks/{first['id']}/records",
            json={"type": "note", "content_md": "只在甲里"},
        )
    ).json()
    # 拿甲的记录 id 去乙里删：删不掉，也不该把甲的那条带走
    assert (
        await client.delete(f"/api/v1/notebooks/{second['id']}/records/{record['id']}")
    ).status_code == 404
    assert len((await client.get(f"/api/v1/notebooks/{first['id']}")).json()["records"]) == 1


async def test_patch_notebook_updates_name_and_description(client):
    notebook = await _new_notebook(client, "旧名", "旧描述")
    updated = (
        await client.patch(
            f"/api/v1/notebooks/{notebook['id']}", json={"name": "新名", "description": ""}
        )
    ).json()
    assert updated["name"] == "新名"
    assert updated["description"] is None  # 空串 = 清空
    # 只给一个字段时另一个不动
    again = (
        await client.patch(f"/api/v1/notebooks/{notebook['id']}", json={"description": "回来了"})
    ).json()
    assert again["name"] == "新名"
    assert again["description"] == "回来了"


async def test_patch_notebook_rejections(client):
    notebook = await _new_notebook(client)
    empty = await client.patch(f"/api/v1/notebooks/{notebook['id']}", json={})
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "invalid_name"
    blank = await client.patch(f"/api/v1/notebooks/{notebook['id']}", json={"name": "  "})
    assert blank.status_code == 422
    missing = await client.patch("/api/v1/notebooks/nb-nope", json={"name": "x"})
    assert missing.status_code == 404


async def test_patch_record_edits_title_and_body(client):
    notebook = await _new_notebook(client)
    record = (
        await client.post(
            f"/api/v1/notebooks/{notebook['id']}/records",
            json={"type": "note", "title": "旧标题", "content_md": "旧正文"},
        )
    ).json()
    edited = (
        await client.patch(
            f"/api/v1/notebooks/{notebook['id']}/records/{record['id']}",
            json={"title": "新标题", "content_md": "# 新正文\n\n第二行"},
        )
    ).json()
    assert edited["title"] == "新标题"
    assert edited["content_md"] == "# 新正文\n\n第二行"
    # title 传空串 = 按正文首行重取（剥 markdown 符号）
    rederived = (
        await client.patch(
            f"/api/v1/notebooks/{notebook['id']}/records/{record['id']}",
            json={"title": ""},
        )
    ).json()
    assert rederived["title"] == "新正文"
    # 只改正文不动标题
    body_only = (
        await client.patch(
            f"/api/v1/notebooks/{notebook['id']}/records/{record['id']}",
            json={"content_md": "再换一次"},
        )
    ).json()
    assert body_only["title"] == "新正文"
    assert body_only["content_md"] == "再换一次"


async def test_patch_record_rejections(client):
    first = await _new_notebook(client, "甲")
    second = await _new_notebook(client, "乙")
    record = (
        await client.post(
            f"/api/v1/notebooks/{first['id']}/records", json={"type": "note", "content_md": "x"}
        )
    ).json()
    empty = await client.patch(f"/api/v1/notebooks/{first['id']}/records/{record['id']}", json={})
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "invalid_record"
    blank = await client.patch(
        f"/api/v1/notebooks/{first['id']}/records/{record['id']}", json={"content_md": "   "}
    )
    assert blank.status_code == 422
    wrong_home = await client.patch(
        f"/api/v1/notebooks/{second['id']}/records/{record['id']}", json={"title": "x"}
    )
    assert wrong_home.status_code == 404
    missing_target = await client.patch(
        f"/api/v1/notebooks/{first['id']}/records/{record['id']}",
        json={"target_notebook_id": "nb-nope"},
    )
    assert missing_target.status_code == 404


async def test_move_record_keeps_id_and_switches_notebook(client):
    """@ 引用不失效的根：移动只换归属、id 不变，内容原样。"""
    first = await _new_notebook(client, "甲")
    second = await _new_notebook(client, "乙")
    record = (
        await client.post(
            f"/api/v1/notebooks/{first['id']}/records",
            json={"type": "chat", "content_md": "要移动的记录", "source_ref": "sess-m"},
        )
    ).json()
    moved = (
        await client.patch(
            f"/api/v1/notebooks/{first['id']}/records/{record['id']}",
            json={"target_notebook_id": second["id"]},
        )
    ).json()
    assert moved["id"] == record["id"]
    assert moved["notebook_id"] == second["id"]
    assert moved["content_md"] == "要移动的记录"
    assert moved["source_ref"] == "sess-m"
    assert (await client.get(f"/api/v1/notebooks/{first['id']}")).json()["records"] == []
    listed = (await client.get(f"/api/v1/notebooks/{second['id']}")).json()["records"]
    assert [item["id"] for item in listed] == [record["id"]]


async def test_move_and_edit_in_one_patch(client):
    first = await _new_notebook(client, "甲")
    second = await _new_notebook(client, "乙")
    record = (
        await client.post(
            f"/api/v1/notebooks/{first['id']}/records",
            json={"type": "note", "title": "旧", "content_md": "旧正文"},
        )
    ).json()
    updated = (
        await client.patch(
            f"/api/v1/notebooks/{first['id']}/records/{record['id']}",
            json={"target_notebook_id": second["id"], "title": "新", "content_md": "新正文"},
        )
    ).json()
    assert updated["notebook_id"] == second["id"]
    assert updated["title"] == "新"
    assert updated["content_md"] == "新正文"


async def test_copy_record_to_other_notebook(client):
    first = await _new_notebook(client, "甲")
    second = await _new_notebook(client, "乙")
    record = (
        await client.post(
            f"/api/v1/notebooks/{first['id']}/records",
            json={
                "type": "research",
                "title": "报告",
                "content_md": "报告正文",
                "source_ref": "sess-c",
            },
        )
    ).json()
    copied = (
        await client.post(
            f"/api/v1/notebooks/{first['id']}/records/{record['id']}/copy",
            json={"target_notebook_id": second["id"]},
        )
    ).json()
    assert copied["id"].startswith("nbr-") and copied["id"] != record["id"]
    assert copied["notebook_id"] == second["id"]
    assert (copied["title"], copied["content_md"], copied["type"]) == (
        "报告",
        "报告正文",
        "research",
    )
    # 原记录不动；缺省复制回原笔记本
    assert len((await client.get(f"/api/v1/notebooks/{first['id']}")).json()["records"]) == 1
    same = (
        await client.post(f"/api/v1/notebooks/{first['id']}/records/{record['id']}/copy")
    ).json()
    assert same["notebook_id"] == first["id"]
    assert len((await client.get(f"/api/v1/notebooks/{first['id']}")).json()["records"]) == 2


async def test_get_record_globally_with_notebook_name(client):
    notebook = await _new_notebook(client, "定位本")
    record = (
        await client.post(
            f"/api/v1/notebooks/{notebook['id']}/records",
            json={"type": "note", "content_md": "找得到"},
        )
    ).json()
    found = (await client.get(f"/api/v1/notebooks/records/{record['id']}")).json()
    assert found["id"] == record["id"]
    assert found["notebook_name"] == "定位本"
    assert (await client.get("/api/v1/notebooks/records/nbr-nope")).status_code == 404


async def test_export_notebook_markdown(client):
    notebook = await _new_notebook(client, "导出本", "一段描述")
    for title, body in (("第一条", "正文一"), ("第二条", "正文二")):
        await client.post(
            f"/api/v1/notebooks/{notebook['id']}/records",
            json={"type": "note", "title": title, "content_md": body},
        )
    response = await client.get(f"/api/v1/notebooks/{notebook['id']}/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert f'filename="{notebook["id"]}.md"' in response.headers["content-disposition"]
    text = response.text
    assert text.startswith("# 导出本")
    assert "一段描述" in text
    assert "## 第一条" in text and "正文一" in text
    assert "## 第二条" in text and "正文二" in text
    assert (await client.get("/api/v1/notebooks/nb-nope/export")).status_code == 404
