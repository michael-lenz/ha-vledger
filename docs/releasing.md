# Releasing

*Operating manual — what the person cutting a release does, in order. Why
the release looks like this is ADR-0002 in the project's register: one
repository, two packages, one version number; a tag publishes the library
to PyPI and is the HACS release.*

## Once: let GitHub publish to PyPI for you

PyPI can trust a GitHub Actions workflow to publish a project, with no
token to mint, store or rotate ("trusted publishing"). It is set up once,
before the first release, and the project name is reserved by that act.

1. Create an account on <https://pypi.org> and turn on two-factor
   authentication (PyPI requires it for publishing).
2. *Your projects → Publishing* (<https://pypi.org/manage/account/publishing/>)
   → *Add a new pending publisher*, GitHub:
   - PyPI project name: `vledger`
   - Owner: `michael-lenz`
   - Repository name: `ha-vledger`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. In the GitHub repository, *Settings → Environments → New environment*,
   named `pypi`. Nothing else is required; adding yourself as a required
   reviewer there makes every publish wait for your click, which is a
   reasonable habit.

That is all the credentials there are: PyPI trusts the workflow
`.github/workflows/release.yml` running in the `pypi` environment of this
repository, and nothing else.

## Every release

1. The version is one number in three places, and a test keeps them in
   step: `pyproject.toml`, `src/vledger/__init__.py`,
   `custom_components/vledger/manifest.json` (both `version` and the
   pinned requirement). Bump all three in one commit:

   ```bash
   sed -i 's/0\.1\.0/0.2.0/' pyproject.toml src/vledger/__init__.py custom_components/vledger/manifest.json
   python -m pytest -q && ruff check src tests custom_components
   git commit -am "Release 0.2.0"
   git push
   ```

2. Tag it and push the tag. The tag is the release:

   ```bash
   git tag -a v0.2.0 -m "vledger 0.2.0"
   git push origin v0.2.0
   ```

3. The workflow builds the library, publishes it to PyPI, and creates the
   GitHub release with the built files attached. Watch it under
   *Actions*; approve it under *Environments* if you set a reviewer.

4. Check <https://pypi.org/project/vledger/> shows the version. From that
   moment `manifest.json`'s `vledger==0.2.0` resolves, and a Home
   Assistant instance can load the integration.

## By hand, without the workflow

The same thing from a shell, if the workflow is unavailable. It needs an
API token instead of the trust: *Account settings → API tokens → Add API
token*, scoped to the project `vledger` once it exists (the first upload
needs an account-wide token, which you then delete).

```bash
pip install build twine
rm -rf dist
python -m build                 # dist/vledger-X.Y.Z.tar.gz and .whl
twine check dist/*
twine upload dist/*             # username: __token__, password: the token
```

`twine check` is what refuses a broken README or metadata before PyPI
does. The build contains the library and its tests only; the integration
is never on PyPI — HACS ships it from the repository.

## What a release must not do

- Never re-upload a version. PyPI refuses it, and for good reason: a
  version is immutable. A mistake is the next version.
- Never tag before the three version strings agree; the workflow runs the
  tests first and will refuse, but the tag is already pushed then.
- Never publish a version whose L0 schema (`l0.VERSION`) changed without
  the reader for the old one still in place (QUA-03).
