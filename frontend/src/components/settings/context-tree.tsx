"use client";

import { BookOpen, FileText, Pencil } from "lucide-react";

import { cn } from "@/lib/utils";
import type { ContextFileSummary } from "@/lib/api/datacontext";

/** `ontology/customers.md` -> `customers`. */
function leaf(path: string): string {
  return path.split("/").pop()?.replace(/\.md$/, "") ?? path;
}

const FOLDERS = [
  { prefix: "ontology/", label: "ontology", icon: BookOpen },
  { prefix: "playbooks/", label: "playbooks", icon: FileText },
] as const;

/**
 * The file tree, shaped like the thing it documents: a directory listing.
 *
 * Deliberately shows each file's summary, because that one line is exactly what
 * reaches every agent prompt -- seeing it here is how an editor knows whether it
 * is doing its job.
 */
export function ContextTree({
  files,
  selected,
  onSelect,
}: {
  files: ContextFileSummary[];
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  const root = files.filter((f) => !f.path.includes("/"));

  return (
    <nav aria-label="Context model" className="space-y-3 text-[13px]">
      <p className="label-caps text-ink-tertiary">context</p>

      <ul className="space-y-px">
        {root.map((f) => (
          <FileRow
            key={f.path}
            file={f}
            active={f.path === selected}
            onSelect={onSelect}
          />
        ))}
      </ul>

      {FOLDERS.map(({ prefix, label, icon: Icon }) => {
        const inFolder = files.filter((f) => f.path.startsWith(prefix));
        if (inFolder.length === 0) return null;
        return (
          <div key={prefix}>
            <p className="flex items-center gap-1.5 py-1 text-ink-tertiary">
              <Icon className="size-3.5" aria-hidden />
              {label}
            </p>
            <ul className="space-y-px border-l border-border pl-2">
              {inFolder.map((f) => (
                <FileRow
                  key={f.path}
                  file={f}
                  active={f.path === selected}
                  onSelect={onSelect}
                />
              ))}
            </ul>
          </div>
        );
      })}
    </nav>
  );
}

function FileRow({
  file,
  active,
  onSelect,
}: {
  file: ContextFileSummary;
  active: boolean;
  onSelect: (path: string) => void;
}) {
  return (
    <li>
      <button
        type="button"
        onClick={() => onSelect(file.path)}
        aria-current={active ? "true" : undefined}
        title={file.summary || file.path}
        className={cn(
          "flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left transition-colors duration-[120ms]",
          active
            ? "bg-accent font-medium text-ink-primary"
            : "text-ink-secondary hover:bg-accent/60 hover:text-ink-primary",
        )}
      >
        <span className="truncate">{leaf(file.path)}</span>
        {file.human_owned && (
          // Not colour alone: the icon carries the meaning, the label carries it
          // to a screen reader.
          <Pencil
            className="size-3 shrink-0 text-ink-tertiary"
            aria-label="edited by a person"
          />
        )}
      </button>
    </li>
  );
}
