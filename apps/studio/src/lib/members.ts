// The Members page's API: GET/POST /api/v1/iam/members, PATCH/DELETE /api/v1/iam/members/{id} (design 7.3). The change is
// made in the identity provider (ZITADEL); the API answers 404 when no membership source is configured.
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import { rememberMember } from "./subjects";

export const MEMBER_ROLES = ["admin", "editor", "viewer"] as const;
export type MemberRole = (typeof MEMBER_ROLES)[number];

export interface Member {
  id: string;
  email: string | null;
  display_name: string | null;
  roles: string[];
  status: string;
}

export interface MemberList {
  items: Member[];
  /** False: read only, "managed in the identity provider". */
  mutable: boolean;
}

const URL_BASE = `${BASE_URL}/api/v1/iam/members`;
const JSON_HEADERS = { "Content-Type": "application/json" };

export async function listMembers(): Promise<MemberList> {
  const response = await fetch(URL_BASE, { headers: apiHeaders(), cache: "no-store" });
  if (!response.ok) throw await apiErrorFromResponse(response);
  const body = (await response.json()) as Partial<MemberList>;
  const items = body.items ?? [];
  for (const member of items) rememberMember(member.id, member.email); // so `decided_by` / audit subjects show as e-mails
  return { items, mutable: body.mutable === true };
}

export async function inviteMember(email: string, role: MemberRole): Promise<Member> {
  const response = await fetch(URL_BASE, { method: "POST", headers: apiHeaders(JSON_HEADERS), body: JSON.stringify({ email: email.trim(), role }) });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as Member;
}

export async function changeMemberRole(id: string, role: MemberRole): Promise<Member> {
  const response = await fetch(`${URL_BASE}/${encodeURIComponent(id)}`, { method: "PATCH", headers: apiHeaders(JSON_HEADERS), body: JSON.stringify({ role }) });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as Member;
}

export async function removeMember(id: string): Promise<void> {
  const response = await fetch(`${URL_BASE}/${encodeURIComponent(id)}`, { method: "DELETE", headers: apiHeaders() });
  if (!response.ok) throw await apiErrorFromResponse(response);
}

/** The member's single role for the picker (the highest if the IdP reports several), or "" when it has none of ours. */
export function primaryRole(member: Member): MemberRole | "" {
  return MEMBER_ROLES.find((role) => member.roles.includes(role)) ?? "";
}
