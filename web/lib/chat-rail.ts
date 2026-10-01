/** 会话进度条（右缘跳题轨）的全部纯逻辑：条目派生、markdown 扁平化、活动刻度数学。
 *
 * 这里零 DOM 零 React——组件（`components/chat/QuestionRail.tsx`）只负责测量、摆位
 * 和事件，判断全在下面这些函数里，vitest 直接测（`tests/chat-rail.test.ts`）。
 *
 * 刻度契约：**一个可见的用户气泡 = 一个刻度**，ordinal 与屏幕上的气泡一一对应。
 * 所以这里只挡「扁平化后为空」的消息（纯附件、空正文）；「确认，开始研究」这类
 * 短确认照常入列——它有自己的回复（报告），是合法的跳转目标。将来若引入模型替
 * 用户打的合成消息，再按前缀过滤并补测试。
 */

export interface RailMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
}

export interface QuestionEntry {
  id: string;
  /** 从 1 数起，跟屏幕上气泡的顺序一致 */
  ordinal: number;
  /** 提问的扁平化预览（刻度悬停卡第一行内容） */
  title: string;
  /** 该轮回复的扁平化预览；这轮还没回复（或回复为空）就是空串 */
  reply: string;
}

/** 视口顶往下这一段算「已经翻过去的轮次」（px）。 */
export const ACTIVE_LINE_PX = 140;
/** 跳转落点：那轮气泡顶边离容器顶留的白（px）。 */
export const JUMP_TOP_GAP_PX = 56;
/** 内容列右侧的槽位窄于它就藏轨（px）。 */
export const MIN_GUTTER_PX = 44;
/** 刻度标题最多留这么多字，超了截断加省略号。 */
export const RAIL_TITLE_MAX = 160;
/** 轨道最高占视口的比例（超出就在轨内滚——问特别多的时候）。 */
export const TRACK_MAX_VH = 0.62;
/** 轨道上下留白（px），预览卡定位也按它算。 */
export const TRACK_PAD_PX = 8;
/** 跳转后气泡高亮环挂多久（ms）；globals.css 里 rail-flash 的时长跟它一致。 */
export const FLASH_MS = 1300;

/**
 * 把一段 markdown 压成一行纯文本，给刻度当标题/回复预览用。
 * 规则：围栏代码、图片、分隔线整段丢；行首标记（标题/列表/引用）去标记留字；
 * 链接留字、行内 HTML 与行内 code 去壳；强调标记去掉（不碰 snake_case）；
 * 空白折叠成一个空格；超长截断加省略号。
 */
export function plainTextPreview(markdown: string, maxLength = RAIL_TITLE_MAX): string {
  let text = markdown;
  text = text.replace(/```[\s\S]*?(?:```|$)/g, " "); // 围栏代码整段丢（没闭合的从围栏丢到文末）
  text = text.replace(/!\[[^\]]*\]\([^)]*\)/g, " "); // 图片整段丢
  text = text.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1"); // 链接留字
  text = text.replace(/^\s*(?:[-*+]|\d+[.)])\s+/gm, ""); // 列表标记
  text = text.replace(/^\s*#{1,6}\s+/gm, ""); // 标题井号
  text = text.replace(/^\s*>\s?/gm, ""); // 引用尖角
  text = text.replace(/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/gm, " "); // 分隔线
  text = text.replace(/<[^>]*>/g, " "); // 行内 HTML
  text = text.replace(/`([^`]*)`/g, "$1"); // 行内 code 去反引号
  text = text.replace(/(?:\*\*|__|~~)/g, ""); // 粗体/删除线标记
  text = text.replace(/\*(?!\s)([^*\n]*[^\s*])\*/g, "$1"); // 单星斜体（两边得贴着字，乘法号不误伤）
  text = text.replace(/(^|[\s(])_([^_]+)_(?=[\s).,!?:;]|$)/g, "$1$2"); // 下划线斜体（不碰 snake_case）
  text = text.replace(/\s+/g, " ").trim();
  return text.length > maxLength ? `${text.slice(0, maxLength).trimEnd()}…` : text;
}

/** 这条消息占不占一个刻度：用户消息且压平后还有字。 */
export function isRailQuestion(message: RailMessage): boolean {
  return message.role === "user" && plainTextPreview(message.content).length > 0;
}

/**
 * 按消息序派生刻度条目：一问一条，reply 取其后第一条有正文的 assistant
 * （多段回合里第一段可能是空的，继续找；遇到下一条用户消息就停）。
 */
export function buildQuestionEntries(messages: RailMessage[]): QuestionEntry[] {
  const entries: QuestionEntry[] = [];
  messages.forEach((message, index) => {
    if (!isRailQuestion(message)) {
      return;
    }
    let reply = "";
    for (let i = index + 1; i < messages.length; i += 1) {
      const next = messages[i];
      if (next.role === "user") {
        break; // 下一轮开始了，回复预览就到这里为止
      }
      const flat = plainTextPreview(next.content);
      if (flat) {
        reply = flat;
        break;
      }
    }
    entries.push({
      id: message.id,
      ordinal: entries.length + 1,
      title: plainTextPreview(message.content),
      reply,
    });
  });
  return entries;
}

/**
 * 哪一格刻度算「当前」：气泡顶边相对容器顶挨得最近、且已经翻过活动线的那一格。
 * 空数组 → -1；谁都没过线（还在最顶上）→ 0。NaN/Infinity 参与不了比较，自然不误判。
 */
export function activeIndexFromTops(tops: number[], line = ACTIVE_LINE_PX): number {
  if (tops.length === 0) {
    return -1;
  }
  let passed = -1;
  tops.forEach((top, index) => {
    if (top <= line) {
      passed = index;
    }
  });
  return passed === -1 ? 0 : passed;
}

/** 跳转落点：目标元素顶边对到容器顶再上移 gap 后的 scrollTop（调用方负责夹到合法范围）。 */
export function jumpTargetTop(
  elTop: number,
  containerTop: number,
  scrollTop: number,
  gap = JUMP_TOP_GAP_PX
): number {
  return scrollTop + (elTop - containerTop) - gap;
}

/** 够不够格显示这条轨：至少两问（一问没有「切换」可言）+ 槽位够宽。 */
export function shouldShowRail(count: number, gutterPx: number): boolean {
  return count >= 2 && gutterPx >= MIN_GUTTER_PX;
}

/** 每格刻度多高（px）：问得越多压得越扁，四档。 */
export function rowHeightPx(count: number): number {
  if (count <= 10) {
    return 18;
  }
  if (count <= 24) {
    return 12;
  }
  if (count <= 40) {
    return 8;
  }
  return 7;
}

/** 预览卡的纵坐标：对在该格刻度中心上（轨内滚动过要减掉滚动量）。 */
export function previewTopPx(index: number, count: number, trackScrollTop = 0): number {
  const row = rowHeightPx(count);
  return TRACK_PAD_PX + index * row + row / 2 - trackScrollTop;
}
