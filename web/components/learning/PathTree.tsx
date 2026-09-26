"use client";

/** 路径树（§7.5）：前序列表按 depth 缩进 + 行内编辑（标题/类型/上下移/删除）。
 *
 * 纯 REST、零 LLM（拍板 #2）：改树就是 PATCH/POST/DELETE，改完 invalidate 重取。
 * **没有锁**（门就是游标，没有「当前解锁节点」这回事）：下一目标（服务端算的
 * `next_target.node_id`）用 `data-next="true"` + 箭头高亮，已过门的节点用绿点。
 * 树是扁平前序数组（不是嵌套 DOM），父子关系靠 depth 表达，缩进多深由 lib 决定。
 */

import { useState } from "react";
import { ArrowDown, ArrowUp, ChevronRight, Pencil, Plus, Trash2 } from "lucide-react";
import { useI18n } from "@/hooks/useI18n";
import { useAddNode, useDeleteNode, useMoveNode, useUpdateNode } from "@/hooks/useLearning";
import {
  clearedOf,
  indentPx,
  masteryPercent,
  nodeStateKey,
  nodeTypeKey,
  NODE_TYPES,
} from "@/lib/learning";
import type { LearningNode } from "@/types/api";

interface Draft {
  title: string;
  nodeType: string;
  description: string;
}

const EMPTY_DRAFT: Draft = { title: "", nodeType: "concept", description: "" };

export default function PathTree({
  pathId,
  nodes,
  nextNodeId,
  selectedId,
  onSelect,
}: {
  pathId: string;
  nodes: LearningNode[];
  nextNodeId: string | null;
  selectedId: string | null;
  onSelect: (nodeId: string) => void;
}) {
  const { t } = useI18n();
  const update = useUpdateNode();
  const remove = useDeleteNode();
  const move = useMoveNode();
  const add = useAddNode();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [addParent, setAddParent] = useState<string | null>(null);
  const [addDraft, setAddDraft] = useState<Draft>(EMPTY_DRAFT);
  const [error, setError] = useState<string | null>(null);

  const siblings = (node: LearningNode) =>
    nodes.filter((item) => item.parent_id === node.parent_id);

  const startEdit = (node: LearningNode) => {
    setEditingId(node.id);
    setAddParent(null);
    setError(null);
    setDraft({ title: node.title, nodeType: node.node_type, description: node.description });
  };

  const saveEdit = async () => {
    if (!editingId) {
      return;
    }
    if (!draft.title.trim()) {
      setError(t("learning.titleRequired"));
      return;
    }
    try {
      await update.mutateAsync({
        id: editingId,
        title: draft.title.trim(),
        node_type: draft.nodeType,
        description: draft.description.trim(),
      });
      setEditingId(null);
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  };

  const saveAdd = async () => {
    if (!addDraft.title.trim()) {
      setError(t("learning.titleRequired"));
      return;
    }
    try {
      await add.mutateAsync({
        pathId,
        title: addDraft.title.trim(),
        node_type: addDraft.nodeType,
        description: addDraft.description.trim(),
        parent_id: addParent ? addParent : null, // "" = 加在根上
      });
      setAddParent(null);
      setAddDraft(EMPTY_DRAFT);
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  };

  const shift = async (node: LearningNode, direction: "up" | "down") => {
    try {
      await move.mutateAsync({ id: node.id, direction });
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  };

  const drop = async (node: LearningNode) => {
    if (!window.confirm(t("learning.deleteNodeConfirm", { title: node.title }))) {
      return;
    }
    try {
      await remove.mutateAsync(node.id);
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  };

  const draftFields = (value: Draft, onChange: (next: Draft) => void, prefix: string) => (
    <div className="flex flex-col gap-2">
      <div className="flex gap-2">
        <input
          data-testid={`${prefix}-title`}
          value={value.title}
          onChange={(event) => onChange({ ...value, title: event.target.value })}
          placeholder={t("learning.nodeTitlePlaceholder")}
          className="min-w-0 flex-1 rounded-lg border border-border bg-transparent px-2.5 py-1.5 text-sm outline-none focus:border-primary/50"
        />
        <select
          data-testid={`${prefix}-type`}
          value={value.nodeType}
          onChange={(event) => onChange({ ...value, nodeType: event.target.value })}
          className="rounded-lg border border-border bg-transparent px-2 py-1.5 text-xs outline-none focus:border-primary/50"
        >
          {NODE_TYPES.map((type) => (
            <option key={type} value={type}>
              {t(nodeTypeKey(type))}
            </option>
          ))}
        </select>
      </div>
      <input
        data-testid={`${prefix}-description`}
        value={value.description}
        onChange={(event) => onChange({ ...value, description: event.target.value })}
        placeholder={t("learning.nodeDescPlaceholder")}
        className="w-full rounded-lg border border-border bg-transparent px-2.5 py-1.5 text-xs outline-none focus:border-primary/50"
      />
    </div>
  );

  return (
    <div className="flex flex-col gap-2">
      <ul className="space-y-1" data-testid="l-tree">
        {nodes.map((node) => {
          const row = siblings(node);
          const index = row.findIndex((item) => item.id === node.id);
          const isNext = node.id === nextNodeId;
          const cleared = clearedOf(node);
          if (editingId === node.id) {
            return (
              <li
                key={node.id}
                data-testid="l-node-edit"
                className="rounded-xl border border-primary/40 bg-surface p-3"
              >
                {draftFields(draft, setDraft, "l-node-edit")}
                <div className="mt-2 flex items-center gap-2">
                  <button
                    type="button"
                    data-testid="l-node-save"
                    disabled={update.isPending}
                    onClick={() => void saveEdit()}
                    className="rounded-lg bg-primary px-3 py-1 text-xs text-primary-foreground disabled:opacity-40"
                  >
                    {t("common.save")}
                  </button>
                  <button
                    type="button"
                    onClick={() => setEditingId(null)}
                    className="rounded-lg px-2 py-1 text-xs text-muted hover:bg-accent"
                  >
                    {t("common.discard")}
                  </button>
                </div>
              </li>
            );
          }
          return (
            <li
              key={node.id}
              data-testid="l-node"
              data-id={node.id}
              data-depth={node.depth}
              data-state={node.state}
              data-next={isNext ? "true" : "false"}
              data-cleared={cleared ? "true" : "false"}
              data-selected={node.id === selectedId}
              style={{ paddingLeft: indentPx(node.depth) }}
              className="group"
            >
              <div
                className={`flex items-center gap-1.5 rounded-xl border px-2.5 py-1.5 transition-colors ${
                  node.id === selectedId
                    ? "border-primary/50 bg-primary/5"
                    : isNext
                      ? "border-primary/30 bg-primary/[0.03] hover:bg-accent/40"
                      : "border-border hover:bg-accent/40"
                }`}
              >
                <button
                  type="button"
                  data-testid="l-node-open"
                  onClick={() => onSelect(node.id)}
                  className="flex min-w-0 flex-1 items-center gap-2 text-left"
                >
                  {isNext ? (
                    <ChevronRight className="h-3.5 w-3.5 shrink-0 text-primary" />
                  ) : (
                    <span
                      className={`h-3.5 w-3.5 shrink-0 rounded-full ${
                        cleared ? "bg-success" : "bg-accent"
                      }`}
                    />
                  )}
                  <span className="min-w-0 flex-1 truncate text-sm">{node.title}</span>
                  <span className="shrink-0 rounded bg-accent/60 px-1.5 py-0.5 text-[10px] text-muted">
                    {t(nodeTypeKey(node.node_type))}
                  </span>
                  <span className="shrink-0 text-[11px] text-muted">
                    {t(nodeStateKey(node.state))} · {masteryPercent(node.mastery)}
                  </span>
                </button>
                <div className="flex shrink-0 items-center gap-0.5">
                  <button
                    type="button"
                    data-testid="l-node-up"
                    title={t("learning.moveUp")}
                    disabled={index <= 0 || move.isPending}
                    onClick={() => void shift(node, "up")}
                    className="rounded p-1 text-muted hover:bg-accent hover:text-foreground disabled:opacity-30"
                  >
                    <ArrowUp className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    data-testid="l-node-down"
                    title={t("learning.moveDown")}
                    disabled={index >= row.length - 1 || move.isPending}
                    onClick={() => void shift(node, "down")}
                    className="rounded p-1 text-muted hover:bg-accent hover:text-foreground disabled:opacity-30"
                  >
                    <ArrowDown className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    data-testid="l-node-add-child"
                    title={t("learning.addNode")}
                    onClick={() => {
                      setAddParent(node.id);
                      setAddDraft(EMPTY_DRAFT);
                      setEditingId(null);
                    }}
                    className="rounded p-1 text-muted hover:bg-accent hover:text-foreground"
                  >
                    <Plus className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    data-testid="l-node-edit-open"
                    title={t("learning.editNode")}
                    onClick={() => startEdit(node)}
                    className="rounded p-1 text-muted hover:bg-accent hover:text-foreground"
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    data-testid="l-node-delete"
                    title={t("learning.deleteNode")}
                    onClick={() => void drop(node)}
                    className="rounded p-1 text-muted hover:bg-accent hover:text-danger"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
            </li>
          );
        })}
      </ul>

      {addParent !== null ? (
        <div
          className="rounded-xl border border-primary/40 bg-surface p-3"
          data-testid="l-node-add"
        >
          <p className="mb-2 text-[11px] text-muted">
            {t("learning.addUnder", {
              title: nodes.find((item) => item.id === addParent)?.title ?? "",
            })}
          </p>
          {draftFields(addDraft, setAddDraft, "l-node-new")}
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              data-testid="l-node-add-save"
              disabled={add.isPending}
              onClick={() => void saveAdd()}
              className="rounded-lg bg-primary px-3 py-1 text-xs text-primary-foreground disabled:opacity-40"
            >
              {t("learning.createNode")}
            </button>
            <button
              type="button"
              onClick={() => setAddParent(null)}
              className="rounded-lg px-2 py-1 text-xs text-muted hover:bg-accent"
            >
              {t("common.discard")}
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          data-testid="l-node-add-root"
          onClick={() => {
            setAddParent("");
            setAddDraft(EMPTY_DRAFT);
            setEditingId(null);
          }}
          className="inline-flex w-fit items-center gap-1 rounded-lg px-2 py-1 text-xs text-muted transition-colors hover:bg-accent hover:text-foreground"
        >
          <Plus className="h-3.5 w-3.5" />
          {t("learning.addNode")}
        </button>
      )}

      {error && (
        <p data-testid="l-tree-error" className="text-xs text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
