// Who is who: a recorded subject (`created_by`, `decided_by`, an audit actor) is either an e-mail (rows written by
// `eap-iam-cfaccess`, and legacy rows) or a stable id (`zitadel:<sub>`, rows written by `eap-iam-zitadel`). People read
// e-mails, so the UI shows the e-mail for both. The directory is filled from what the API already returns: the audit
// row's `actor_email`, the Members list (ZITADEL user id -> e-mail) and the caller's own capabilities. An id nobody has
// introduced yet (an approval by someone who has since left, read by a non-admin) shows as a short, labelled id.
// Legacy e-mail rows and new `zitadel:` rows of the same person are not merged: both show the same e-mail.

const SUBJECT_PREFIX = "zitadel:";
const known = new Map<string, string>();

export function rememberSubject(subject: string | null | undefined, email: string | null | undefined): void {
  if (subject && email && subject !== email) known.set(subject, email.toLowerCase());
}

/** Members come with the source's own user id; the principal subject of ZITADEL users is `zitadel:<id>`. */
export function rememberMember(memberId: string, email: string | null | undefined): void {
  rememberSubject(`${SUBJECT_PREFIX}${memberId}`, email);
}

export function resetSubjectsForTests(): void {
  known.clear();
}

/** The text to show for a recorded subject. */
export function subjectLabel(subject: string | null | undefined): string {
  if (!subject) return "";
  const email = known.get(subject);
  if (email) return email;
  if (subject.startsWith(SUBJECT_PREFIX)) return `ZITADEL user ${subject.slice(SUBJECT_PREFIX.length, SUBJECT_PREFIX.length + 8)}…`;
  return subject;
}
