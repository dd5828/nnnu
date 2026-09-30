/** P2 遗留 3：窄屏导航（汉堡菜单 + 侧栏/会话列表抽屉）。
 *
 * 离屏判定必须用 toBeInViewport()：抽屉是 translate 移出去的，元素照样有尺寸、
 * Playwright 的 toBeVisible() 会判它「可见」（宽高非零），只有视口相交才分得出开没开。
 */

import { expect, test } from "@playwright/test";

test.use({ viewport: { width: 390, height: 844 } }); // iPhone 14 竖屏

test("⑰ 窄屏：侧栏与会话列表都是抽屉，汉堡开、点导航/遮罩/Esc 关", async ({ page }) => {
  await page.goto("/");

  const sidebar = page.getByTestId("app-sidebar");
  await expect(sidebar).not.toBeInViewport(); // 默认收着
  await expect(page.getByTestId("nav-menu")).toBeVisible(); // 汉堡只在窄屏露头

  // 汉堡开 → 点导航项：既跳转又自动收起
  await page.getByTestId("nav-menu").click();
  await expect(sidebar).toBeInViewport();
  await page.getByRole("link", { name: "知识中心" }).click();
  await expect(page).toHaveURL(/\/knowledge$/);
  await expect(sidebar).not.toBeInViewport();

  // 遮罩点空白也能关（抽屉宽 208px，点 x=380 落在遮罩上）
  await page.getByTestId("nav-menu").click();
  await expect(sidebar).toBeInViewport();
  await page.getByTestId("nav-overlay").click({ position: { x: 380, y: 800 } });
  await expect(sidebar).not.toBeInViewport();

  // Esc 也能关
  await page.getByTestId("nav-menu").click();
  await expect(sidebar).toBeInViewport();
  await page.keyboard.press("Escape");
  await expect(sidebar).not.toBeInViewport();

  // 会话列表同样是抽屉：默认藏起、toggle 展开、遮罩收起
  await page.goto("/");
  const sessions = page.getByTestId("chat-sessions");
  await expect(sessions).not.toBeInViewport();
  await expect(page.getByTestId("chat-sessions-toggle")).toBeVisible();
  await page.getByTestId("chat-sessions-toggle").click();
  await expect(sessions).toBeInViewport();
  await page.getByTestId("sessions-overlay").click({ position: { x: 380, y: 800 } });
  await expect(sessions).not.toBeInViewport();
});
