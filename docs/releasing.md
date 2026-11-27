# Releasing

A release is a **named snapshot** of `main`: a version, a git tag `vX.Y.Z`, a CHANGELOG section and a GitHub
Release. It is decoupled from deploys. Every merge to `main` can still be deployed on its own; a release only
names a commit that was good enough to point at. Nothing in the release workflows deploys anything, and a deploy
does not wait for a release. The deploy workflow records the version it deployed (`<version>+<commit>`, the same
string as `platform_version`) in its run summary.

## Version policy

One platform version, [SemVer](https://semver.org/) `MAJOR.MINOR.PATCH`. The root `pyproject.toml` `version` is the
source of truth. `scripts/release/bump.py` writes the same version into every workspace `pyproject.toml` (apps,
packages, plugins; spikes keep their own), `apps/studio/package.json` and the package entries of `uv.lock`, and CI
runs `bump.py --check`, which fails when any of them disagrees. Images report `platform_version` as
`<version>+<commit>`; the `+<commit>` part is added at build time and is never part of the file version. New workspace members start at the current platform version, not `0.1.0`.

The platform is **0.x** until its HTTP API, the stored definitions and the plug-in interfaces are declared stable.
Before 1.0:

| Bump | When |
|---|---|
| **minor** (`0.2.0` → `0.3.0`) | New capabilities, anything a user or operator notices, a breaking change to the API, a stored definition or a plug-in interface (say so under the change's entry). The default. |
| **patch** (`0.2.0` → `0.2.1`) | Fixes, security fixes, documentation and deploy corrections only: nothing a caller has to adapt to. |
| **major** | Refused by `bump.py` while the version is 0.x. Declaring 1.0 is a decision: pass the explicit version `1.0.0`. After 1.0 the usual SemVer rules apply (breaking = major). |

## Release notes: labels

GitHub's generated notes are grouped by label (`.github/release.yml`) and the CHANGELOG is built from the same
labels. Every pull request gets one category label:

| Label | Section | Set from a title starting with | Or from the changed paths (no prefix) |
|---|---|---|---|
| `feature` | Features | `feat`, `feature` | |
| `fix` | Fixes | `fix`, `bugfix`, `hotfix` | |
| `security` | Security | `security`, `sec`; any title naming a CVE or a vulnerability (unless `docs` or `internal`) | |
| `docs` | Docs | `docs`, `doc` | only `docs/`, `*.md`, licence files |
| `infra` | Infra and CI | `ci`, `build`, `deploy`, `infra`, `images` | only `.github/`, `deploy/`, `infrastructure/`, `scripts/ci/`, `scripts/release/` |
| `internal` | left out of public notes | `chore`, `test`, `refactor`, `style`, `perf`, `spike` | only `tests/`, `spikes/` |

The `labeler.yml` workflow applies the label to a pull request that has none, when it is opened or its title is
edited. It runs the labeler from the base branch (never the pull request's code), skips forks, and never changes a
label a person has set: add or change the label by hand whenever the rule guesses wrong. A pull request no rule
classifies (a mixed change without a prefix) stays unlabelled and is listed under **Other changes**. Prefer a
conventional title (`feat(studio): ...`): it reads well in the notes too, because the prefix is turned into a
bold scope. `skip-changelog` keeps a pull request out of GitHub's generated notes.

Pull requests merged before the labeler existed are labelled once with `scripts/release/backfill_labels.py --repo
OWNER/NAME` (a dry run that prints the plan) and again with `--apply`. It uses `backfill-overrides.json`, a reviewed
decision for titles no rule can classify, and is safe to re-run.

## The steps

1. **Pick the level.** Look at what merged since the last tag (the Compare view, or `git log vX.Y.Z..main`).
2. **Run the `Release PR` workflow** (Actions, from `main`, as the repository owner) with a `level` (minor or patch)
   or an explicit `version`. It runs `bump.py`, then `changelog.py build`, and opens the pull request
   **`release: vX.Y.Z`** with the bump and the CHANGELOG section. A pull request opened by a workflow does not
   start CI: close and reopen it once. (The repository setting *Actions → General → Allow GitHub Actions to create
   and approve pull requests* must be on.)
3. **Review the wording** of the CHANGELOG section in that pull request and edit the lines you want changed (edit
   `CHANGELOG.md` on the branch; an entry is one pull request title). Fix a wrong section by changing the pull
   request's label and rebuilding, or by moving the line by hand.
4. **Merge it.** The `Tag release` workflow runs on the push to `main`. It compares the version before and after
   the push; when it changed and `vX.Y.Z` does not exist, it tags the pushed commit and creates the GitHub Release
   with the CHANGELOG section as its body. It works with any merge style, because it looks at the version and not
   at the commit message. A push that does not change the version does nothing.
5. **Public release.** See below.

To build the section on your own machine (needs `gh` signed in): `python3 scripts/release/changelog.py build
--version X.Y.Z` prints it; `--write` updates the files. `--prs FILE` reads a saved `gh pr list --json
number,title,labels,mergedAt,mergeCommit` result instead of the API, which is what the tests do.

### The changelog files

`CHANGELOG.md` follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/): `## [Unreleased]`, then one
`## [X.Y.Z] - date` section per release, newest first, with the sections Features, Fixes, Security, Documentation,
Infrastructure and Other changes. The pull requests labelled `internal` are listed separately in
`CHANGELOG.internal.md`, which only the upstream repository has. The upstream GitHub Release body is both; the
public one is `CHANGELOG.md` only.

## The public mirror

The public repository is produced from upstream by the export (`deploy/export`, operator only). The export
carries the version and `CHANGELOG.md`, and leaves out `CHANGELOG.internal.md` and the upstream release workflows
(`release.yml`, `release-tag.yml`). Before anything is pushed, the export passes the public changelog through its
replacement rules and its residual-string scan (the same scan it runs on the whole history). **A hit blocks the
export**, so the notes of a public release never carry an internal name, host or address: GitHub keeps edit history
for releases, so an internal string must never be published and removed afterwards. Pull request links are
removed from the public notes (they point at private pull requests). A release the public repository skips (it has no
tag there) keeps its section, and the export re-points the compare links so that none needs the missing tag.

If the export is blocked, fix the offending pull request title (rename the PR, then edit its line in
`CHANGELOG.md`) or add a replacement rule, and run the export again.

The public repository has the `Public release` workflow (`release-public.yml`). When a push to its `main` brings a
CHANGELOG version that has no tag, or when someone runs it by hand, it checks that `pyproject.toml` says the same
version, tags the commit and creates the GitHub Release with that CHANGELOG section as its body. It refuses a body
that contains an internal block, an e-mail address or a local path, as a last lock.

So the order is: release upstream (steps 1 to 4) → run the export and open the merge pull request on the public
repository → merge it → the public release follows by itself.

## Tag protection

Protect `v*` tags with a repository ruleset (Settings → Rules → Rulesets → new tag ruleset, target `v*`): block
deletion and updates (nobody moves or removes a published tag). Restrict creation to the workflows by leaving the
ruleset's bypass list to the repository admin; the release workflows create tags through the GitHub Release API with
the workflow token, and never overwrite a tag that exists. Apply the same ruleset in the public repository.

## Rollback

A release is only a tag and a text, so rolling back is about the deployment, not the release:

- **Deployed a bad commit:** deploy the previous good commit from `main` (the deploy workflow takes any commit of
  `main`; `rollout undo` cannot recover a schema change, so a migration needs its own forward fix). The version
  line in the deploy summary tells you what is running.
- **A bad release:** do not delete or move the tag. Mark the GitHub Release as a pre-release (or edit its notes to
  say what is wrong), fix on `main`, and cut a patch release (`0.2.1`) the same way. A tag that was already
  published stays, because someone may have pinned it.
- **A release PR that is wrong, before merging:** close it and delete its branch; nothing was tagged. Re-run the
  workflow. If it was merged and the tag does not exist yet, fix forward with a new release PR.
- **The release workflow failed after the merge:** re-run the `Tag release` workflow run. It is idempotent: it does
  nothing when the tag exists, and otherwise creates it.
