/** API 客户端：相对路径直发同源（经 Next proxy 反代到后端），解包 §9.1 错误信封。 */

export class ApiError extends Error {
  status: number;
  code: string;
  recoverable: boolean;

  constructor(status: number, code: string, message: string, recoverable: boolean) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.recoverable = recoverable;
  }
}

/** 默认请求超时（毫秒）：后端卡死时别让页面永远转圈。
 *  上传（FormData）走放宽的一档——200MB 的 PDF 传十分钟不算异常。 */
export const API_TIMEOUT_MS = 30_000;
export const API_UPLOAD_TIMEOUT_MS = 10 * 60_000;

export async function apiFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  // JSON 字符串才标 Content-Type；FormData 交给浏览器自动带 multipart 边界
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string>) };
  if (typeof init?.body === "string" && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const isUpload = typeof FormData !== "undefined" ? init?.body instanceof FormData : false;
  const resp = await fetch(path, {
    ...init,
    headers,
    // 调用方显式给了 signal 就照它的来（比如组件卸载时中止）
    signal: init?.signal ?? AbortSignal.timeout(isUpload ? API_UPLOAD_TIMEOUT_MS : API_TIMEOUT_MS),
  });
  if (!resp.ok) {
    let code = "http_error";
    let message = resp.statusText;
    let recoverable = false;
    try {
      const body = (await resp.json()) as {
        error?: { code?: string; message?: string; recoverable?: boolean };
      };
      if (body.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
        recoverable = body.error.recoverable ?? recoverable;
      }
    } catch {
      // 非 JSON 响应，用默认值
    }
    throw new ApiError(resp.status, code, message, recoverable);
  }
  return (await resp.json()) as T;
}
