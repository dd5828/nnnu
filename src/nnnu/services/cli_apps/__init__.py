"""命令行应用目录（§7.18）：装的是一包 CLI，跑的是 argv。

- spec.py：条目结构、安装计划解析、参数模板渲染
- catalog.py：内置目录（catalogs/cli_apps.json）+ 用户自加条目
- install.py：探测/pip/npm/自带的安装与卸载（沙箱外、argv 化、超时 300s）
- runner.py：模板 → 沙箱 argv 执行 → 首跑注入指南、输出截断
- service.py：门面（列表带状态、装/卸、记录）
"""
