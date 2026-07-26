/**
 * The workspace context model: the curated markdown the agents read.
 *
 * Mirrors `datatalk/web/routes_datacontext.py`. File paths travel as a query
 * parameter, never a path segment -- `ontology/customers.md` contains a slash.
 */

import { apiDelete, apiGet, apiPost, apiPut } from "./client";
import { streamNdjson } from "./stream";

export interface ContextFileSummary {
  path: string;
  summary: string;
  origin: string;
  /** Body differs from what the generator last wrote: a person owns this file. */
  human_owned: boolean;
  bytes: number;
  generated_at: string;
  edited_at: string;
  updated_at: string;
}

export interface ContextFileDetail extends ContextFileSummary {
  body_md: string;
  covers: { source: string; table: string }[];
  evidence: { source?: string; sql?: string; row_count?: number }[];
}

export interface ContextDocMeta {
  model: string;
  generated_at: string;
  stats: Record<string, unknown>;
}

export interface ContextResponse {
  doc: ContextDocMeta | null;
  files: ContextFileSummary[];
  /** Literally the bytes this contributes to every prompt. */
  tree_preview: string;
}

/**
 * Context generation is its own protocol. It reuses the NDJSON transport and
 * the `status`/`sql`/`error`/`done` vocabulary, but `saved` carries a different
 * payload than a report run's, so it gets its own union rather than widening
 * `RunEvent` with fields that never arrive.
 */
export type ContextRunEvent =
  | { kind: "status"; data: { message: string } }
  | {
      kind: "context_plan";
      data: { entities: string[]; playbooks: string[] };
    }
  | { kind: "sql"; data: { sql: string; source?: string } }
  | {
      kind: "result";
      data: { dataset_id: string; row_count: number; columns: string[] };
    }
  | { kind: "file"; data: { path: string; summary: string; revised: boolean } }
  | { kind: "error"; data: { message: string } }
  | {
      kind: "saved";
      data: { files: number; model: string; stats: Record<string, unknown> };
    }
  | { kind: "done"; data: Record<string, never> };

const base = (orgId: string) => `/api/orgs/${orgId}/context`;
const filePath = (orgId: string, path: string) =>
  `${base(orgId)}/file?path=${encodeURIComponent(path)}`;

export function getContext(orgId: string, signal?: AbortSignal) {
  return apiGet<ContextResponse>(base(orgId), signal);
}

export function getContextFile(orgId: string, path: string) {
  return apiGet<ContextFileDetail>(filePath(orgId, path));
}

export function updateContextFile(
  orgId: string,
  path: string,
  body: { body_md: string; summary?: string },
) {
  return apiPut<ContextFileDetail>(filePath(orgId, path), body);
}

export function createContextFile(
  orgId: string,
  body: { path: string; summary?: string; body_md?: string },
) {
  return apiPost<ContextFileDetail>(`${base(orgId)}/file`, body);
}

export function deleteContextFile(orgId: string, path: string) {
  return apiDelete<void>(filePath(orgId, path));
}

export function generateContext(
  orgId: string,
  body: { mode: "revise" | "replace"; confirm_overwrite?: boolean },
  signal?: AbortSignal,
) {
  return streamNdjson<ContextRunEvent>(`${base(orgId)}/generate`, body, signal);
}
