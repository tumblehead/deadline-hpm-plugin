# HPM Deadline Plugin

A Deadline plugin that runs a farm task **from an HPM package resolved on the
worker**, so a render node reproduces the exact source and Python environment
the job was submitted with — instead of assuming every machine mirrors the
submitter's `~/.hpm` store on disk.

It is the HPM-aware successor to the
[UV plugin](https://github.com/tumblehead/deadline-uv-plugin). The two can run
side by side during migration (jobs pick `Plugin=UV` or `Plugin=HPM`).

## Why this exists

The UV plugin bakes an **absolute** `ScriptFile` at submit time. Under HPM the
submitter runs from `~/.hpm/packages/tumblepipe@1.11.0/…`, so that submitter-
local path is what the worker is told to run. The moment a worker doesn't have
that exact version laid down at that exact path, the task dies with:

```
Script file not found: /mnt/c/Users/<user>/.hpm/packages/tumblepipe@1.11.0/…/render.py
```

It also never reconstructs the package's third-party Python dependencies
(`pymongo`, `dictdiffer`, …) — in-Houdini those come from HPM; on the farm the
UV venv only had `python-dotenv`.

This plugin fixes both from one source of truth: the **resolved package's own
`hpm.toml`**.

## What it does, per task

Given `Package=tumblepipe@1.11.0` and a package-relative
`ScriptFile=python/tumblepipe/farm/tasks/render/render.py`:

1. **Resolve** the package set against the worker's local HPM store
   (`~/.hpm/packages/<name>@<version>`). Present → cache hit, no network. Missing
   → `hpm install` the manifest (`Manifest`) the submitter bundled in the shared
   job dir. hpm writes `hpm.lock`/`.hpm/` next to the manifest, so the plugin
   copies it to a worker-local temp dir first — a render job's chunks install
   concurrently on different workers and must not race on the shared copy. If no
   `Manifest` is provided (legacy jobs), the plugin synthesizes one for the
   missing specs.
2. **Create** a fresh uv venv (`/tmp/uv-venvs/<hex>`), Python from `PythonVersion`.
3. **Install** `python-dotenv` (for `Runner.py`) plus the resolved packages'
   declared `[python_dependencies]` (and any legacy `RequirementsFile`).
4. **Run** the task script from the resolved package, with each package's
   `python/` dir on `PYTHONPATH` (passed explicitly to `Runner.py`) so runtime
   `import tumblepipe` binds to the submitted version deterministically.
5. **Clean up** the venv.

## Plugin Info options

| Parameter | Required | Description |
|---|---|---|
| `Package` | yes | Package identity, `name@version` (e.g. `tumblepipe@1.11.0`) |
| `ScriptFile` | yes | **Package-relative** path to the task script |
| `ExtraPackages` | no | More `name@version` specs (comma/space sep) added to PYTHONPATH + dep install |
| `HpmVersion` | no | hpm CLI to self-bootstrap: pinned `vX.Y.Z` (default env `HPM_VERSION`, else studio-pinned `v0.21.0`) |
| `HpmExecutable` | no | Override: explicit hpm path instead of the self-bootstrapped one |
| `HpmManagedDirectory` | no | Where the bootstrapped hpm lives (default `~/.deadline/hpm`) |
| `HpmPackagesDirectory` | no | Override the local store (default `~/.hpm/packages`) |
| `PythonVersion` | no | Venv Python version (default `3.11`) |
| `EnvironmentFile` | no | `.env` merged into the task environment |
| `RequirementsFile` | no | Deprecated extra `requirements.txt` |
| `Arguments` | no | Script arguments |
| `StartupDirectory` | yes | Working directory for the task |
| `CacheDirectory` | no | uv cache dir (default `/tmp/uv-cache`) |
| `SingleFramesOnly` | no | One frame per task (default `true`) |

## Worker requirements

- **WSL** with `uv` on PATH (same as the UV plugin), used for the venv + render.
- The **`hpm` CLI is self-bootstrapped** — the plugin downloads/updates it under
  `~/.deadline/hpm` on the first package cache miss, so render nodes need no
  TumbleTrove Desktop install.
- **No per-node registry setup.** The bundled job manifest declares its own
  `[[registries]]`, so a render node that was never `hpm registry add`-ed still
  resolves packages. The registry and its archives are public — no credentials.
- The HPM store is read natively by the plugin and through `/mnt/<drive>` by the
  WSL render, so a single Windows-side store serves both.

A fully **pre-warmed** worker (target hpm already in `~/.deadline/hpm`, all
package versions already in `~/.hpm/packages`) runs the render path with **zero
network**.

## hpm self-bootstrap

On a package cache miss the plugin ensures a managed hpm binary exists, mirroring
`.ci/install_hpm.sh`:

- Source: GitHub releases of [`3db-dk/hpm`](https://github.com/3db-dk/hpm),
  per-platform asset `hpm-<tag>-<suffix>` (`windows-x86_64.exe`, `linux-x86_64`,
  `darwin-universal`).
- Target version: `HpmVersion` plugin info → `HPM_VERSION` env → the
  studio-pinned default (`v0.21.0`). This default tracks the central
  `HPM_VERSION` knob (Infisical, prod) so the worker runs the SAME hpm as CI and
  the submitter. **Do not default to `latest`** — an unvetted release can change
  behavior under the farm; keep the default in lockstep with the Infisical knob.
- A pinned `vX.Y.Z` is downloaded directly and never calls the GitHub API. If
  `HpmVersion`/`HPM_VERSION` is explicitly set to `latest`, the newest tag is
  resolved via the GitHub API and **TTL-cached (6h)** in
  `~/.deadline/hpm/state.json` to bound rate-limit usage.
- If the download is unreachable but a binary is already installed, the plugin
  logs a warning and uses it rather than failing the render.

## Deployment

Copy this directory to the Deadline repository's custom plugins folder:

```
<DeadlineRepository>/custom/plugins/HPM/
```

then restart the workers.

## Notes / current limitations

- `[python_dependencies]` parsing handles the flat `name = "spec"` form. Table-
  form entries (`name = { version = "…" }`) are skipped with a warning.
- Submitting from a **dev/editable** install (a checkout, not a published
  `name@version`) has no identity to resolve; the submitter side rejects that
  with a clear error rather than baking a non-reproducible path.
