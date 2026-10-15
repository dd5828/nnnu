"""技能系统（§7.16）：SKILL.md 剧本 + skills_meta + 市场安装。

- spec.py：格式规范（frontmatter 校验/组装）
- store.py：data/user/skills/<name>/ 的文件读写（防穿越）
- gate.py：安装前静态安全扫描（§11.4）
- service.py：skills_meta 与磁盘的对账服务
- hub.py：内置/远程市场目录与安装
"""
