/** 聊天滚动几何（纯函数，只被 MessageList 用，单测见 tests/chat-scroll.test.ts）。
 *
 * 「回复开头对到视口顶部」的物理前提：回合下方得**够滚**。回合内容比一屏短时，
 * 目标 scrollTop 超出了 maxScrollTop，浏览器一夹，回复开头就永远到不了顶。
 * 办法是在列表尾巴垫一块占位（spacer），把空出来的高度补上——见 MessageList
 * 的 writeTailSpacer。
 */

/** 回合开头与视口顶之间留的白（px）：不贴着边，看着不局促 */
export const TURN_TOP_GAP_PX = 8;

/** 尾巴占位高：让 `scrollTop = 锚点顶 - GAP` 恰好可达（不多滚）。
 *
 * @param viewport 滚动容器可视高（clientHeight）
 * @param contentBelowAnchor 锚点顶以下、**不含占位本身**的内容总高（含底部留白与间距）
 */
export function tailSpacerHeight(viewport: number, contentBelowAnchor: number): number {
  return Math.max(0, viewport - TURN_TOP_GAP_PX - contentBelowAnchor);
}
