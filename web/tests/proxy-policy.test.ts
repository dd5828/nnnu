import { describe, expect, it } from "vitest";
import { isBackendPath } from "@/lib/proxy-policy";

describe("isBackendPath", () => {
  it("后端路径命中", () => {
    expect(isBackendPath("/api/v1/health")).toBe(true);
    expect(isBackendPath("/api")).toBe(true);
    expect(isBackendPath("/ws")).toBe(true);
    expect(isBackendPath("/ws/chat")).toBe(true);
  });

  it("前端路径不命中", () => {
    expect(isBackendPath("/settings")).toBe(false);
    expect(isBackendPath("/_next/static/chunk.js")).toBe(false);
    expect(isBackendPath("/favicon.ico")).toBe(false);
    expect(isBackendPath("/")).toBe(false);
  });
});
