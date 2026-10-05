/** 路由级加载态（Next 约定）：切到还没水合的页面时给主区一个不闪的骨架。 */

export default function WorkspaceLoading() {
  return (
    <main className="flex min-h-0 flex-1 items-center justify-center p-6">
      <div
        aria-hidden
        className="h-6 w-6 animate-spin rounded-full border-2 border-border border-t-primary"
      />
    </main>
  );
}
