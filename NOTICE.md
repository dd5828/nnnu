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
| 深度研究提示词（§16.2 白名单 A） | `prompts/{zh,en}/deep_research.yaml` | `deeptutor/agents/research/prompts/{zh,en}/pipeline.yaml` | 只改写 rephrase/decompose/research_step/report 四段 system 文案：删 THINK/TOOL/APPEND/FINISH 标签协议、删 APPEND 队列话术、删 note 摘要 agent，报告从「大纲 + 引言 + 逐节 + 结论」多次调用压成单次成稿。**引用编号重排（`src/nnnu/services/research/citations.py`）为自研**，未搬 `utils/citation_manager.py`（§16.5 禁止复制实现层） |
| 可视化提示词（§16.2 白名单 A） | `prompts/{zh,en}/visualize.yaml` | `deeptutor/agents/visualize/prompts/{zh,en}/{analysis_agent,code_generator_agent,review_agent,visualize}.yaml` | 只改写分析/生成/复查三段 system 与四条渲染类型规则：Chart.js 规则改写成 ECharts（本仓库前端用 ECharts）；去掉 manim_image 与 visual_genre 路由；点击追问（data-prompt）不实现；正文围栏契约（标题 + 一句说明 + 恰好一个渲染围栏）与降级说明为本仓库自定。**路由、校验、降级、正文拼装全是自研**（`src/nnnu/capabilities/visualize/capability.py` + `src/nnnu/services/render/`）。注：方案 §16.5 写的 `deeptutor/capabilities/visualize` 路径不存在，实际在上游 `agents/` 下 |
| 数学动画提示词（§16.2 白名单 A） | `prompts/{zh,en}/math_animator.yaml` | `deeptutor/agents/math_animator/prompts/{zh,en}/{concept_analysis_agent,concept_design_agent,code_generator_agent,summary_agent,math_animator}.yaml` | 只改写六阶段 system 与 task 文案：去掉 image 模式与 YON_IMAGE 锚点（本轮只出视频）、去掉 visual_review 截图质检（本仓库渲染器不产可截帧的中间结果）；生成段改成「恰好一个 ```python 代码块」的正文契约；渲染日志节选、失败收场与依赖安装指引为本仓库自定。保留上游硬约束：3D 坐标、禁 Tex/MathTex/MarkupText、禁 stroke_dash_pattern、禁 self.save_state、MovingCameraScene 相机规则、节奏预算。**六阶段流水线与假渲染器全是自研**（`src/nnnu/services/render/animator.py`、`services/render/service.py`） |
| Co-Writer 提示词（§16.2 白名单 A） | `prompts/{zh,en}/co_writer.yaml` | `deeptutor/co_writer/prompts/{zh,en}/edit_agent.yaml` | 只改写「编辑助手 system / 动作 / 参考上下文 / 目标文本」四段模板：动作收敛成六档枚举（rewrite/expand/shorten/translate/tone/free）；去掉 auto_mark 标注提示词（本仓不做标注）；保留上游「只输出编辑后文本、参考上下文仅在提供时使用」的用法。**切片校验、行级 diff、待确认编辑的 accept/reject 全为自研**（方案 §7.13；上游只有直接替换 + 客户端 undo，没有 diff/accept-reject） |
| MCP 客户端（连接管理） | `src/nnnu/services/mcp/manager.py` | `deeptutor/services/mcp/manager.py` | 保留「每台服务器一个连接对象 + 工具包成适配器 + 指数退避重连」的结构；改的是生命周期模型：上游按多用户/多租户写（owner、作用域、按请求取连接），本项目是单机单人，去掉这两样，改成「启动不阻塞、后台各自连、30 秒巡检重试、needs_auth 不重试」；新增 `probe`（一次性探测，测连接按钮用）、连接指纹（配置或凭据变了就重连）、读流监视（SDK 静默吞掉的对端断开要能自己发现，见 `_WatchedReadStream`）。工具命名、进度回调、失败原因码全为自研 |
| MCP 客户端（OAuth） | `src/nnnu/services/mcp/oauth.py` | `deeptutor/services/mcp/oauth.py` | 保留令牌存储接口与 SDK 授权流程的接法（`OAuthClientProvider` 的四件套）；回调地址从「公网回调路由 + 前端页面」换成**本机环回临时端口**（本项目是单机应用，没有公网回调地址），授权结果直接由本机 HTTP 响应页告知；令牌与客户端注册存进本仓 SecretsStore（域 `mcp/<id>`），不在各自 JSON 里 |
| MCP 凭据引用 | `src/nnnu/services/mcp/secrets.py` | `deeptutor/services/mcp/secrets.py` | 保留「配置里只写引用、明文在密钥库、连接时在内存里换」的规矩；存储从上游「每服务器一个 JSON」换成本仓 SecretsStore 域（§11.1）；引用语法从 `${secret:<服务器>/<字段>}` 收成 `${secret:字段}`（服务器已经在域名里）；新增 URL 查询参数与请求头的替换入口（整串/部分两种粒度，理由见文件头） |
| 活书引擎提示词（§16.2 白名单 A） | `prompts/{zh,en}/book.yaml` | `deeptutor/book/prompts/{zh,en}/{spine_agent,spine_synthesizer,page_planner,text,callout,code,deep_dive,figure,flash_cards,timeline,animation,interactive,section}.yaml` | 只改写目录设计与块写作的 system/user 文案：保留「章节树 + 学习目标 + 内容类型 + 来源引用 + block 目录」骨架与 block 目录的选型建议；上游 proposal/exploration/critique/revise 多段环压成一次成（本书目录一次产出「章节 + 每章块计划 + 概念图」）；键名换成方案词汇（learning_objectives→objectives、source_anchors→source_refs、flash_cards→flashcard、interactive→interactive_html、section→text）；概念图只留 id/label/group 与 source/target/label；上游 block 目录里「不要 deep_dive / concept_graph」的一句删掉。**素材汇集、估算口径、编译调度、类型化块校验、页聊天、进度、导出、漂移检查全为自研**（`src/nnnu/book/`） |
| 技能创作指南（§16.2 白名单 C，改写） | `catalogs/skills_hub.json` 的 `skill-authoring` 条目 | `deeptutor/skills/builtin/skill-creator/SKILL.md` | 只改写「技能是什么、正文怎么写、自检清单」三层文案，改成本仓词汇与限制：frontmatter 改成 name/description/when_to_use 三个字段（带 48/1024/512 字符上限，对应本仓 `skills/spec.py` 的校验口径），正文写「先自己写目标 / 分步骤写指令 / 给一条最小示例 / 末尾自检」四步，并列出本仓的落盘与体积上限（SKILL.md 256KB、单技能 2MB、文件数 20）。上游的 `tags/always/requires` 字段、`references/` 渐进披露与 read_skill 取文件的说法一律不写——本仓技能就是一份纯文本剧本（不做渐进披露）。**技能读写校验、市场目录与安全扫描全为自研**（`src/nnnu/services/skills/`） |
| 命令行应用目录清单（数据改形） | `catalogs/cli_apps.json` | `deeptutor/services/cli_apps/vendor/catalog.json`（上游为 HKUDS/CLI-Anything 的目录快照，commit `bc536c9b`，2026-07-09；Apache-2.0） | 取上游 101 条里的 12 条（条目里 `upstream` 字段标着来历），**数据改形**：上游的 `install_cmd` 自由字符串（brew/cargo/uv/curl/cd/python3 等五花八门）重写成 `install:{kind:pip\|npm\|preinstalled\|bundled, target}` 的枚举，只留本仓代劳得了的两种（pip/npm）与「本来就装好了」的检测；新增双语 description/guide、`params_schema`、`argv_template`、`platform`、`network`、`default_timeout_s`；上游那 12 条 pip 装的是 CLI-Anything 仓里各 harness 的 subdirectory（安装时真去拉它的包，只当普通 pip 依赖用）。其余 49 条（ffmpeg 一类）为本仓自写。**解析校验、安装器、运行器、界面全为自研**（`src/nnnu/services/cli_apps/`） |

DeepTutor 版权声明（HKUDS，Apache-2.0 原文保留）：

> Copyright (c) The University of Hong Kong. All rights reserved.
> Licensed under the Apache License, Version 2.0 (the "License");
> you may not use this file except in compliance with the License.
> You may obtain a copy of the License at
> http://www.apache.org/licenses/LICENSE-2.0

CLI-Anything 目录数据声明（HKUDS，Apache-2.0）：
`catalogs/cli_apps.json` 里带 `upstream` 字段的 12 条来自 HKUDS/CLI-Anything
（https://github.com/HKUDS/CLI-Anything ，Apache License 2.0，版权归其作者所有，
许可全文见该仓库根目录的 LICENSE）。只取目录元数据（名字、程序名、分类、主页）
并改形，未搬运其代码。安装方式上：5 条 pip 直装它在 GitHub 上的 harness 子目录
（`git+…#subdirectory=…`）——**没钉 commit**，跟着默认分支走（上游快照里那个
`bc536c9b` 是它自己给首方 harness 钉的版本，本项目暂时没跟），1 条
（hacker-feeds-cli）装的是第三方仓库 collectivewinca/hacker-feeds-cli，
其余走 npm 公共注册表。

## 前端随仓库分发的第三方资产

| 资产 | 位置 | 许可 | 说明 |
|------|------|------|------|
| Geist / Geist Mono 字体（woff2，子集） | `web/public/fonts/geist/` | SIL Open Font License 1.1（Vercel） | 自托管替代 `next/font/google`：构建期不再请求 fonts.googleapis.com，离线/被墙也能 `next build` |
| KaTeX 运行时（katex.min.js / katex.min.css / auto-render.min.js / 字体） | `web/public/vendor/katex/` | MIT（Copyright (c) 2013-2020 Khan Academy and other contributors） | 沙箱 iframe（HtmlViewer）同源加载 KaTeX，替代 jsdelivr CDN；版本随 `web/package.json` 的 katex 依赖同步 |
