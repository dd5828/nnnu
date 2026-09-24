"use client";

/**
 * 拉一个文本 URL 的内容（markdown / 代码 / 纯文本预览用）。
 *
 * 用裸 fetch 而不是 apiFetch：后者会 resp.json()，而这里要的是原始文本
 * （且相对路径经 Next proxy 反代，同源 cookie 自动带上）。
 *
 * 两个护栏：
 * - content-length 超过 8MB 直接报 too_large（不把响应体读完）；
 * - reqId + AbortController 双保险：切文档时旧请求的结果不许覆盖新的
 *   （abort 只是省流量，竞态还得靠 reqId 判）。
 *
 * 换 url 时不做「重置成 loading」的 setState——结果自带 url 键，对不上就
 * 天然算 loading（也顺手躲开 setState-in-effect 那条 lint 规则）。
 */

import { useEffect, useRef, useState } from "react";

export type TextSourceState =
  | { kind: "loading" }
  | { kind: "ready"; text: string }
  | { kind: "error"; code: "too_large" | "load" };

const MAX_TEXT_BYTES = 8 * 1024 * 1024;

// 模块级常量：loading 态每次渲染都得是同一个对象，否则消费方的 effect
// 以返回值为依赖时会每次渲染都重跑。
const LOADING: TextSourceState = { kind: "loading" };

interface TextLoad {
  url: string;
  state: TextSourceState;
}

export function useTextSource(url: string): TextSourceState {
  const [loaded, setLoaded] = useState<TextLoad | null>(null);
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
        if (lengthHeader && Number(lengthHeader) > MAX_TEXT_BYTES) {
          if (reqIdRef.current === reqId) {
            setLoaded({ url, state: { kind: "error", code: "too_large" } });
          }
          return;
        }
        const text = await resp.text();
        if (reqIdRef.current === reqId) {
          setLoaded({ url, state: { kind: "ready", text } });
        }
      } catch {
        // abort 是切文档/卸载的正常路径，不报错；过期请求也不许改状态
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
