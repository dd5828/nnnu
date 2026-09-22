"""E2E 样例 PDF（global-setup 调用）：一页含"傅里叶变换"关键词的中文文档。

输出路径由第一个命令行参数指定（默认 e2e/.artifacts/sample.pdf）。
"""

import sys
from pathlib import Path

import pymupdf

out = Path(sys.argv[1] if len(sys.argv) > 1 else "e2e/.artifacts/sample.pdf")
out.parent.mkdir(parents=True, exist_ok=True)
doc = pymupdf.open()
page = doc.new_page()
page.insert_text((72, 100), "傅里叶变换", fontname="china-s", fontsize=18)
page.insert_text(
    (72, 140),
    "傅里叶变换是一种数学变换，把时域信号分解为不同频率的正弦波分量之和。",
    fontname="china-s",
    fontsize=12,
)
page.insert_text(
    (72, 180),
    "它在信号滤波、图像压缩和频谱分析中广泛应用。",
    fontname="china-s",
    fontsize=12,
)
doc.save(str(out))
print("generated", out)
