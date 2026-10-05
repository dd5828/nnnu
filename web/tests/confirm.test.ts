/** confirmDialog：Promise 语义与并发收场（弹窗组件本身由 E2E ㉒ 的删除路径覆盖）。 */

import { beforeEach, describe, expect, it } from "vitest";
import { confirmDialog, useConfirmStore } from "@/hooks/useConfirm";

describe("confirmDialog", () => {
  beforeEach(() => {
    useConfirmStore.setState({ pending: null });
  });

  it("确认 / 取消各回各的 Promise，收场后 pending 清空", async () => {
    const accept = confirmDialog({ message: "删吗", danger: true });
    expect(useConfirmStore.getState().pending?.message).toBe("删吗");
    expect(useConfirmStore.getState().pending?.danger).toBe(true);
    useConfirmStore.getState().settle(true);
    await expect(accept).resolves.toBe(true);
    expect(useConfirmStore.getState().pending).toBeNull();

    const cancel = confirmDialog({ message: "再问一次" });
    useConfirmStore.getState().settle(false);
    await expect(cancel).resolves.toBe(false);
  });

  it("并发请求：旧的按取消收场，不悬着", async () => {
    const first = confirmDialog({ message: "一" });
    const second = confirmDialog({ message: "二" });
    await expect(first).resolves.toBe(false);
    expect(useConfirmStore.getState().pending?.message).toBe("二");
    useConfirmStore.getState().settle(true);
    await expect(second).resolves.toBe(true);
  });
});
