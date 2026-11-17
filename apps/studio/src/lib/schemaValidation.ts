import type { JsonSchema } from "./tools";

/** Messages never contain the value, only the field and the rule. */
function label(name: string, schema: JsonSchema): string {
  return schema.title || name.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

function isEmpty(value: unknown): boolean {
  return value === undefined || value === null || value === "";
}

function check(schema: JsonSchema, value: unknown, name: string, path: string[], errors: Record<string, string>): void {
  const key = path.join(".");
  const name_ = label(name, schema);
  if (schema.type === "object") {
    const record = value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
    for (const [child, childSchema] of Object.entries(schema.properties ?? {})) {
      const childValue = record[child];
      if (isEmpty(childValue)) {
        if ((schema.required ?? []).includes(child) && childSchema.type !== "boolean") {
          errors[[...path, child].join(".")] = `${label(child, childSchema)} is required`;
        }
        continue;
      }
      check(childSchema, childValue, child, [...path, child], errors);
    }
    return;
  }
  if (isEmpty(value)) return;
  if (schema.enum && !schema.enum.includes(value as string | number)) {
    errors[key] = `${name_} must be one of the listed options`;
    return;
  }
  if (schema.type === "integer" && !Number.isInteger(value)) errors[key] = `${name_} must be an integer`;
  else if (schema.type === "number" && (typeof value !== "number" || !Number.isFinite(value))) errors[key] = `${name_} must be a number`;
  else if (schema.type === "string" && typeof value !== "string") errors[key] = `${name_} must be text`;
  else if (schema.type === "boolean" && typeof value !== "boolean") errors[key] = `${name_} must be true or false`;
  else if (schema.type === "array") {
    if (!Array.isArray(value)) errors[key] = `${name_} must be a list`;
    else if (schema.items) value.forEach((item, index) => check(schema.items as JsonSchema, item, name, [...path, String(index)], errors));
    return;
  }
  if (errors[key]) return;
  if (typeof value === "number") {
    if (typeof schema.minimum === "number" && value < schema.minimum) errors[key] = `${name_} must be at least ${schema.minimum}`;
    else if (typeof schema.maximum === "number" && value > schema.maximum) errors[key] = `${name_} must be at most ${schema.maximum}`;
  }
}

/** Checks `value` against the schema subset the form renders (required, type, enum, bounds, nested
 * objects, arrays). Returns messages keyed by dotted path ("address.city"); empty when valid. */
export function validateAgainstSchema(schema: JsonSchema, value: Record<string, unknown>): Record<string, string> {
  const errors: Record<string, string> = {};
  check(schema, value, "", [], errors);
  return errors;
}

/** The server's 422 `detail` ([{loc: ["body","data",...], msg, type}]) as messages keyed by dotted
 * path, for the form's `errors` prop. Errors outside body.data are left out. */
export function serverFieldErrors(details: ReadonlyArray<{ loc: ReadonlyArray<string | number>; msg: string }>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const { loc, msg } of details) {
    if (loc[0] !== "body" || loc[1] !== "data") continue;
    const path = loc.slice(2).map(String).join(".");
    out[path || "_"] = msg;
  }
  return out;
}
