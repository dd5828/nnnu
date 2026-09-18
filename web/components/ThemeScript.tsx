// 防 FOUC：hydration 前按 localStorage 设置 <html data-theme>。
// 逻辑与 lib/theme.ts 的 resolveTheme 保持一致（改动需同步）。
// 必须是 Server Component + dangerouslySetInnerHTML：React 19 下客户端组件内的
// <script> 是惰性的，不会在首帧执行。
const script = `(function () {
  try {
    var themes = ["light", "beige", "dark", "glass"];
    var stored = localStorage.getItem("nnnu-theme");
    var theme = themes.indexOf(stored) >= 0
      ? stored
      : (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.dataset.theme = theme;
  } catch (e) { /* 忽略存储不可用 */ }
})();`;

export default function ThemeScript() {
  return <script dangerouslySetInnerHTML={{ __html: script }} />;
}
