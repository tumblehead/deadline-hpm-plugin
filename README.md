# HPM Deadline Plugin

A Deadline plugin that runs a farm task **from an
[HPM](https://github.com/3db-dk/hpm) package resolved on the worker**, so a
render node reproduces the exact package versions and Python environment the
job was submitted with, instead of assuming every machine mirrors the
submitter's `~/.hpm` store on disk.

The plugin is self-contained: it needs a Deadline repository, workers that can
reach the hpm release downloads and your package registry, and a submitter that
writes an `hpm.toml` job manifest. It has no dependency on any particular
studio's CI, secrets store or pipeline.

> **Requires hpm >= v0.22.2** (the `package-env` script mode). The plugin
> downloads hpm itself; see [hpm self-bootstrap](#hpm-self-bootstrap).

## How it works

The job bundles an `hpm.toml` whose dependency is the package that holds the
task, and whose `[scripts.task]` runs the task module with `package-env = true`:

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
"yourstudio/farm-tasks" = "1.4.0"

[scripts.task]
cmd = "python -m farm_tasks.render"
package-env = true
```

The manifest declares its own `[[registries]]`, so workers need no
`hpm registry add`. Point it at whichever registry hosts your packages.

For each task the plugin:

1. **Bootstraps** a managed `hpm` binary under `~/.deadline/hpm` if needed.
2. **Exports** the job environment: `TH_FARM_DATA` (the shared job data dir)
   plus every `KEY=VALUE` from the job's `EnvironmentFile`.
3. **Installs** the manifest from a **worker-local** copy:
   `hpm -C <job_dir> install`. This resolves the package and its
   `[python_dependencies]` and builds the package-env venv (interpreter pinned
   to the package's Houdini-mapped CPython). The copy is worker-local because
   hpm writes `hpm.lock` next to the manifest, and the chunks of one job
   install concurrently on different workers.
4. **Runs** the task: `hpm -C <job_dir> run task -- <args...> <first> <last>`,
   inside the package-env (package importable, its dependencies on
   `PYTHONPATH`).

## The task contract

The module named in `[scripts.task]` is your code. The plugin calls it like
this:

- **Arguments:** the job's `Arguments` split on whitespace, then the task's
  first and last frame. If the first argument is a relative path, it is made
  absolute against `StartupDirectory`, because the task's working directory is
  the worker-local manifest dir, not the data dir.
- **Environment:** `TH_FARM_DATA` is the shared job data dir
  (`StartupDirectory`), and every entry of `EnvironmentFile` is set.
- **Progress:** a stdout line matching `Progress: NN` (an optional `%` is
  allowed) sets the task's progress.
- **Exit code:** non-zero fails the task.

What the task does (drive `husk`, `hoiiotool`, a simulation, anything) is not
the plugin's concern.

## Submitting a job

Any submitter that can write two Deadline info files works. A minimal
submission with `deadlinecommand`:

```ini
# job_info.job
Plugin=HPM
Name=shot010 render
Frames=1001-1100
ChunkSize=1
Pool=yourpool
```

```ini
# plugin_info.job
Manifest=//server/jobs/shot010/hpm.toml
StartupDirectory=//server/jobs/shot010
Arguments=context.json
EnvironmentFile=//server/jobs/shot010/job.env
```

```sh
deadlinecommand job_info.job plugin_info.job
```

Write the manifest, the data dir contents and the env file to storage every
worker can read before submitting.

## Plugin Info options

| Parameter | Required | Description |
|---|---|---|
| `Manifest` | yes | Bundled `hpm.toml` with the task dependency + `[scripts.task]` |
| `StartupDirectory` | yes | Shared job data dir (exported as `TH_FARM_DATA`; a relative first argument is resolved against it) |
| `Arguments` | no | Task arguments; the frame range is appended |
| `EnvironmentFile` | no | `.env` whose `KEY=VALUE`s are set in the task process env. Read as UTF-8, else the worker locale's encoding |
| `HpmVersion` | no | hpm to self-bootstrap: pinned `vX.Y.Z` >= `v0.22.2`, or `latest`. Defaults to the worker's `HPM_VERSION` env var, else the version this plugin release pins (`v0.36.1`) |
| `HpmExecutable` | no | Explicit hpm path instead of the self-bootstrapped one (air-gapped workers) |
| `HpmManagedDirectory` | no | Where the bootstrapped hpm lives (default `~/.deadline/hpm`) |
| `SingleFramesOnly` | no | One frame per task (`false` when omitted from the plugin info; the Monitor form defaults it to `true`) |
| `Package` / `ScriptFile` | no | Informational only; the task to run comes from the `Manifest` |

## Worker requirements

- **Houdini**, in the version the job's packages need, if the task uses it.
  No WSL and no `uv` are required.
- **Network access** to:
  - `github.com` (hpm release downloads), unless `HpmExecutable` is set or the
    target hpm is already bootstrapped;
  - the registries the job manifest declares, and wherever their archives are
    hosted;
  - `pypi.org` and `files.pythonhosted.org`, if your packages declare
    `[python_dependencies]`.

  When `hpm install` fails, the plugin logs which of these endpoints the worker
  can and cannot reach.
- **Paths:** the paths in the plugin info are passed through the repository's
  **Path Mapping** rules first, so a mixed Windows/Linux/macOS farm maps shares
  the usual Deadline way. On top of that, the plugin converts between WSL drive
  mounts (`/mnt/c/...`) and drive letters (`C:/...`) for the worker's OS. Other
  paths are left as they are.

A **pre-warmed** worker (target hpm already in `~/.deadline/hpm`, all package
versions already in `~/.hpm/packages`) keeps the network footprint minimal.

## hpm self-bootstrap

On first use the plugin makes sure a managed hpm binary exists:

- Source: the public GitHub releases of
  [`3db-dk/hpm`](https://github.com/3db-dk/hpm), per-platform asset
  `hpm-<tag>-<suffix>` (`windows-x86_64.exe`, `linux-x86_64`,
  `darwin-universal`). No credentials are needed.
- Target version: `HpmVersion` plugin info, then the `HPM_VERSION` env var on
  the worker, then the version this plugin release pins. To run one hpm across
  your whole farm, set `HPM_VERSION` on the workers. **Avoid `latest`**: an
  unvetted hpm release can change behavior under the farm.
- A pinned `vX.Y.Z` is downloaded directly and never calls the GitHub API. If
  the target is `latest`, the newest tag is resolved through the GitHub API and
  cached for 6 hours in `~/.deadline/hpm/state.json` to stay under its rate
  limit.
- If the download fails but a binary is already installed, the plugin logs a
  warning and uses it rather than failing the render.
- Air-gapped farms: install hpm yourself and set `HpmExecutable`.

## Deployment

Copy this directory to the Deadline repository's custom plugins folder:

```
<DeadlineRepository>/custom/plugins/HPM/
```

then restart the workers.

## Development

The plugin's path, argument, env-file and manifest helpers have
property-based tests (pytest + Hypothesis) that run without Deadline:

```sh
python -m pip install -r requirements-dev.txt
python -m pytest tests
```

They run on GitHub Actions for Linux and Windows on Python 3.10 (no
`tomllib`, as on older Deadline releases) and 3.12.

## Notes and limitations

- The bundled `Manifest` is **required**; there is no synthesized fallback (the
  task to run lives in its `[scripts.task]`).
- Arguments are split on whitespace, so a context path cannot itself contain
  spaces (relative to `StartupDirectory` it rarely needs to; the data dir
  itself may contain spaces).
- Submit published package versions only. A dev/editable install (a checkout,
  not a published `name@version`) has no identity a worker can resolve, so
  submitters should refuse it rather than bake in a path that only exists on
  the submitting machine.

## Background

This plugin replaces an earlier design that baked an absolute `ScriptFile` into
the job and rebuilt the task's Python dependencies in a hand-made venv. That
broke as soon as a worker lacked the exact package version at the exact path
the submitter had. Letting hpm resolve and own the environment on the worker
removes both problems.
