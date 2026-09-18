---
name: release
description: >-
  Write wayland-vnc release docs and version pins for the version on the current
  branch by reviewing every change (committed, staged, and unstaged vs the merge
  base). Use when the user asks for a release, release notes, changelog, GitHub
  release description, or to document the current version from the branch name.
---

# Release Docs

Produce release documentation for the **current branch version**, covering **all**
changes for that release. Do **not** tag, push, or `gh release create` unless the user
explicitly asks.

## Agent mandate

1. **Version from the current branch only** — parse `git branch --show-current`. Write
   that semver into the repo-root **`VERSION`** (single source of truth, no `v`
   prefix), then run `scripts/sync-version.py` so `pyproject.toml` `[project] version`
   matches. Do not invent a version from tags or prior docs.
2. **Review ALL changes** — every file and theme in this release: product (CLI, runtime,
   serving path, installer, native patches), fixtures and the qualification harness,
   CI / Docker / scripts / workflows, packaging (deb/rpm/arch/appimage/flatpak/snap),
   the PPA workflow, tests, docs, and the agent skills.
3. Add a **new top** `debian/changelog` entry `wayland-vnc (${version}) resolute`
   summarizing the release; keep older entries.
4. Reset `PPA_UPLOAD_REVISION` to `"1"` in `.github/workflows/release.yml` when
   `VERSION` itself bumps (keep a higher revision only when re-uploading the same
   `VERSION`).
5. Write both release files under `docs/releases/` and update the `AGENTS.md`
   current-version line and skill tree in the same change set.

## Version from branch

```bash
branch="$(git branch --show-current)"
version="$(printf '%s' "$branch" | sed -E 's#.*/##; s/^v//')"
# VER=1.0.0 (VERSION + pyproject); TAG=v1.0.0 (docs, GitHub tag, install URLs)
```

Abort if `$version` is not `N.N.N`. After resolving it: write `VERSION`, run
`scripts/sync-version.py`, point human install URLs at tag `v$version`, reset
`PPA_UPLOAD_REVISION` to `1` on a new `VERSION`, and add the top `debian/changelog`
entry.

## Gather all changes

```bash
base="$(git merge-base HEAD main 2>/dev/null || git merge-base HEAD master)"
git log --oneline "$base"..HEAD
git diff --name-status "$base"...HEAD
git diff "$base"...HEAD                 # committed changes, in full
git status --porcelain=v1
git diff HEAD                           # unstaged tracked changes, in full
git diff --cached                       # staged changes, in full
git ls-files --others --exclude-standard # every untracked file
```

Read the untracked files too -- on a branch built from scratch they can be most of
the release. Print each one and review its contents:

```bash
git ls-files --others --exclude-standard -z \
  | xargs -0 -I{} sh -c 'printf "\n===== %s =====\n" "{}"; cat -- "{}"'
```

`--stat` alone is not a review: it names files without showing what changed in them.

Group by theme; never paste raw file lists as the narrative. When the release touches
targets, name them: GNOME, KDE Plasma, Xfce+labwc, LXQt+labwc, Sway, Hyprland, Wayfire.
When packaging changes, name the formats: deb, rpm, Arch, AppImage, Flatpak, Snap, and
the PPA.

## Output files

| File | Purpose |
| --- | --- |
| `docs/releases/vX.Y.Z.md` | Full release page (install + changelog) |
| `docs/releases/vX.Y.Z_github_description.md` | GitHub Release body (H1 title) |

GitHub H1:

```markdown
# wayland-vnc vX.Y.Z - <Short Theme Title>
```

End the GitHub description with:

```markdown
**Full Changelog**: [vPREV...vX.Y.Z](https://github.com/ventura8/wayland-vnc/compare/vPREV...vX.Y.Z)
```

## Evidence honesty

A release page must not claim a desktop is supported unless its full qualification
suite passed on a trusted runner and the record is published to the evidence store for
the tagged commit (`docs/evidence-store.md`).
Document partial evidence as partial. Missing app provisioning or hardware
(suspend/resume VM, Android device) is a documented blocker, not a silent omission.

## Tag → publish (only when the user asks)

Per release, once merged to the default branch: confirm `VERSION` == tag == pyproject;
with the tree frozen, run every target/viewer pair (`scripts/qualify-all.sh` and
`scripts/qualify-all.sh --android`, plus the VM and hardware pairs) against that exact
SHA and publish with `scripts/publish-evidence.py --push` -- the gate matches records
to `github.sha`, so evidence from an earlier commit leaves the release a prerelease and
the PPA changelog "Not release-qualified"; then
push tag `vX.Y.Z`, and let `.github/workflows/release.yml` build the source package,
`dput` to `ppa:ventura8/wayland-vnc`, build every binary format, and create the GitHub
Release with a `SHA256SUMS` manifest. Do not `--force-push` `main`/`master` or amend a
pushed tip without an explicit confirmed `--force-with-lease`.

## Checklist

```text
Release progress:
- [ ] Version parsed from current branch only
- [ ] VERSION written + scripts/sync-version.py (pyproject synced)
- [ ] PPA_UPLOAD_REVISION reset to 1 (new VERSION) or left as a re-upload bump
- [ ] debian/changelog top entry added
- [ ] Human install URLs / docs TAG updated
- [ ] ALL diffs vs merge-base + working tree reviewed
- [ ] docs/releases/vX.Y.Z.md + _github_description.md written
- [ ] Release docs list every GitHub artifact (.deb, .rpm, Arch, AppImage, Flatpak, Snap, SHA256SUMS)
- [ ] Compatibility claims match the evidence store's records for the tagged SHA (no unqualified "supported")
- [ ] Evidence published for the exact SHA being tagged (desktop and Android viewers)
- [ ] AGENTS.md current-version line + skill tree updated
```
