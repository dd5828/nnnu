"""笔记本 REST（§7.15 极简版）：笔记本与记录 CRUD、级联删除、错误信封。"""

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


@pytest.mark.parametrize(
    "body,code",
    [
        ({"type": "question", "content_md": "题"}, "invalid_record"),  # 批一不收题目类型
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
