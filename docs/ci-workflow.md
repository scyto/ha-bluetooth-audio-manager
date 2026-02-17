# CI/CD Workflow

## Overview

The project uses a single GitHub Actions workflow (`build.yaml`) that builds multi-arch
Docker images for both stable and dev channels. Images are published to GHCR as
multi-arch manifests (not per-arch packages).

## Triggers

| Event | Scope | What runs |
|---|---|---|
| Push to `dev` | Source changes only | Dev build + dev manifest + version bump PR |
| Push `v*` tag | Always | Stable build + stable manifest |
| PR to `main` | Source changes only | Stable validation build (no push) |

### paths-ignore

Both `push` and `pull_request` triggers skip builds when only non-build files change:

- `**.md` (all markdown)
- `.github/ISSUE_TEMPLATE/**`, `.github/FUNDING.yml`
- `repository.json`, `LICENSE`

The `pull_request` trigger additionally ignores `bluetooth_audio_manager_dev/**` to
prevent the automated dev version bump PRs from triggering wasteful validation builds.

## Build Flow

### Dev builds (push to `dev`)

```
push to dev
  -> build-dev: 4 per-arch images pushed by digest
  -> merge-dev: combines digests into multi-arch manifest
     tagged: sha-XXXXXXX + latest
  -> update-addon-version-dev: creates PR to update
     bluetooth_audio_manager_dev/config.yaml on main
     with the new SHA version (merged immediately)
```

The dev image is published to `ghcr.io/scyto/ha-bluetooth-audio-manager-dev`.

### Stable builds (push `v*` tag)

```
publish GitHub release (creates v* tag)
  -> build: 4 per-arch images pushed by digest
  -> merge: combines digests into multi-arch manifest
     tagged: {version from config.yaml} + latest
```

The stable image is published to `ghcr.io/scyto/ha-bluetooth-audio-manager`.

### PR validation (PR to `main`)

The stable `build` job runs but does not push images (`push=false`). This validates
that the code compiles and the Docker image builds successfully before merging.

## Release Process

1. **Create draft GitHub release** targeting `main` with a `v*` tag (e.g., `v1.1.0`)
2. **Create PR from `dev` to `main`** — include a version bump in
   `bluetooth_audio_manager/config.yaml` to match the release tag
3. **Merge the PR** — CI validates the build but does not push images
   (main is not in the push trigger branches)
4. **Publish the draft release** — the `v*` tag push triggers the stable build,
   creating the immutable release image
5. **Sync dev** — merge main back into dev so it picks up the version bump and
   any main-only changes

### Why stable builds only trigger on tags (not main pushes)

Initially, stable builds triggered on both `main` pushes and `v*` tags. This caused
an artifact immutability problem: merging a PR to main built and published the
release image _before_ the GitHub release was published. The same version tag got
built twice — once on merge, once on release publication — and the first build
meant the image existed before the release was "officially" created.

Removing `main` from the push trigger means only publishing a GitHub release (which
creates the tag) triggers a stable build. The release is the single gate for when
stable images are built and published.

## Dev Version Bump (automatically merged PR)

After each dev build, the `update-addon-version-dev` job updates
`bluetooth_audio_manager_dev/config.yaml` on `main` with the new SHA version.
HAOS reads add-on metadata from the default branch (main), so this file must be
kept current for users to see dev updates in the add-on store.

### Why it uses a PR instead of direct push

The "Protect main" branch ruleset requires all changes go through a pull request.
The `GITHUB_TOKEN` used by GitHub Actions authenticates as `github-actions[bot]`,
which cannot be added as a bypass actor on personal (non-organization) repositories.
The GitHub API returns an error referencing an internal codename ("poptarts
integration") when attempting to add it.

The PR approach works with standard `GITHUB_TOKEN` permissions:

1. Closes any stale dev-version PRs from previous runs
2. Creates a temporary branch (`chore/dev-version-sha-XXXXXXX`)
3. Opens a PR to main
4. Immediately merges with `--squash --delete-branch`

Note: `--auto` is not used because it requires the "Allow auto-merge" repo setting,
which in turn requires required status checks. Since there are no checks to wait for
(the PR only touches `bluetooth_audio_manager_dev/**` which is in `paths-ignore`),
the merge executes immediately.

### Loop prevention

The automatically merged PR does not create infinite build loops because:

1. The PR only changes `bluetooth_audio_manager_dev/config.yaml`, which is in the
   `pull_request` `paths-ignore` list — so no validation build triggers
2. The merge to main does not trigger any build because `main` is not in the
   `push` trigger branches
3. The `update-addon-version-dev` job only runs when `github.ref == 'refs/heads/dev'`,
   so nothing in the PR flow can re-trigger it

## Branch Protection

Two GitHub rulesets protect the repository:

### Protect main (branch ruleset)

- **Restrict deletions** — prevents accidental branch deletion
- **Block force pushes** — preserves commit history
- **Require pull request** (0 approvals) — ensures audit trail for all changes;
  solo maintainer can self-merge

### Protect releases (tag ruleset, pattern `v*`)

- **Restrict deletions** — once a release tag exists, it cannot be removed
- **Restrict updates** — prevents re-pointing a tag to a different commit
- **Block force pushes** — tags are immutable once created

Together these ensure artifact immutability: a released version's tag cannot be
moved or deleted, and its image cannot be silently overwritten by a new build.
