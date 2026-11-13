/** A query for a route builder: URLSearchParams as built by a page's own filter code, or a plain
 * record. In a record, `null`, `undefined`, `false` and `""` are left out; key order is kept. */
export type RouteQuery =
  | URLSearchParams
  | Record<string, string | number | boolean | null | undefined>;

/** Thrown for an id that cannot be a path segment: exactly "." or "..", which URL parsers collapse
 * (as they do %2E and %2E%2E). Ids are server UUIDs and the API refuses dot-only ids, so this is a bug
 * to fail on loudly rather than a link to a different page. */
export class RouteIdError extends Error {
  constructor(public readonly id: string) {
    super(`route id cannot be '.' or '..' (got '${id}')`);
    this.name = "RouteIdError";
  }
}

/** One path segment, percent-encoded (ids may hold `/`, `:`, `?`, `#` or spaces). Throws RouteIdError
 * for "." and "..". */
export function seg(value: string | number): string {
  const s = String(value);
  if (s === "." || s === "..") throw new RouteIdError(s);
  return encodeURIComponent(s);
}

/** `path` plus `?query` when the query has any entry; the bare path otherwise. */
export function withQuery(path: string, query?: RouteQuery): string {
  if (!query) return path;
  let params: URLSearchParams;
  if (query instanceof URLSearchParams) {
    params = query;
  } else {
    params = new URLSearchParams();
    for (const [key, value] of Object.entries(query)) {
      if (value === null || value === undefined || value === false || value === "") continue;
      params.set(key, String(value));
    }
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}
