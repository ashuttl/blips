# Releasing blips

Releases are built and published to PyPI by `.github/workflows/publish.yml`
when a `v*` tag is pushed. The build and publish jobs are separate, and PyPI
authentication uses GitHub's short-lived OIDC identity rather than a stored API
token.

## One-time setup

1. Make the GitHub repository public.
2. In the repository settings, create an environment named `pypi`. Require
   manual approval for deployments if the account supports it.
3. On PyPI's **Publishing** account page, add a pending GitHub publisher with:
   - PyPI project name: `blips`
   - GitHub owner: `ashuttl`
   - Repository: `blips`
   - Workflow: `publish.yml`
   - Environment: `pypi`

The pending publisher creates the PyPI project on the first successful
workflow run. It does **not** reserve the name beforehand, so configure it only
when the first tag is ready to push. No PyPI token belongs in GitHub secrets.

## Release checklist

1. Start from a clean `main` branch and pull the latest remote changes.
2. Set `project.version` in `pyproject.toml` and refresh `uv.lock` with
   `uv lock`.
3. Run `uv run --with pytest pytest tests/ -q`.
4. Build locally with `uv build --clear` and inspect the result with
   `uvx twine check dist/*`.
5. Commit the version and release notes, then tag that exact commit. The tag
   must match the package version: version `0.1.0` uses tag `v0.1.0`.
6. Push `main` and the tag:

   ```sh
   git push origin main
   git push origin v0.1.0
   ```

7. Approve the `pypi` environment deployment, if required, and confirm the
   publish workflow succeeds.
8. In a clean temporary environment, verify the public artifact:

   ```sh
   uvx --refresh blips --version
   uvx --refresh blips --location jfk --print --no-weather
   ```

9. Create GitHub release notes for the tag and only then announce the release.

PyPI distributions are immutable. If a release is wrong, increment the version
and publish a new one rather than trying to replace its files.
