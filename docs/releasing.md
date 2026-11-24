# Releasing

This page is for maintainers. It covers how a release is cut.

## Every release

1. Make sure CI is green on `main`.
2. Set the version in `pyproject.toml` and `web/package.json`, then refresh the lock files:

   ```bash
   uv lock
   (cd web && npm install --package-lock-only)
   uv run python scripts/export_openapi.py
   ```

3. In `CHANGELOG.md`, make the top heading `## [X.Y.Z] - YYYY-MM-DD` with today's date. For 0.1.0 that means replacing `Unreleased` in `## [0.1.0] - Unreleased`. After the release, add `## [Unreleased]` above it for the next changes.
4. Check the tree:

   ```bash
   uv run python scripts/check_release.py --tag vX.Y.Z
   ```

   It fails on any unset placeholder, a version that does not match the tag in either file, or a changelog section that is missing, undated or empty.

5. Merge that change, then tag the merge commit and push the tag:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

## What the release workflow does

`.github/workflows/release.yml` runs on a tag that starts with `v`:

1. Runs `scripts/check_release.py --tag` and stops if it fails.
2. Builds the API image (`Dockerfile`) and the console image (`web/Dockerfile`) for `linux/amd64` and `linux/arm64`, with build provenance and an SBOM attached.
3. Pushes them to `ghcr.io/mihirp381/checkcord` and `ghcr.io/mihirp381/checkcord/web`, tagged `X.Y.Z` and `latest`.
4. Creates a GitHub release named `vX.Y.Z` whose notes are that version's changelog section.

It publishes nothing to PyPI or npm.

If it fails for a reason outside the code, such as a permission, fix that and re-run the failed jobs: the images are overwritten, and the release is created only if it does not exist yet. If the code is at fault, release a new patch version. Never move a tag that people may already have pulled.
