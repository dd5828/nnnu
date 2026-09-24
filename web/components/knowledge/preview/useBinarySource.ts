"use client";

/**
 * 拉一个文件的原始字节（docx / xlsx 预览用）。
 *
 * docx-preview 和 exceljs 都要把整份文档收进内存再同步布局，所以上限压到
 * 25MB（文本预览那边是 8MB），超了直接劝下载。
 *
 * 护栏与 useTextSource 同款：裸 fetch + content-length 预检 + reqId 防竞态；
 * 换 url 靠结果自带的 url 键降级成 loading，不在 effect 里 setState。
 */

import { useEffect, useRef, useState } from "react";

export type BinarySourceState =
  | { kind: "loading" }
  | { kind: "ready"; buffer: ArrayBuffer }
  | { kind: "error"; code: "too_large" | "load" };

const MAX_BYTES = 25 * 1024 * 1024;

// 同 useTextSource：loading 态必须是同一个对象，消费方的 effect 依赖它。
const LOADING: BinarySourceState = { kind: "loading" };

interface BinaryLoad {
  url: string;
  state: BinarySourceState;
}

export function useBinarySource(url: string): BinarySourceState {
  const [loaded, setLoaded] = useState<BinaryLoad | null>(null);
  const reqIdRef = useRef(0);

  useEffect(() => {
    const reqId = ++reqIdRef.current;
    const controller = new AbortController();

    void (async () => {
      try {
        const resp = await fetch(url, { signal: controller.signal });
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status}`);
        }
        const lengthHeader = resp.headers.get("content-length");
        if (lengthHeader && Number(lengthHeader) > MAX_BYTES) {
          if (reqIdRef.current === reqId) {
            setLoaded({ url, state: { kind: "error", code: "too_large" } });
          }
          return;
        }
        const buffer = await resp.arrayBuffer();
        if (reqIdRef.current === reqId) {
          setLoaded({ url, state: { kind: "ready", buffer } });
        }
      } catch {
        if (controller.signal.aborted || reqIdRef.current !== reqId) {
          return;
        }
        setLoaded({ url, state: { kind: "error", code: "load" } });
      }
    })();

    return () => controller.abort();
  }, [url]);

  return loaded && loaded.url === url ? loaded.state : LOADING;
}
