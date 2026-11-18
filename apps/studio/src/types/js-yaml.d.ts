// The part of js-yaml (safe `load`) Studio uses; the package ships no types and none are mirrored offline.
declare module "js-yaml" {
  export function load(text: string): unknown;
}
