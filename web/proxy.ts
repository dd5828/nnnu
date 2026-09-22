import { NextResponse, type NextRequest } from "next/server";
import { isBackendPath } from "@/lib/proxy-policy";

// IPv4 字面量：双栈主机上 localhost 可能先解析到 ::1，而 uvicorn 只绑 IPv4。
// 运行期读取环境变量（Docker 注入），URL 知识不进 bundle。
const API_BASE = process.env.NNNU_API_BASE_URL ?? "http://127.0.0.1:8001";

/**
 * Next 16 Proxy：/api/* 与 /ws/* 反代到后端 loopback:8001。
 *
 * 注意：本文件运行在 Node.js runtime（非 Edge），勿用 Edge 专属 API。
 * 例外：/api/upload/* 从 matcher 里排除（它由 web/app/api/upload/ 下的 route handler
 * 自己流式转发）——rewrite 的请求体在 ~10MB 处会断（实测 6MB 过、12MB 起 socket hang up），
 * 而知识库单文件上限 50MB。
 */
export function proxy(request: NextRequest) {
  if (isBackendPath(request.nextUrl.pathname)) {
    const target = new URL(request.nextUrl.pathname + request.nextUrl.search, API_BASE);
    return NextResponse.rewrite(target);
  }
  return NextResponse.next();
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|api/upload/|.*\\.(?:svg|png|jpg|jpeg|gif|ico|woff2?)$).*)",
  ],
};
