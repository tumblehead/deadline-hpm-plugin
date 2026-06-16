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
submitter runs from `~/.hpm/packages/tumblepipe@1.12.6/…`, so that submitter-
local path is what the worker is told to run. The moment a worker doesn't have
that exact version laid down at that exact path, the task dies with:

```
Script file not found: …/.hpm/packages/tumblepipe@1.12.6/…/render.py
```

It also has to reproduce the package's third-party Python dependencies
(`pymongo`, `dictdiffer`, …) — in-Houdini those come from HPM, but a hand-built
venv has to reconstruct them.

This plugin solves both by letting **hpm itself own the environment**: the job
bundles an `hpm.toml` whose dependency is the task package and whose
`[scripts.task]` runs the task module with `package-env = true`. The worker
installs that manifest and runs the task inside the resolved package-env — no
absolute paths, no dependency reconstruction.

> **Requires hpm ≥ v0.22.1** (the `package-env` script mode).

## What it does, per task

Given a bundled `Manifest` (`hpm.toml`) like:

```toml
[package]
path = "local/deadline-hpm-job"
name = "deadline-hpm-job"
version = "0.0.0"

[compat]
houdini = ">=21, <99"

[[registries]]
name = "tumbletrove"
url = "https://api.tumbletrove.com/v1/registry"
type = "api"

[dependencies]
"tumblehead/tumblepipe" = "1.12.6"

[scripts.task]
cmd = "python -m tumblepipe.farm.tasks.render.render"
package-env = true
```

1. **Bootstrap** a managed `hpm` binary under `~/.deadline/hpm` if needed.
2. **Export** the job environment so the task can run standalone: `TH_FARM_DATA`
   (the shared job data dir) plus every `KEY=VALUE` from the job's
   `EnvironmentFile` (`OCIO`, `TH_CONFIG_PATH`, `TH_PROJECT_PATH`, …) — the task's
   `default_client()` reads these at import.
3. **Install** the manifest into the worker's HPM store from a **worker-local**
   copy: `hpm -C <job_dir> install`. This resolves the package + its
   `[python_dependencies]` and materializes the package-env venv (interpreter
   pinned to the package's Houdini-mapped CPython). Worker-local because hpm
   writes `hpm.lock` next to the manifest, and a render job's chunks install
   concurrently on different workers and must not race on the shared copy.
4. **Run** the task: `hpm -C <job_dir> run task -- <context.json> <first> <last>`.
   The script runs **in native Windows python** inside the package-env (package
   importable, its deps on `PYTHONPATH`). The context path is passed absolute
   because `hpm run` executes the script in the manifest dir, not the data dir.

The task itself drives Houdini's bundled tools directly — `husk.exe`, the Windows
USD resolver, and `hoiiotool`/`hffmpeg` for image/video — entirely in native
Windows python, no WSL. None of that is the plugin's concern.

## Plugin Info options

| Parameter | Required | Description |
|---|---|---|
| `Manifest` | yes | Bundled `hpm.toml` with the task dependency + `[scripts.task]` |
| `StartupDirectory` | yes | Shared job data dir (exported as `TH_FARM_DATA`; context resolved against it) |
| `Arguments` | no | Task arguments — the (data-dir-relative) context path; frames are appended |
| `EnvironmentFile` | no | `.env` whose `KEY=VALUE`s are set in the task process env |
| `HpmVersion` | no | hpm CLI to self-bootstrap: pinned `vX.Y.Z` ≥ `v0.22.1` (default env `HPM_VERSION`, else studio-pinned `v0.22.1`) |
| `HpmExecutable` | no | Override: explicit hpm path instead of the self-bootstrapped one |
| `HpmManagedDirectory` | no | Where the bootstrapped hpm lives (default `~/.deadline/hpm`) |
| `SingleFramesOnly` | no | One frame per task (default `true`) |
| `Package` / `ScriptFile` | no | Informational only — the runnable set + task module come from the `Manifest` |

## Worker requirements

- The **`hpm` CLI is self-bootstrapped** — the plugin downloads/updates it under
  `~/.deadline/hpm` on the first job, so render nodes need no TumbleTrove
  Desktop install.
- **No per-node registry setup.** The bundled job manifest declares its own
  `[[registries]]`, so a render node that was never `hpm registry add`-ed still
  resolves packages. The registry and its archives are public — no credentials.
- **Houdini** (matching the project's pinned version) on each worker — the task
  drives its bundled `husk`/`hoiiotool`/`hffmpeg`/`iconvert` directly. No WSL and
  no `uv` are required.

A fully **pre-warmed** worker (target hpm already in `~/.deadline/hpm`, all
package versions already in `~/.hpm/packages`) keeps the network footprint
minimal.

## hpm self-bootstrap

On first use the plugin ensures a managed hpm binary exists, mirroring
`.ci/install_hpm.sh`:

- Source: GitHub releases of [`3db-dk/hpm`](https://github.com/3db-dk/hpm),
  per-platform asset `hpm-<tag>-<suffix>` (`windows-x86_64.exe`, `linux-x86_64`,
  `darwin-universal`).
- Target version: `HpmVersion` plugin info → `HPM_VERSION` env → the
  studio-pinned default (`v0.22.1`). This default tracks the central
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

- The bundled `Manifest` is **required**; there is no synthesized fallback (the
  task module to run lives in the manifest's `[scripts.task]`).
- Submitting from a **dev/editable** install (a checkout, not a published
  `name@version`) has no identity to resolve; the submitter side rejects that
  with a clear error rather than baking a non-reproducible path.
