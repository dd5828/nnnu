"use client";

/**
 * Book 活书读写（§7.14）：TanStack Query + 「编译中才轮询」。
 *
 * 几条和别处不一样的约定：
 * - GET 详情在 compiling 时 1500ms 轮询（照知识库 isKbBusy 手法）；其余状态停轮询，
 *   生成节奏由这一轮数据自己翻状态。轮询不额外失效列表：编译中的书在书库里
 *   也跟着旧一档，等状态翻页时再刷；
 * - 建书（同步跑 spine + 估算）与页聊天是模型调用：超时放宽（180s / 120s，
 *   照 co-writer 的 EDIT_TIMEOUT_MS 手法）；编译与 animation 再生是 202 + 轮询；
 * - 变更后失效前缀 ["book"] 与 ["book", id]（列表卡片与控制台都要刷）。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api";
import { BOOK_POLL_MS, hasPendingBlocks, isBookBusy } from "@/lib/book";
import type {
  BookAttemptResponse,
  BookBlockEditBody,
  BookBlocksResponse,
  BookChatResponse,
  BookCompileResponse,
  BookDetail,
  BookPageDetail,
  BookPauseResponse,
  BookSources,
  BookSummary,
} from "@/types/api";

/** 建书超时（ms）：spine 一次模型调用 + 素材汇集，默认 30s 不够。 */
export const CREATE_BOOK_TIMEOUT_MS = 180_000;
/** 页聊天超时（ms）：对话级模型调用（照 co-writer 改写档）。 */
export const BOOK_CHAT_TIMEOUT_MS = 120_000;

export function useBookList() {
  return useQuery({
    queryKey: ["book"],
    queryFn: () => apiFetch<{ books: BookSummary[] }>("/api/v1/books"),
  });
}

export function useBookDetail(id: string | null) {
  return useQuery({
    queryKey: ["book", id],
    queryFn: () => apiFetch<BookDetail>(`/api/v1/books/${id}`),
    enabled: Boolean(id),
    refetchInterval: (query) => {
      const data = query.state.data;
      return data && isBookBusy(data.status) ? BOOK_POLL_MS : false;
    },
  });
}

/** 页详情（阅读器装载用）：块还有没生成完的（compiling/pending）就轮询——
 *  只有 animation 再生会落在这个窗口里，其余路径一次往返就到位。 */
export function useBookPage(bookId: string | null, pageId: string | null) {
  return useQuery({
    queryKey: ["book", bookId, "page", pageId],
    queryFn: () => apiFetch<BookPageDetail>(`/api/v1/books/${bookId}/pages/${pageId}`),
    enabled: Boolean(bookId && pageId),
    refetchInterval: (query) => {
      const data = query.state.data;
      return data && hasPendingBlocks(data.blocks) ? BOOK_POLL_MS : false;
    },
  });
}

/** 变更后失效列表与控制台详情。 */
function useInvalidateBook() {
  const queryClient = useQueryClient();
  return (id?: string, pageId?: string) => {
    void queryClient.invalidateQueries({ queryKey: ["book"] });
    if (id) {
      void queryClient.invalidateQueries({ queryKey: ["book", id] });
    }
    if (id && pageId) {
      void queryClient.invalidateQueries({ queryKey: ["book", id, "page", pageId] });
    }
  };
}

export interface CreateBookInput {
  title: string;
  sources: BookSources;
  language: string;
}

/** 建书：服务端同步跑 spine + 估算后返回整本详情（忙态由按钮自己表现）。 */
export function useCreateBook() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CreateBookInput) =>
      apiFetch<BookDetail>("/api/v1/books", {
        method: "POST",
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(CREATE_BOOK_TIMEOUT_MS),
      }),
    onSuccess: (book) => {
      // 控制台马上要跳过去读详情：把刚拿到的整本先塞缓存，省一次往返
      queryClient.setQueryData(["book", book.id], book);
      void queryClient.invalidateQueries({ queryKey: ["book"] });
    },
  });
}

/** 改书名 / 改目录（chapters 是**全量有序列表**：改名靠新 title、删章靠不带给它）。 */
export function useUpdateBook() {
  const queryClient = useQueryClient();
  const invalidate = useInvalidateBook();
  return useMutation({
    mutationFn: ({
      id,
      ...body
    }: {
      id: string;
      title?: string;
      chapters?: { key: string; title: string }[];
    }) =>
      apiFetch<BookDetail>(`/api/v1/books/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: (book) => {
      queryClient.setQueryData(["book", book.id], book);
      invalidate(book.id);
    },
  });
}

export function useDeleteBook() {
  const invalidate = useInvalidateBook();
  return useMutation({
    mutationFn: (id: string) => apiFetch(`/api/v1/books/${id}`, { method: "DELETE" }),
    onSuccess: (_data, id) => invalidate(id),
  });
}

/** 开编 / 续跑（202 立即回，状态由详情轮询自己翻）；给 chapters = 整章重跑。 */
export function useCompileBook() {
  const invalidate = useInvalidateBook();
  return useMutation({
    mutationFn: ({ id, chapters }: { id: string; chapters?: string[] }) =>
      apiFetch<BookCompileResponse>(`/api/v1/books/${id}/compile`, {
        method: "POST",
        body: JSON.stringify(chapters ? { chapters } : {}),
      }),
    onSuccess: (_data, variables) => invalidate(variables.id),
  });
}

/** 请求暂停：任务在下一个块边界收手；没任务在跑是 no-op。 */
export function usePauseCompile() {
  const invalidate = useInvalidateBook();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<BookPauseResponse>(`/api/v1/books/${id}/compile/pause`, { method: "POST" }),
    onSuccess: (_data, id) => invalidate(id),
  });
}

/** 块编辑六种操作：常规同步回全量块数组；animation 再生 202（调用方轮询页）。 */
export function useEditBlocks() {
  const invalidate = useInvalidateBook();
  return useMutation({
    mutationFn: ({
      bookId,
      pageId,
      ...body
    }: { bookId: string; pageId: string } & BookBlockEditBody) =>
      apiFetch<BookBlocksResponse>(`/api/v1/books/${bookId}/pages/${pageId}/blocks`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onSuccess: (_data, variables) => invalidate(variables.bookId, variables.pageId),
  });
}

/** 页聊天（非流式）：答完一次性返回；历史自存本页，前端拉页详情就有。 */
export function useBookChat(bookId: string, pageId: string) {
  const invalidate = useInvalidateBook();
  return useMutation({
    mutationFn: (message: string) =>
      apiFetch<BookChatResponse>(`/api/v1/books/${bookId}/pages/${pageId}/chat`, {
        method: "POST",
        body: JSON.stringify({ message }),
        signal: AbortSignal.timeout(BOOK_CHAT_TIMEOUT_MS),
      }),
    onSuccess: () => invalidate(bookId, pageId),
  });
}

/** 阅读进度点：进入页置 visited、书签开关。详情与页缓存都要刷（进度环跟着变）。 */
export function usePageFlags(bookId: string, pageId: string) {
  const invalidate = useInvalidateBook();
  return useMutation({
    mutationFn: (body: { visited?: boolean; bookmarked?: boolean }) =>
      apiFetch<{ visited: boolean; bookmarked: boolean }>(
        `/api/v1/books/${bookId}/pages/${pageId}`,
        { method: "PATCH", body: JSON.stringify(body) }
      ),
    onSuccess: () => invalidate(bookId, pageId),
  });
}

/** 测验作答：零 LLM 判分，返回对错与解析。 */
export function useAttempt(bookId: string, pageId: string) {
  const invalidate = useInvalidateBook();
  return useMutation({
    mutationFn: ({ blockId, answer }: { blockId: string; answer: string[] }) =>
      apiFetch<BookAttemptResponse>(`/api/v1/books/${bookId}/pages/${pageId}/attempts`, {
        method: "POST",
        body: JSON.stringify({ block_id: blockId, answer }),
      }),
    onSuccess: () => invalidate(bookId, pageId),
  });
}
