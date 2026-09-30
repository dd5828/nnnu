"""渲染产物下载（§7.7/§7.8）：`GET /api/v1/renders/{render_id}`，`?download=1` 切附件。

URL **刻意不带扩展名**（`web/proxy.ts` 的 matcher 会跳过 `.mp4/.png/...` 结尾的
路径，见 services/render/models.py 的 `RenderMeta.url`）。读取一律过 RenderStore
（正则 + resolve 双重防穿越，后缀白名单在存储层卡死），这里只管回文件。
"""

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter()


@router.get("/api/v1/renders/{render_id}")
async def get_render(render_id: str, http_request: Request, download: int = 0):
    found = http_request.app.state.renders.get(render_id)
    if found is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "not_found",
                    "message": f"渲染产物 {render_id} 不存在",
                    "recoverable": False,
                }
            },
        )
    path, meta = found
    return FileResponse(
        path,
        filename=meta.filename,
        media_type=meta.mime,
        # inline：视频卡片直接 <video src> 播放；download=1 才走附件下载
        content_disposition_type="attachment" if download else "inline",
    )
