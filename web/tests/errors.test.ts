/** 页面级错误文案口径：ApiError 只取 message、网络层错误换人话、其余不盖真话。 */

import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api";
import { errorText } from "@/lib/errors";

const FALLBACK = "请求没发出去";

describe("errorText", () => {
  it("ApiError 只取后端 message，不带类名前缀", () => {
    const error = new ApiError(404, "not_found", "知识库不存在", false);
    expect(errorText(error, FALLBACK)).toBe("知识库不存在");
  });

  it("TypeError（fetch 网络层失败）换成一句人话，不透出英文原文", () => {
    expect(errorText(new TypeError("Failed to fetch"), FALLBACK)).toBe(FALLBACK);
  });

  it("超时/中止（AbortSignal.timeout）也换算成一句人话", () => {
    expect(errorText(new DOMException("signal timed out", "TimeoutError"), FALLBACK)).toBe(
      FALLBACK
    );
    expect(errorText(new DOMException("aborted", "AbortError"), FALLBACK)).toBe(FALLBACK);
  });

  it("其他异常原样透出，别把真话盖掉", () => {
    expect(errorText(new Error("某个没被包装的错"), FALLBACK)).toBe("Error: 某个没被包装的错");
    expect(errorText("裸字符串", FALLBACK)).toBe("裸字符串");
  });
});
