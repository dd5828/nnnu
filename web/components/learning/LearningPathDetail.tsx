"use client";

/** 路径详情 / 学习看板（§7.5）：树 + 节点仪表 + 薄弱点 + 复习建议 + 开始学习会话。
 *
 * 三块数据一个响应（后端把看板并进了 `GET /learning/paths/{id}`），所以这页只有
 * 一条查询；选中节点走 URL（`?node=<id>`），深链可分享，也方便从薄弱点跳回来。
 *
 * **没有推进按钮**（门就是游标）：服务端每回合现算 `next_target`，这页只把
 * 「下一目标 + 为什么要做它」摆出来（`l-next` 面板），真学还得去聊天里学。
 *
 * 「开始学习会话」= 起一个 mastery_path 回合（正文在 WS 上流）→ `attachSession`
 * 把它接上 → 跳聊天页看讲解。REST 起的回合不会自己进客户端的订阅，不 attach 就是空转。
 */

import { useMemo, useState } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, ListChecks, Loader2, MessageSquare, Pencil, Play, Trash2 } from "lucide-react";
import QuestionCard from "@/components/quiz/QuestionCard";
import { useI18n } from "@/hooks/useI18n";
import { useChatStore } from "@/hooks/useChat";
import {
  useDeletePath,
  useLearningPath,
  useStartSession,
  useUpdatePath,
} from "@/hooks/useLearning";
import { useQuestionList } from "@/hooks/useQuestions";
import { useNow } from "@/hooks/useNow";
import {
  gateKey,
  nextActionKey,
  nodeStateKey,
  nodeTypeKey,
  strategyKey,
  weakPointHref,
} from "@/lib/learning";
import type { LearningNode } from "@/types/api";
import MasteryRing from "./MasteryRing";
import PathTree from "./PathTree";
import ReviewList from "./ReviewList";
import WeakPointList from "./WeakPointList";

export default function LearningPathDetail() {
  const { t, lang } = useI18n();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const pathId = params?.id ?? null;
  const attachSession = useChatStore((s) => s.attachSession);

  const { data, isLoading, error } = useLearningPath(pathId);
  const startSession = useStartSession();
  const updatePath = useUpdatePath();
  const removePath = useDeletePath();

  const [manualNode, setManualNode] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [pathEditing, setPathEditing] = useState(false);
  const [pathDraft, setPathDraft] = useState({ title: "", summary: "" });

  const nodes = useMemo(() => data?.nodes ?? [], [data]);
  const now = useNow();
  const nextTarget = data?.next_target ?? null;
  const nextId = nextTarget?.node_id ?? null;
  const selected = useMemo<LearningNode | null>(() => {
    const wanted = manualNode ?? search.get("node");
    return (
      nodes.find((node) => node.id === wanted) ??
      nodes.find((node) => node.id === nextId) ??
      nodes[0] ??
      null
    );
  }, [nodes, manualNode, search, nextId]);

  // 节点题目：只在这个节点真的存在时才查（空 node_id 会变成「全部题目」，白拉一页）
  const { data: questionData } = useQuestionList({
    filter: "all",
    nodeId: selected?.id ?? "",
    enabled: Boolean(selected?.id),
  });
  const questions = questionData?.questions ?? [];
  const answered = questions.filter((item) => item.last_attempt_at !== null).length;

  const stats = data?.stats;

  const start = async () => {
    if (!pathId) {
      return;
    }
    setNotice(null);
    try {
      const response = await startSession.mutateAsync({ pathId, language: lang });
      attachSession(response.session_id); // REST 起的回合要显式订阅，否则页面空转
      router.push("/");
    } catch (err) {
      setNotice(String(err));
    }
  };

  const savePath = async () => {
    if (!pathId) {
      return;
    }
    try {
      await updatePath.mutateAsync({
        id: pathId,
        title: pathDraft.title.trim(),
        summary: pathDraft.summary.trim(),
      });
      setPathEditing(false);
      setNotice(null);
    } catch (err) {
      setNotice(String(err));
    }
  };

  const dropPath = async () => {
    if (
      !pathId ||
      !window.confirm(t("learning.deletePathConfirm", { title: data?.path.title ?? "" }))
    ) {
      return;
    }
    try {
      await removePath.mutateAsync(pathId);
      router.push("/learning");
    } catch (err) {
      setNotice(String(err));
    }
  };

  if (isLoading) {
    return (
      <main className="flex flex-1 items-center justify-center">
        <Loader2 className="h-5 w-5 animate-spin text-muted" />
      </main>
    );
  }

  if (error || !data) {
    return (
      <main className="flex flex-1 flex-col items-center justify-center gap-3">
        <p className="text-sm text-danger">{String(error ?? t("learning.notFound"))}</p>
        <Link href="/learning" className="text-xs text-primary hover:underline">
          {t("learning.backToList")}
        </Link>
      </main>
    );
  }

  const path = data.path;
  const gateText = selected ? gateKey(selected) : null;

  return (
    <main className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex max-w-4xl flex-col gap-4 px-4 py-6">
        <div className="flex items-start gap-3">
          <Link
            href="/learning"
            className="mt-0.5 rounded-lg p-1 text-muted transition-colors hover:bg-accent hover:text-foreground"
            title={t("learning.backToList")}
          >
            <ArrowLeft className="h-4 w-4" />
          </Link>
          {pathEditing ? (
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <input
                data-testid="l-path-title"
                value={pathDraft.title}
                onChange={(event) => setPathDraft({ ...pathDraft, title: event.target.value })}
                className="rounded-lg border border-border bg-transparent px-2.5 py-1.5 text-sm outline-none focus:border-primary/50"
              />
              <input
                data-testid="l-path-summary"
                value={pathDraft.summary}
                onChange={(event) => setPathDraft({ ...pathDraft, summary: event.target.value })}
                placeholder={t("learning.summaryPlaceholder")}
                className="rounded-lg border border-border bg-transparent px-2.5 py-1.5 text-xs outline-none focus:border-primary/50"
              />
              <div className="flex gap-2">
                <button
                  type="button"
                  data-testid="l-path-save"
                  onClick={() => void savePath()}
                  className="rounded-lg bg-primary px-3 py-1 text-xs text-primary-foreground"
                >
                  {t("common.save")}
                </button>
                <button
                  type="button"
                  onClick={() => setPathEditing(false)}
                  className="rounded-lg px-2 py-1 text-xs text-muted hover:bg-accent"
                >
                  {t("common.discard")}
                </button>
              </div>
            </div>
          ) : (
            <div className="min-w-0 flex-1">
              <h1 className="truncate text-xl font-semibold" data-testid="l-path-title-text">
                {path.title}
              </h1>
              <p className="mt-0.5 text-xs text-muted">
                {path.topic}
                {path.summary ? ` · ${path.summary}` : ""}
              </p>
            </div>
          )}
          {!pathEditing && (
            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                data-testid="l-path-edit"
                onClick={() => {
                  setPathDraft({ title: path.title, summary: path.summary ?? "" });
                  setPathEditing(true);
                }}
                title={t("learning.editPath")}
                className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-foreground"
              >
                <Pencil className="h-4 w-4" />
              </button>
              <button
                type="button"
                data-testid="l-path-delete"
                onClick={() => void dropPath()}
                title={t("learning.deletePath")}
                className="rounded-lg p-1.5 text-muted transition-colors hover:bg-accent hover:text-danger"
              >
                <Trash2 className="h-4 w-4" />
              </button>
            </div>
          )}
        </div>

        {/* 汇总条：进度 + 四态 + 弱点数 */}
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-border bg-surface px-3 py-2.5 text-xs">
          <span className="inline-flex items-center gap-2">
            <span>{t("learning.progress")}</span>
            <span className="inline-block h-1.5 w-24 overflow-hidden rounded-full bg-accent">
              <span
                data-testid="l-progress"
                data-value={stats ? Math.round(stats.progress * 100) : 0}
                className="block h-full rounded-full bg-primary"
                style={{ width: `${Math.round((stats?.progress ?? 0) * 100)}%` }}
              />
            </span>
            <span>{Math.round((stats?.progress ?? 0) * 100)}%</span>
          </span>
          <span data-testid="l-stats" className="flex flex-wrap items-center gap-2 text-muted">
            <span>{t("learning.statMastered", { n: String(stats?.mastered ?? 0) })}</span>
            <span>{t("learning.statLearning", { n: String(stats?.learning ?? 0) })}</span>
            <span>{t("learning.statNotStarted", { n: String(stats?.not_started ?? 0) })}</span>
            <span>{t("learning.statDue", { n: String(stats?.due ?? 0) })}</span>
            <span className={stats?.weak ? "text-danger" : undefined}>
              {t("learning.statWeak", { n: String(stats?.weak ?? 0) })}
            </span>
          </span>
          <div className="ml-auto flex items-center gap-2">
            <button
              type="button"
              data-testid="l-start-session"
              onClick={() => void start()}
              disabled={startSession.isPending}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
            >
              {startSession.isPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Play className="h-3.5 w-3.5" />
              )}
              {t("learning.startSession")}
            </button>
          </div>
        </div>

        {/* 下一目标：服务端每回合现算，这里只摆出来（没有推进按钮——门就是游标） */}
        <section
          data-testid="l-next"
          data-action={nextTarget?.action ?? "complete"}
          data-node-id={nextTarget?.node_id ?? ""}
          className="flex flex-wrap items-center gap-3 rounded-xl border border-primary/30 bg-primary/[0.03] px-3 py-2.5"
        >
          <span className="text-xs font-medium text-primary">{t("learning.nextTarget")}</span>
          <span className="text-xs" data-testid="l-next-action">
            {t(nextActionKey(nextTarget?.action))}
          </span>
          {nextTarget?.node_title && (
            <span className="text-sm" data-testid="l-next-title">
              {nextTarget.node_title}
            </span>
          )}
          {nextTarget?.node_type && (
            <span className="rounded bg-accent/60 px-1.5 py-0.5 text-[10px] text-muted">
              {t(nodeTypeKey(nextTarget.node_type))}
            </span>
          )}
          {nextTarget && nextTarget.action !== "complete" && (
            <span className="text-[11px] text-muted">
              {nextTarget.gate_kind === "quantitative" && nextTarget.gate !== null
                ? t("learning.gateQuantitative", { gate: String(Math.round(nextTarget.gate)) })
                : t("learning.gateQualitative")}
              {" · "}
              {t("learning.masteryLine", { n: String(Math.round(nextTarget.mastery)) })}
            </span>
          )}
          <button
            type="button"
            data-testid="l-next-go"
            onClick={() => void start()}
            disabled={startSession.isPending}
            className="ml-auto inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-xs text-primary-foreground transition-colors hover:opacity-90 disabled:opacity-40"
          >
            <MessageSquare className="h-3.5 w-3.5" />
            {t("learning.goToChat")}
          </button>
          {nextTarget?.reason && (
            <p className="w-full text-[11px] text-muted" data-testid="l-next-reason">
              {nextTarget.reason}
            </p>
          )}
        </section>

        {notice && <p className="text-xs text-danger">{notice}</p>}

        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <section className="flex flex-col gap-2">
            <h2 className="text-sm font-medium">{t("learning.tree")}</h2>
            <PathTree
              pathId={path.id}
              nodes={nodes}
              nextNodeId={nextId}
              selectedId={selected?.id ?? null}
              onSelect={(nodeId) => setManualNode(nodeId)}
            />
          </section>

          <section className="flex flex-col gap-2" data-testid="l-node-detail">
            {selected ? (
              <div className="flex flex-col gap-3 rounded-xl border border-border bg-surface p-3">
                <div className="flex items-start gap-3">
                  <MasteryRing
                    mastery={selected.mastery}
                    state={selected.state}
                    gate={selected.gate}
                    gateKind={selected.gate_kind}
                    cleared={selected.cleared}
                  />
                  <div className="min-w-0 flex-1">
                    <h3 className="truncate text-sm font-medium">{selected.title}</h3>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-muted">
                      <span className="rounded bg-accent/60 px-1.5 py-0.5">
                        {t(nodeTypeKey(selected.node_type))}
                      </span>
                      <span data-testid="l-node-state" data-state={selected.state}>
                        {t(nodeStateKey(selected.state))}
                      </span>
                      <span data-testid="l-node-gate" data-gate-kind={selected.gate_kind}>
                        {gateText ? t(gateText.key, gateText.vars) : ""}
                      </span>
                      <span>
                        {t("learning.stageLine", { n: String(selected.review_stage + 1) })}
                      </span>
                      {selected.id === nextId && (
                        <span className="text-primary">{t("learning.nextNode")}</span>
                      )}
                    </div>
                    <p className="mt-2 text-xs text-muted">{t(strategyKey(selected.node_type))}</p>
                    {selected.description && (
                      <p className="mt-1 text-xs text-muted">{selected.description}</p>
                    )}
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-3 border-t border-border/60 pt-2 text-[11px] text-muted">
                  <span data-testid="l-node-questions">
                    {t("learning.questionStats", {
                      total: String(questions.length),
                      answered: String(answered),
                    })}
                  </span>
                  <Link
                    href={weakPointHref(selected.id)}
                    className="inline-flex items-center gap-1 text-primary hover:underline"
                  >
                    <ListChecks className="h-3 w-3" />
                    {t("learning.openInBank")}
                  </Link>
                </div>

                {questions.length > 0 && (
                  <ul className="space-y-2" data-testid="l-node-question-list">
                    {questions.map((question) => (
                      <QuestionCard key={question.id} question={question} language={lang} />
                    ))}
                  </ul>
                )}
              </div>
            ) : (
              <p className="text-xs text-muted">{t("learning.noNodes")}</p>
            )}
          </section>
        </div>

        <section className="flex flex-col gap-2">
          <h2 className="text-sm font-medium">{t("learning.weakPoints")}</h2>
          <WeakPointList points={data.weak_points} />
        </section>

        <section className="flex flex-col gap-2">
          <h2 className="text-sm font-medium">{t("learning.reviews")}</h2>
          <ReviewList items={data.reviews} now={now} />
        </section>
      </div>
    </main>
  );
}
