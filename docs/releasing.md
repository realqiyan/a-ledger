# Releasing

`a-ledger` is published by GitHub Actions on tag push (see
[ADR 0001](adr/0001-pypi-release-automation.md)). A tag containing `rc`
(e.g. `v0.1.0rc1`) publishes to **TestPyPI**; a stable tag (e.g. `v0.1.0`)
publishes to **PyPI**. No local upload tooling is required — publishing runs
entirely in CI via trusted publishing.

## One-time PyPI setup

The project name is claimed on first publish, so the trusted-publisher
configuration must exist on both indexes before the first release tag is pushed.

### TestPyPI

1. Log in at <https://test.pypi.org/> (register and verify email if needed).
2. Open **Account → Publishing** (`https://test.pypi.org/manage/account/publishing/`).
3. Add a **pending publisher** (GitHub):
   - GitHub repository: `realqiyan/a-ledger`
   - Workflow file name: `release.yml`
   - Environment name: `testpypi`
   - PyPI project name: `a-ledger`
4. Save.

### PyPI

Repeat the same steps at `https://pypi.org/manage/account/publishing/` with
environment name `pypi`.

Notes:

- A pending publisher does not reserve the name until the first successful
  publish. If someone else registers `a-ledger` first, the pending publisher is
  invalidated.
- The account needs a verified email; enable 2FA as required by the index.

## Cutting a release

1. Set `version` in `pyproject.toml` to the exact release version.
2. Commit and push to `main`.
3. Tag and push. CI builds, verifies the tag matches the version, creates a
   GitHub Release, and publishes:

   ```bash
   # release candidate -> TestPyPI
   git tag v0.1.0rc1
   git push origin v0.1.0rc1

   # stable -> PyPI
   git tag v0.1.0
   git push origin v0.1.0
   ```

4. Verify installation:

   ```bash
   python -m pip install --index-url https://test.pypi.org/simple/ a-ledger==0.1.0rc1
   python -m pip install a-ledger
   ```
