# NOTICE —— 复用出处声明（方案 §16.6）

本项目整体采用 Apache License 2.0（见 LICENSE）。
下表列出全部复用 DeepTutor（https://github.com/HKUDS/DeepTutor，Apache-2.0）
的模块与修改说明；未列出的代码均为自研（白名单外仅对照阅读）。

| 模块 | 本项目路径 | DeepTutor 原路径 | 修改说明 |
|------|-----------|-----------------|---------|
| 联网搜索（多提供商适配） | `src/nnnu/services/search/{types,providers}.py` | `deeptutor/services/search/{types.py,providers/}` | 保留 Citation/SearchResult 字段语义与 Bocha/DDG/SearXNG 接口约定；实现改为 httpx 异步 + 自研解析（原版 requests/ddgs 依赖），响应类型合并为 SearchHit；新增 serpapi_compat 通用端点 |
| arXiv 论文检索 | `src/nnnu/tools/builtin/paper_search_tool.py` | `deeptutor/tools/paper_search_tool.py` | 保留检索语义（all: 查询/年份窗口/相关度与日期排序）；实现改为 httpx 直连 arXiv Atom API + stdlib XML（原版依赖 arxiv 包） |
| 代码沙箱（思路对照） | `src/nnnu/services/sandbox/{spec,service}.py` | `deeptutor/services/sandbox/{service.py,quota.py,spec.py}` | 参考服务门面与进程内配额思路；实现为子进程沙箱（方案 §11.2），Docker 多后端不搬运 |
| 模型价格表 | `src/nnnu/services/cost/pricing.py` | `deeptutor/logging/stats/llm_stats.py` | 复制价格数据（P1 已声明于文件头），补充 deepseek-reasoner/kimi |

DeepTutor 版权声明（HKUDS，Apache-2.0 原文保留）：

> Copyright (c) The University of Hong Kong. All rights reserved.
> Licensed under the Apache License, Version 2.0 (the "License");
> you may not use this file except in compliance with the License.
> You may obtain a copy of the License at
> http://www.apache.org/licenses/LICENSE-2.0
