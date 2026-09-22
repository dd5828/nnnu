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

export async function apiFetch<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  // JSON 字符串才标 Content-Type；FormData 交给浏览器自动带 multipart 边界
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string>) };
  if (typeof init?.body === "string" && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, { ...init, headers });
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
