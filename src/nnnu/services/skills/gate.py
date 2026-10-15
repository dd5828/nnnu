"""技能安全门（§11.4 / §7.16）：安装前静态扫描。

判定：任一 block 级发现 → 拒绝（市场安装硬拦）；warn 只提示。本地自建/编辑
不走硬拦（文件是用户自己的），但响应照带 findings，编辑器红/黄条展示。

扫描对象 = SKILL.md 全文（frontmatter 与正文都要扫——命令/URL 藏哪儿都算）
+ 每个引用文件文本；另查体积上限与引用文件扩展名（可执行载荷）。
规则表见 docs（STAGE_LOG 偏离清单同步登记）。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from nnnu.services.skills.spec import (
    BINARY_EXTS,
    MAX_REF_FILE_BYTES,
    MAX_REF_FILES,
    MAX_SKILL_MD_BYTES,
    MAX_SKILL_TOTAL_BYTES,
    SKILL_MD,
    SkillFormatError,
    parse_skill_md,
)

MAX_FINDINGS = 40  # 报告上限：够用户判断，也防响应被塞爆
MAX_EXCERPT_CHARS = 120


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    severity: str  # "block" | "warn"
    path: str  # SKILL.md 或引用文件相对路径
    excerpt: str  # 命中处的行文（截断）


@dataclass(frozen=True, slots=True)
class GateReport:
    ok: bool
    findings: tuple[Finding, ...]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "findings": [
                {"rule": f.rule, "severity": f.severity, "path": f.path, "excerpt": f.excerpt}
                for f in self.findings
            ],
        }


@dataclass(frozen=True, slots=True)
class _Rule:
    id: str
    severity: str
    patterns: tuple[re.Pattern[str], ...]


_RULES: tuple[_Rule, ...] = (
    _Rule(
        "dangerous_command",
        "block",
        (
            re.compile(r"\brm\s+(?:-[a-zA-Z]+\s+)+(?:/|~)(?![\w.-])"),  # rm -rf /、rm -rf ~
            re.compile(r"\bmkfs(?:\.\w+)?\b"),
            re.compile(r"\bdd\s+if\s*="),
            re.compile(r"\bformat\s+[a-zA-Z]:"),  # Windows format C:
            re.compile(r"\bdiskpart\b", re.I),
            re.compile(r"\breg\s+delete\b", re.I),
            re.compile(r"\bbcdedit\b", re.I),
            re.compile(r"\bshutdown\s+[-/][a-zA-Z]"),  # shutdown -h / shutdown /s
            re.compile(r"\bnet\s+user\b", re.I),
            re.compile(r"\bcipher\s+/w\b", re.I),
        ),
    ),
    _Rule(
        "shell_pipe_exec",
        "block",
        (
            re.compile(r"\b(?:curl|wget)\b[^\n|]{0,200}\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b", re.I),
            re.compile(
                r"\b(?:curl|wget)\b[^\n|]{0,200}\|\s*(?:sudo\s+)?(?:python[23]?|node|perl|ruby)\b",
                re.I,
            ),
            re.compile(r"\biex\s*\(", re.I),
            re.compile(r"Invoke-Expression", re.I),
            re.compile(r"powershell(?:\.exe)?[^\n]{0,60}-e(?:nc|ncodedcommand)\b", re.I),
            re.compile(r"\beval\s*\(\s*(?:atob|base64)", re.I),
            re.compile(r"\bos\.system\s*\("),
            re.compile(r"subprocess\.[A-Za-z_.]*\([^\n)]{0,200}\bshell\s*=\s*True"),
        ),
    ),
    _Rule(
        "credential_exfil",
        "block",
        (
            re.compile(r"\.ssh[\\/]"),
            re.compile(r"(?:~|%USERPROFILE%)[\\/]\.ssh"),
            re.compile(r"\bid_rsa\b"),
            re.compile(r"\.aws[\\/]credentials"),
            re.compile(r"/etc/passwd\b"),
            re.compile(r"AWS_SECRET", re.I),
            re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
            re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
        ),
    ),
    _Rule(
        "suspicious_url",
        "block",
        (
            re.compile(r"\bhttp://(?!localhost\b|127\.0\.0\.1\b)", re.I),  # 明文 http 外链
            re.compile(r"\b(?!(?:127\.0\.0\.1|0\.0\.0\.0)\b)\d{1,3}(?:\.\d{1,3}){3}\b"),  # 裸 IP
            re.compile(
                r"\b(?:bit\.ly|t\.cn|tinyurl\.com|goo\.gl|is\.gd|rb\.gy|suo\.im|dwz\.cn)\b", re.I
            ),
            re.compile(
                r"\b(?:pastebin\.com|raw\.githubusercontent\.com|gist\.githubusercontent\.com)\b",
                re.I,
            ),
            re.compile(r"\bdiscord(?:app)?\.com/api/webhooks\b", re.I),
            re.compile(r"\.onion\b", re.I),
            re.compile(r"\bfile://", re.I),
            re.compile(r"data:[a-zA-Z0-9/+.-]{0,40}base64,"),
        ),
    ),
)

_ESCAPE_RUN_RE = re.compile(r"(?:(?:\\x|%)(?:[0-9a-fA-F]{2})){30,}")
_ENTROPY_LINE_LEN = 500
_ENTROPY_THRESHOLD = 4.2


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    length = len(text)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _excerpt(text: str, start: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", start)
    line = text[line_start : line_end if line_end != -1 else len(text)].strip()
    if len(line) > MAX_EXCERPT_CHARS:
        line = line[:MAX_EXCERPT_CHARS] + "…"
    return line


def _scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for rule in _RULES:
        for pattern in rule.patterns:
            match = pattern.search(text)
            if match:
                findings.append(
                    Finding(rule.id, rule.severity, path, _excerpt(text, match.start()))
                )
                break  # 同一规则同一文件只报一次，避免刷屏
    for line in text.splitlines():
        if len(line) >= _ENTROPY_LINE_LEN and _shannon_entropy(line) > _ENTROPY_THRESHOLD:
            findings.append(Finding("obfuscation", "warn", path, line[:MAX_EXCERPT_CHARS] + "…"))
            break
    match = _ESCAPE_RUN_RE.search(text)
    if match:
        findings.append(Finding("obfuscation", "warn", path, _excerpt(text, match.start())))
    return findings


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Finding] = []
    for finding in findings:
        key = (finding.rule, finding.path, finding.excerpt)
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    return out


def scan_skill(
    skill_md: str, ref_files: dict[str, str] | None = None, *, total_bytes: int | None = None
) -> GateReport:
    """扫描一份技能（SKILL.md 全文 + 引用文件文本），返回 GateReport。

    frontmatter 解析失败的技能必是 block——市场安装被拒，本地保存只带 findings。
    """
    files = ref_files or {}
    findings: list[Finding] = []
    try:
        parse_skill_md(skill_md)
    except SkillFormatError as exc:
        findings.append(
            Finding("frontmatter_invalid", "block", SKILL_MD, str(exc)[:MAX_EXCERPT_CHARS])
        )

    findings += _scan_text(SKILL_MD, skill_md)
    for rel, content in sorted(files.items()):
        findings += _scan_text(rel, content)
        suffix = Path(rel).suffix.lower()
        if suffix in BINARY_EXTS:
            findings.append(
                Finding("binary_payload", "block", rel, f"扩展名 {suffix} 属于可执行/压缩载荷")
            )

    md_bytes = len(skill_md.encode("utf-8"))
    if md_bytes > MAX_SKILL_MD_BYTES:
        findings.append(
            Finding(
                "oversized", "block", SKILL_MD, f"SKILL.md {md_bytes}B 超过 {MAX_SKILL_MD_BYTES}B"
            )
        )
    if len(files) > MAX_REF_FILES:
        findings.append(
            Finding(
                "oversized",
                "block",
                SKILL_MD,
                f"引用文件 {len(files)} 个超过 {MAX_REF_FILES} 个上限",
            )
        )
    for rel, content in files.items():
        size = len(content.encode("utf-8"))
        if size > MAX_REF_FILE_BYTES:
            findings.append(
                Finding("oversized", "block", rel, f"{size}B 超过单文件 {MAX_REF_FILE_BYTES}B 上限")
            )
    total = (
        total_bytes
        if total_bytes is not None
        else md_bytes + sum(len(c.encode("utf-8")) for c in files.values())
    )
    if total > MAX_SKILL_TOTAL_BYTES:
        findings.append(
            Finding(
                "oversized", "block", SKILL_MD, f"整包 {total}B 超过 {MAX_SKILL_TOTAL_BYTES}B 上限"
            )
        )

    deduped = _dedupe(findings)[:MAX_FINDINGS]
    return GateReport(ok=not any(f.severity == "block" for f in deduped), findings=tuple(deduped))
