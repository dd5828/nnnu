"""GitHub REST 客户端（§7.9，P14）：只做同步需要的两件事——列文件树、取文件。

不引 PyGithub：整个同步只用到两个 REST 端点，一个 httpx 客户端足够，也免得
给可选功能再加一个依赖。Token 是可选的：公开仓库匿名就能读；私有仓库在
设置 → 知识库里填（只进 user-secrets，不回显），或走环境变量。

API 细节：
- 文件树用 `GET /repos/{repo}/git/trees/{ref}?recursive=1`，一次拿全（GitHub
  单次上限 10 万条 / 7MB，超了返回 truncated=true，这里明确报错而不是静默截断）；
- 文件内容走 `raw.githubusercontent.com`（不走 contents API：后者有 1MB 的
  base64 返回上限，raw 没有）；
- 403 有两种：限流（看 x-ratelimit-remaining）和权限不足；分开说，用户才知道
  是该等还是该换 token。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from nnnu.services.secrets.store import get_secrets_store

API_BASE = "https://api.github.com"
RAW_BASE = "https://raw.githubusercontent.com"
TOKEN_DOMAIN = "github"
TOKEN_SLOT = "default"
USER_AGENT = "nnnu-github-source/1.0"
TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
MAX_FILE_BYTES = 200 * 1024 * 1024  # 与 KB 上传上限一致


class GithubError(RuntimeError):
    """GitHub 请求失败（消息写给用户看：仓库不存在、token 无效、限流……）。"""


class GithubConfigError(ValueError):
    """源配置本身不合法（仓库名格式等）——422，不是上游故障。"""


@dataclass(frozen=True, slots=True)
class TreeFile:
    path: str
    sha: str
    size: int


def resolve_github_token() -> str | None:
    """设置里的 token 优先，其次环境变量（NNNU_GITHUB_TOKEN → GITHUB_TOKEN）。"""
    stored = get_secrets_store().get(TOKEN_DOMAIN, TOKEN_SLOT)
    if stored:
        return stored
    for name in ("NNNU_GITHUB_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def validate_repo(repo: str) -> str:
    """`owner/name` 两段；GitHub 用户名/仓库名允许字母数字与 . _ -。"""
    clean = (repo or "").strip().strip("/")
    parts = clean.split("/")
    if len(parts) != 2 or not all(parts):
        raise GithubConfigError("仓库要写成 owner/name，例如 openai/whisper")
    for part in parts:
        if not all(char.isalnum() or char in "._-" for char in part):
            raise GithubConfigError(f"非法的 GitHub 仓库名：{repo!r}")
    return clean


class GithubClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base: str = API_BASE,
        raw_base: str = RAW_BASE,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._raw_base = raw_base.rstrip("/")
        self._token = token if token is not None else resolve_github_token()
        self._client = httpx.AsyncClient(
            timeout=TIMEOUT,
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    async def list_files(self, repo: str, ref: str) -> list[TreeFile]:
        """分支/标签下的全部文件（递归）。"""
        url = f"{self._api_base}/repos/{repo}/git/trees/{quote(ref, safe='')}"
        payload = await self._get_json(url, params={"recursive": "1"})
        if payload.get("truncated"):
            raise GithubError(
                "仓库文件树太大，GitHub 只返回了一部分；请把仓库拆小或改用更具体的分支"
            )
        files: list[TreeFile] = []
        for item in payload.get("tree", []):
            if not isinstance(item, dict) or item.get("type") != "blob":
                continue
            path = str(item.get("path") or "")
            sha = str(item.get("sha") or "")
            if path and sha:
                files.append(TreeFile(path=path, sha=sha, size=int(item.get("size") or 0)))
        return files

    async def download(self, repo: str, ref: str, path: str) -> bytes:
        """raw 地址取文件字节；超过大小上限直接拒绝（不把整个大文件读进内存）。"""
        url = f"{self._raw_base}/{repo}/{quote(ref, safe='')}/{quote(path, safe='/')}"
        try:
            async with self._client.stream("GET", url, headers=self._headers()) as response:
                if response.status_code != 200:
                    raise self._http_error(response, f"读取文件 {path} 失败")
                length = int(response.headers.get("content-length") or 0)
                if length > MAX_FILE_BYTES:
                    raise GithubError(f"文件 {path} 超过 200MB 上限，已跳过")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise GithubError(f"文件 {path} 超过 200MB 上限，已跳过")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError as exc:
            raise GithubError(f"连不上 GitHub：{exc}") from exc

    async def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = await self._client.get(url, headers=self._headers(), params=params)
        except httpx.HTTPError as exc:
            raise GithubError(f"连不上 GitHub：{exc}") from exc
        if response.status_code != 200:
            raise self._http_error(response, "读取仓库信息失败")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GithubError("GitHub 返回的不是 JSON（网络被劫持？）") from exc
        if not isinstance(payload, dict):
            raise GithubError("GitHub 返回格式异常")
        return payload

    @staticmethod
    def _http_error(response: httpx.Response, context: str) -> GithubError:
        status = response.status_code
        message = _response_message(response)
        if status == 401:
            return GithubError(f"{context}：token 无效或没有访问权限")
        if status == 403:
            if response.headers.get("x-ratelimit-remaining") == "0":
                reset = response.headers.get("x-ratelimit-reset", "")
                return GithubError(
                    f"{context}：GitHub 匿名额度用完，请配置 token 或稍后再试（reset={reset}）"
                )
            return GithubError(f"{context}：没有权限（私有仓库需要配置 token）")
        if status == 404:
            return GithubError(f"{context}：仓库、分支或文件不存在（私有仓库需要配置 token）")
        return GithubError(f"{context}：GitHub 返回 {status} {message}".strip())


def _response_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        return str(payload.get("message") or "")[:200]
    return ""


def mask_token(token: str | None) -> str:
    """给日志/审计用：绝不把 token 原文写出去。"""
    if not token:
        return ""
    return f"{token[:4]}****{token[-4:]}" if len(token) > 8 else "****"


def raw_host(raw_base: str) -> str:
    """测试里核对地址用（不参与业务逻辑）。"""
    return urlparse(raw_base).netloc
