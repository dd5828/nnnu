/** 判定路径是否应反代到后端（/api/* 与 /ws/*）。纯函数，便于 Vitest 覆盖。 */
export function isBackendPath(pathname: string): boolean {
  return (
    pathname === "/api" ||
    pathname.startsWith("/api/") ||
    pathname === "/ws" ||
    pathname.startsWith("/ws/")
  );
}
