# Releasing lib-odc-core

Manual release process, mirroring `lib-core`/`automation_core`. There is no
CI/CD - every step below is done by hand.

## Steps

1. Bump the version in **two places** (they must match):
   - `pyproject.toml` - `[project] version`
   - `src/odc_core/__init__.py` - `__version__`
2. Add a `CHANGELOG.md` entry: `## [X.Y.Z] - YYYY-MM-DD` with `### Added` /
   `### Changed` / `### Fixed` / `### Removed` subsections as applicable.
3. Add a line to `CLAUDE.md`'s `## Change Log` describing the release.
4. Update `README.md` and `lib-odc-core-spec.md` if the public API or expected
   config shape changed. The spec is the source of truth - keep it current.
5. Run the test suite - it must pass:
   ```
   pip install -e ".[dev]"
   pytest
   ```
6. Confirm `git status` is clean, then tag and push:
   ```
   git tag vX.Y.Z
   git push --tags
   ```
7. Build the wheel locally:
   ```
   py -m pip install build
   py -m build --wheel --outdir dist
   ```
   This produces `dist\odc_core-X.Y.Z-py3-none-any.whl`.
8. Draft a GitHub Release on `InspiredAutomation/lib-odc-core` at tag
   `vX.Y.Z`, title `vX.Y.Z`, description = the matching CHANGELOG block,
   **attach the built wheel as a release asset** (required - installing by
   the release wheel URL 404s otherwise), mark "latest", publish.
9. Post-release verification:
   - Release page shows the wheel asset.
   - `pip install git+https://github.com/InspiredAutomation/lib-odc-core.git@vX.Y.Z`
     succeeds and `python -c "import odc_core; print(odc_core.__version__)"`
     prints the new version.
   - Installing the wheel URL directly also succeeds.
10. Pin the new release in each supplier project's `requirements.txt`:
    ```
    odc-core @ https://github.com/InspiredAutomation/lib-odc-core/releases/download/vX.Y.Z/odc_core-X.Y.Z-py3-none-any.whl
    ```

## Versioning

Semantic versioning:
- **PATCH** - bug fix, no API change.
- **MINOR** - backward-compatible feature addition.
- **MAJOR** - breaking change to a function's signature, return shape, or
  required config keys.

## Hard rules

- Never edit, move, or force-push a tag.
- Never delete a release - deprecate it in `CHANGELOG.md` instead.
- Never skip the `CHANGELOG.md` entry, even for a docs-only release.
