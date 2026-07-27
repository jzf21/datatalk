/**
 * The introspection scope of one source, between wire form and picker form.
 *
 * On the wire the scope is two flat string arrays, because that is what the
 * database columns are. In the picker it is a map of namespace to selection.
 * The translation is not quite mechanical, so it lives here with tests rather
 * than inside a component:
 *
 * - An empty scope means the whole server. That is what every source did before
 *   the picker existed, so it has to survive a round trip untouched.
 * - `introspect_tables` is scoped *per namespace*. A namespace with entries
 *   shows only those tables; a namespace with none shows all of its tables. So
 *   "all" is the absence of entries, not a list of every table -- which is what
 *   lets a whole-namespace selection keep picking up tables created later.
 */

/** Every table in this namespace, including ones created after this was saved. */
export const ALL = "all" as const;

/** What is selected inside one namespace. */
export type NamespaceSelection = typeof ALL | ReadonlySet<string>;

/** Namespace name to selection. A namespace absent from the map is not in scope. */
export type Scope = ReadonlyMap<string, NamespaceSelection>;

export interface ScopeArrays {
  introspect_databases: string[];
  introspect_tables: string[];
}

/** True when nothing is narrowed: the source sees the whole server. */
export function isUnscoped(scope: Scope): boolean {
  return scope.size === 0;
}

/**
 * Wire arrays to picker map.
 *
 * A `namespace.table` entry naming a namespace that is not in
 * `introspect_databases` is dropped: the API applies the namespace allowlist
 * first, so such an entry selects nothing and showing it as selected would lie.
 */
export function parseScope(
  introspectDatabases: readonly string[] | undefined,
  introspectTables: readonly string[] | undefined,
): Scope {
  const namespaces = (introspectDatabases ?? []).filter((n) => n.trim());
  if (namespaces.length === 0) return new Map();

  const byNamespace = new Map<string, Set<string>>();
  for (const entry of introspectTables ?? []) {
    const dot = entry.indexOf(".");
    if (dot <= 0 || dot === entry.length - 1) continue;
    const namespace = entry.slice(0, dot).trim();
    const table = entry.slice(dot + 1).trim();
    if (!namespace || !table) continue;
    const existing = byNamespace.get(namespace);
    if (existing) existing.add(table);
    else byNamespace.set(namespace, new Set([table]));
  }

  const scope = new Map<string, NamespaceSelection>();
  for (const namespace of namespaces) {
    const tables = byNamespace.get(namespace);
    scope.set(namespace, tables && tables.size > 0 ? tables : ALL);
  }
  return scope;
}

/**
 * Picker map to wire arrays.
 *
 * A namespace selected down to zero tables is dropped entirely rather than
 * written as an empty selection -- an empty selection would round-trip through
 * `parseScope` as ALL, quietly re-including everything the user just cleared.
 */
export function toScopeArrays(scope: Scope): ScopeArrays {
  const databases: string[] = [];
  const tables: string[] = [];

  for (const [namespace, selection] of scope) {
    if (selection !== ALL && selection.size === 0) continue;
    databases.push(namespace);
    if (selection === ALL) continue;
    for (const table of [...selection].sort()) {
      tables.push(`${namespace}.${table}`);
    }
  }

  return {
    introspect_databases: databases.sort(),
    introspect_tables: tables.sort(),
  };
}

/** Set one namespace's selection, removing it from scope when cleared. */
export function withNamespace(
  scope: Scope,
  namespace: string,
  selection: NamespaceSelection | null,
): Scope {
  const next = new Map(scope);
  if (selection === null || (selection !== ALL && selection.size === 0)) {
    next.delete(namespace);
  } else {
    next.set(namespace, selection);
  }
  return next;
}

/** Toggle one table, expanding an implicit ALL into the explicit list first. */
export function toggleTable(
  scope: Scope,
  namespace: string,
  table: string,
  allTables: readonly string[],
): Scope {
  const current = scope.get(namespace);
  // Unticking one table out of ALL has to name the survivors, since ALL cannot
  // express an exception.
  const explicit = new Set(current === ALL ? allTables : (current ?? []));
  if (explicit.has(table)) explicit.delete(table);
  else explicit.add(table);

  // Back to every table means back to ALL, so the namespace resumes tracking
  // tables added later rather than freezing today's list.
  if (allTables.length > 0 && explicit.size === allTables.length) {
    return withNamespace(scope, namespace, ALL);
  }
  return withNamespace(scope, namespace, explicit);
}

export type CheckState = "checked" | "partial" | "unchecked";

export function namespaceState(scope: Scope, namespace: string): CheckState {
  const selection = scope.get(namespace);
  if (selection === undefined) return "unchecked";
  if (selection === ALL) return "checked";
  return selection.size === 0 ? "unchecked" : "partial";
}

export function isTableSelected(
  scope: Scope,
  namespace: string,
  table: string,
): boolean {
  const selection = scope.get(namespace);
  if (selection === undefined) return false;
  return selection === ALL || selection.has(table);
}

/**
 * One line for the source list. `total` is the number of namespaces on the
 * server when known -- it is not, until someone has run a discovery.
 */
export function scopeSummary(scope: Scope, total?: number): string {
  if (isUnscoped(scope)) return "All databases";

  const namespaces = scope.size;
  const narrowed = [...scope.values()].filter((s) => s !== ALL).length;
  const head =
    total !== undefined
      ? `${namespaces} of ${total} databases`
      : `${namespaces} database${namespaces === 1 ? "" : "s"}`;

  return narrowed === 0
    ? head
    : `${head}, ${narrowed} narrowed to selected tables`;
}
