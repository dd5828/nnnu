/**
 * 知识库文档上传的专用转发口（P4）：proxy 的 rewrite 会在 ~10MB 处把 multipart
 * 请求体弄断（实测 6MB 过、12MB 起 500 socket hang up），而 KB 允许单文件 50MB。
 * 所以上传不走 `/api/*` 的通用反代，改由这个 route handler 把请求体原样流式转给后端。
 *
 * 路径刻意放在 `/api/upload/*`（matcher 里排除，见 web/proxy.ts）：
 * 不碰 `isBackendPath` 那条「/api 一律反代」的规则，避免两边都不认的缝。
 */

const API_BASE = process.env.NNNU_API_BASE_URL ?? "http://127.0.0.1:8001";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ kb_id: string }> }
): Promise<Response> {
  const { kb_id } = await params;
  const upstream = await fetch(`${API_BASE}/api/v1/kbs/${encodeURIComponent(kb_id)}/docs`, {
    method: "POST",
    headers: { "content-type": request.headers.get("content-type") ?? "application/octet-stream" },
    // 不读进内存、不落盘：直接接上下行的流
    body: request.body,
    duplex: "half",
  } as RequestInit);
  // 状态码与响应体（含 §9.1 错误信封）原样回给前端，apiFetch 的解析逻辑不用改
  const body = await upstream.text();
  return new Response(body, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
  });
}
