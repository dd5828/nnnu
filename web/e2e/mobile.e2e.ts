/** P2 遗留 3：窄屏导航（汉堡菜单 + 侧栏/会话列表抽屉）+ 抽屉开着时的焦点圈闭。
 *
 * 离屏判定必须用 toBeInViewport()：抽屉是 translate 移出去的，元素照样有尺寸、
 * Playwright 的 toBeVisible() 会判它「可见」（宽高非零），只有视口相交才分得出开没开。
 */

import { expect, test } from "@playwright/test";

// 与 useFocusTrap 里那份保持一致：桌面端「Tab 能走出去」用它取侧栏最后一个可聚焦元素
const FOCUSABLE =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

test.use({ viewport: { width: 390, height: 844 } }); // iPhone 14 竖屏

test("⑰ 窄屏：侧栏与会话列表都是抽屉，汉堡开、点导航/遮罩/Esc 关，开时焦点圈在抽屉里", async ({
  page,
}) => {
  await page.goto("/");

  const sidebar = page.getByTestId("app-sidebar");
  const navMenu = page.getByTestId("nav-menu");
  /** 焦点现在是不是在某个 testid 容器里（圈闭断言用）。 */
  const focusedIn = (testid: string) =>
    page.evaluate(
      (id) => document.activeElement?.closest(`[data-testid="${id}"]`) !== null,
      testid
    );

  await expect(sidebar).not.toBeInViewport(); // 默认收着
  await expect(navMenu).toBeVisible(); // 汉堡只在窄屏露头

  // 汉堡开 → 点导航项：既跳转又自动收起
  await navMenu.click();
  await expect(sidebar).toBeInViewport();
  await page.getByRole("link", { name: "知识中心" }).click();
  await expect(page).toHaveURL(/\/knowledge$/);
  await expect(sidebar).not.toBeInViewport();

  // 遮罩点空白也能关（抽屉宽 208px，点 x=380 落在遮罩上）
  await navMenu.click();
  await expect(sidebar).toBeInViewport();
  await page.getByTestId("nav-overlay").click({ position: { x: 380, y: 800 } });
  await expect(sidebar).not.toBeInViewport();

  // 开着时是一层「对话框」：焦点搬进抽屉，Tab/Shift+Tab 绕多少圈都出不去
  await navMenu.click();
  await expect(sidebar).toBeInViewport();
  await expect(navMenu).toHaveAttribute("aria-expanded", "true");
  await expect(navMenu).toHaveAttribute("aria-controls", "app-sidebar");
  await expect(sidebar).toHaveAttribute("role", "dialog");
  await expect.poll(() => focusedIn("app-sidebar")).toBe(true);
  for (let i = 0; i < 30; i += 1) {
    await page.keyboard.press("Tab");
  }
  expect(await focusedIn("app-sidebar")).toBe(true);
  for (let i = 0; i < 6; i += 1) {
    await page.keyboard.press("Shift+Tab");
  }
  expect(await focusedIn("app-sidebar")).toBe(true);

  // Esc 关：顺手把焦点还给打开它的汉堡
  await page.keyboard.press("Escape");
  await expect(sidebar).not.toBeInViewport();
  await expect(navMenu).toHaveAttribute("aria-expanded", "false");
  await expect(navMenu).toBeFocused();

  // 桌面端回归：侧栏常驻，圈闭与对话框语义都要摘掉，Tab 一步就能走出去
  await navMenu.click();
  await expect(sidebar).toBeInViewport();
  await page.setViewportSize({ width: 1024, height: 800 });
  await expect(navMenu).toBeHidden(); // 汉堡只属于窄屏
  await expect(sidebar).toBeInViewport();
  await expect(sidebar).not.toHaveAttribute("role", "dialog");
  await sidebar.locator(FOCUSABLE).last().focus();
  await page.keyboard.press("Tab");
  expect(await focusedIn("app-sidebar")).toBe(false);
  await page.setViewportSize({ width: 390, height: 844 }); // 复位，回窄屏

  // 会话列表同样是抽屉：默认藏起、toggle 展开（焦点也圈进去）、遮罩收起
  await page.goto("/");
  const sessions = page.getByTestId("chat-sessions");
  const sessionsToggle = page.getByTestId("chat-sessions-toggle");
  await expect(sessions).not.toBeInViewport();
  await expect(sessionsToggle).toBeVisible();
  await sessionsToggle.click();
  await expect(sessions).toBeInViewport();
  await expect(sessionsToggle).toHaveAttribute("aria-expanded", "true");
  await expect(sessions).toHaveAttribute("role", "dialog");
  await expect.poll(() => focusedIn("chat-sessions")).toBe(true);
  await page.getByTestId("sessions-overlay").click({ position: { x: 380, y: 800 } });
  await expect(sessions).not.toBeInViewport();
});

// ㉔ 窄屏：会话进度条整条不渲染——内容列右边没有一点槽位。先断「会话里有多个提问」，
// 再断「进度条没有」——跟 ㉓（宽屏有）组成对照，才是对槽位门的有效断言。
test("㉔ 窄屏：同一个会话有多个提问，进度条也不出现（槽位不够）", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("chat-sessions-toggle").click();
  await page
    .getByTestId("chat-sessions")
    .getByText("解释傅里叶变换", { exact: false })
    .first()
    .click();
  await expect(page.locator("textarea").first()).toBeVisible({ timeout: 15000 });

  const bubbles = page.getByTestId("user-message");
  await expect(bubbles.first()).toBeVisible({ timeout: 15000 });
  expect(await bubbles.count()).toBeGreaterThanOrEqual(2); // 该显示却没显示，才是门在起作用
  await expect(page.getByTestId("question-rail")).toHaveCount(0);
});
