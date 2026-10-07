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
| 活书引擎提示词（§16.2 白名单 A） | `prompts/{zh,en}/book.yaml` | `deeptutor/book/prompts/{zh,en}/{spine_agent,spine_synthesizer,page_planner,text,callout,code,deep_dive,figure,flash_cards,timeline,animation,interactive,section}.yaml` | 只改写目录设计与块写作的 system/user 文案：保留「章节树 + 学习目标 + 内容类型 + 来源引用 + block 目录」骨架与 block 目录的选型建议；上游 proposal/exploration/critique/revise 多段环压成一次成（本书目录一次产出「章节 + 每章块计划 + 概念图」）；键名换成方案词汇（learning_objectives→objectives、source_anchors→source_refs、flash_cards→flashcard、interactive→interactive_html、section→text）；概念图只留 id/label/group 与 source/target/label；上游 block 目录里「不要 deep_dive / concept_graph」的一句删掉。**素材汇集、估算口径、编译调度、类型化块校验、页聊天、进度、导出、漂移检查全为自研**（`src/nnnu/book/`） |

DeepTutor 版权声明（HKUDS，Apache-2.0 原文保留）：

> Copyright (c) The University of Hong Kong. All rights reserved.
> Licensed under the Apache License, Version 2.0 (the "License");
> you may not use this file except in compliance with the License.
> You may obtain a copy of the License at
> http://www.apache.org/licenses/LICENSE-2.0

## 前端随仓库分发的第三方资产

| 资产 | 位置 | 许可 | 说明 |
|------|------|------|------|
| Geist / Geist Mono 字体（woff2，子集） | `web/public/fonts/geist/` | SIL Open Font License 1.1（Vercel） | 自托管替代 `next/font/google`：构建期不再请求 fonts.googleapis.com，离线/被墙也能 `next build` |
| KaTeX 运行时（katex.min.js / katex.min.css / auto-render.min.js / 字体） | `web/public/vendor/katex/` | MIT（Copyright (c) 2013-2020 Khan Academy and other contributors） | 沙箱 iframe（HtmlViewer）同源加载 KaTeX，替代 jsdelivr CDN；版本随 `web/package.json` 的 katex 依赖同步 |
