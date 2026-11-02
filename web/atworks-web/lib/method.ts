// HTTP method → text color, one table for every screen that prints a method.
// The palette follows the common API-tool convention (read = blue, create = green,
// replace = amber, partial update = violet, delete = red) using the portal's own tokens.

const METHOD_TONE: Record<string, string> = {
  GET: "text-(--info)",
  POST: "text-(--ok)",
  PUT: "text-(--warn)",
  PATCH: "text-(--violet)",
  DELETE: "text-(--danger)",
};

/** Tailwind text-color class for a method; unknown methods keep the muted label color. */
export function methodTone(method: string | null | undefined): string {
  return METHOD_TONE[(method ?? "").toUpperCase()] ?? "text-(--ink-soft)";
}
