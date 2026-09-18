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
  const resp = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
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
