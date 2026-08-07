# Release automation: rc to TestPyPI, stable to PyPI

`a-ledger` is published to PyPI by a GitHub Actions release workflow that builds
an sdist and wheel, verifies the git tag matches the version in `pyproject.toml`,
and uploads through PyPI trusted publishing (OIDC, no stored tokens). A tag whose
name contains `rc` (e.g. `v0.1.0rc1`) publishes to **TestPyPI**; a stable tag
(e.g. `v0.1.0`) publishes to **PyPI**. This mirrors the proven flow used by the
`qmt-rpyc` project.

**Considered Options**:

- Publishing the *same* version to both TestPyPI and PyPI in a single run was
  rejected: it removes the verification gate of testing the release candidate
  before cutting the stable tag. With the rc/stable split, test and prod
  intentionally carry different versions.
- API tokens stored as GitHub secrets were rejected in favor of trusted
  publishing: no long-lived secret to rotate, and the publisher identity is
  bound to the workflow itself.
