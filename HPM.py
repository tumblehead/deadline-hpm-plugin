#!/usr/bin/env python3
from __future__ import absolute_import

from Deadline.Plugins import *
from Deadline.Scripting import *

import urllib.request
import urllib.error
import tempfile
import platform
import shutil
import json
import time
import re
import os

# hpm is distributed as GitHub releases by the same mechanism as the CI
# install_hpm.sh: per-platform assets named hpm-<tag>-<suffix>. HPM_VERSION is
# the studio's central knob (Infisical, env=prod) — "latest" or a pinned vX.Y.Z
# — deliberately not committed anywhere so it can roll globally.
HPM_RELEASES_REPO = '3db-dk/hpm'
# How long a resolved "latest" tag is trusted before we re-ask the GitHub API,
# so a worker fleet behind one NAT doesn't burn the unauthenticated rate limit.
HPM_LATEST_TTL_SECONDS = 6 * 60 * 60
# Studio-pinned hpm fallback. package-env (the [scripts.task] mechanism this
# plugin runs the task through) needs hpm >= 0.22.1. Keep in lockstep with the
# Infisical HPM_VERSION knob.
DEFAULT_HPM_VERSION = 'v0.22.1'

def GetDeadlinePlugin():
    return HPMPlugin()

def CleanupDeadlinePlugin(deadlinePlugin):
    deadlinePlugin.Cleanup()

def _to_windows_path(path):
    raw_path = path.replace('\\', '/')
    if not raw_path.startswith('/mnt/'): return raw_path
    parts = raw_path.split('/')
    drive = f'{parts[2].upper()}:'
    return '/'.join([drive] + parts[3:])

def _to_wsl_path(path):
    raw_path = path.replace('\\', '/')
    if raw_path.startswith('/mnt/'): return raw_path
    parts = raw_path.split('/')
    drive = parts[0][:-1].lower()
    return '/'.join(['', 'mnt', drive] + parts[1:])

def _native_path(path):
    """Path in the worker's own form (Windows on Windows, /mnt on Linux/WSL)."""
    if SystemUtils.IsRunningOnWindows(): return _to_windows_path(path)
    return _to_wsl_path(path)

def _hpm_asset_suffix():
    """Release asset suffix for the worker platform (see install_hpm.sh)."""
    system = platform.system().lower()
    if system == 'windows':
        return 'windows-x86_64.exe'
    if system == 'darwin':
        return 'darwin-universal'
    return 'linux-x86_64'

def _hpm_binary_name():
    return 'hpm.exe' if platform.system().lower() == 'windows' else 'hpm'

def _http_get_json(url, timeout=30):
    request = urllib.request.Request(url, headers={
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'deadline-hpm-plugin',
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))

def _http_download(url, dest, timeout=120):
    """Download url to dest atomically (temp file + replace on same volume)."""
    request = urllib.request.Request(url, headers={'User-Agent': 'deadline-hpm-plugin'})
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix='.hpm-dl-')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, os.fdopen(fd, 'wb') as out:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                out.write(chunk)
        if platform.system().lower() != 'windows':
            os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

class HPMPlugin(DeadlinePlugin):
    """Deadline plugin that runs an HPM package's farm task in native python.

    The submitter bundles an hpm.toml job manifest whose single dependency is the
    task's package at its exact version, and whose `[scripts.task]` runs
    `python -m <module>` with `package-env = true`. On the worker this plugin:

      1. self-bootstraps an hpm binary (so render nodes need no Desktop install),
      2. `hpm -C <job_dir> install`s that manifest into the worker's local HPM
         store (resolving the package + its [python_dependencies] and pinning the
         Houdini-mapped CPython into a package-env venv),
      3. `hpm -C <job_dir> run task -- <context.json> <first> <last>` runs the
         task inside that package-env — the package importable, its python deps
         on PYTHONPATH — entirely in native python.

    The task itself bridges image tools (oiiotool/ffmpeg) to WSL where needed and
    drives husk.exe / the Windows USD resolver directly; none of that is the
    plugin's concern. This replaces the old design (a hand-built uv venv +
    PYTHONPATH + python_dependencies reconstruction + a WSL Runner.py): hpm now
    owns the environment.
    """

    def __init__(self):
        super().__init__()

        # Resolved per task in PreRenderTasks.
        self._hpm_exe = None
        self._job_dir = None

        # Callbacks
        self.InitializeProcessCallback += self._initialize_process
        self.RenderExecutableCallback += self._render_executable
        self.RenderArgumentCallback += self._render_argument
        self.PreRenderTasksCallback += self._prepare_task
        self.CheckExitCodeCallback += self._check_exit_code

    def Cleanup(self):
        del self.InitializeProcessCallback
        del self.RenderExecutableCallback
        del self.RenderArgumentCallback
        del self.PreRenderTasksCallback
        del self.CheckExitCodeCallback

    def _initialize_process(self):
        self.SingleFramesOnly = self.GetBooleanPluginInfoEntryWithDefault('SingleFramesOnly', False)
        self.PluginType = PluginType.Simple
        self.UseProcessTree = True
        self.StdoutHandling = True

        # The task prints "Progress: NN" lines; surface them as task progress.
        self.AddStdoutHandlerCallback(r'.*Progress: (\d+)%?.*').HandleCallback += self._handle_progress

    # ----- Plugin info getters -----------------------------------------------

    def get_cwd(self):
        """The shared job data dir (StartupDirectory), in the worker's path form.

        This is where the submitter bundled the task's context.json (and any
        archives/workfiles), addressed relative to it. The task runs with CWD set
        to the hpm manifest dir, so this is exported as TH_FARM_DATA and used to
        absolutize the context argument.
        """
        cwd_path = self.GetPluginInfoEntryWithDefault('StartupDirectory', '')
        return _native_path(cwd_path) if cwd_path != '' else ''

    def get_environment_path(self):
        env_path = self.GetPluginInfoEntryWithDefault('EnvironmentFile', '')
        return _native_path(env_path) if env_path != '' else ''

    def get_arguments(self):
        return self.GetPluginInfoEntryWithDefault('Arguments', '')

    def get_manifest_path(self):
        """Path to the hpm.toml the submitter bundled in the shared job dir.

        Authored at submit time so the resolved package set + the task module to
        run (`[scripts.task]`) are first-class job artifacts. Required.
        """
        manifest = self.GetPluginInfoEntryWithDefault('Manifest', '')
        return _native_path(manifest) if manifest != '' else ''

    # ----- HPM binary resolution ---------------------------------------------

    def get_hpm_executable(self):
        """Path to a usable hpm CLI.

        An explicit HpmExecutable wins (escape hatch / air-gapped workers).
        Otherwise the plugin self-bootstraps a managed binary under
        ~/.deadline/hpm so render nodes need no TumbleTrove Desktop install.
        """
        override = self.GetPluginInfoEntryWithDefault('HpmExecutable', '').strip()
        if len(override) != 0:
            return override
        return self._ensure_hpm()

    def get_hpm_version_target(self):
        """Which hpm to run: HpmVersion param, else HPM_VERSION env, else the
        studio-pinned default (>= 0.22.1 for package-env). Do NOT default to
        'latest' — an unvetted release can change behavior under the farm.
        """
        param = self.GetPluginInfoEntryWithDefault('HpmVersion', '').strip()
        if len(param) != 0:
            return param
        env = os.environ.get('HPM_VERSION', '').strip()
        if len(env) != 0:
            return env
        return DEFAULT_HPM_VERSION

    def get_hpm_managed_dir(self):
        configured = self.GetPluginInfoEntryWithDefault('HpmManagedDirectory', '').strip()
        if len(configured) != 0:
            return configured.replace('\\', '/')
        home = os.path.expanduser('~')
        return os.path.join(home, '.deadline', 'hpm').replace('\\', '/')

    def _hpm_state_path(self, managed_dir):
        return os.path.join(managed_dir, 'state.json')

    def _load_hpm_state(self, managed_dir):
        try:
            with open(self._hpm_state_path(managed_dir), 'r') as handle:
                return json.load(handle)
        except Exception:
            return {}

    def _save_hpm_state(self, managed_dir, state):
        try:
            with open(self._hpm_state_path(managed_dir), 'w') as handle:
                json.dump(state, handle)
        except Exception as error:
            self.LogWarning(f'Could not save hpm state: {error}')

    def _hpm_download_url(self, tag, suffix):
        return (
            f'https://github.com/{HPM_RELEASES_REPO}/releases/download/'
            f'{tag}/hpm-{tag}-{suffix}'
        )

    def _resolve_latest_hpm(self, managed_dir, state):
        """Resolve the newest hpm (tag, url), TTL-cached to bound API calls.

        Falls back to a stale cached resolution if the GitHub API is
        unreachable, and to (None, None) only if we've never resolved one.
        """
        now = int(time.time())
        cached_tag = state.get('latest_tag')
        if cached_tag and (now - int(state.get('latest_checked_at', 0))) < HPM_LATEST_TTL_SECONDS:
            return cached_tag, state.get('latest_url')
        suffix = _hpm_asset_suffix()
        try:
            data = _http_get_json(
                f'https://api.github.com/repos/{HPM_RELEASES_REPO}/releases/latest'
            )
            tag = data['tag_name']
            url = next(
                (
                    asset['browser_download_url']
                    for asset in data.get('assets', [])
                    if asset['browser_download_url'].endswith('-' + suffix)
                ),
                self._hpm_download_url(tag, suffix)
            )
            state['latest_tag'] = tag
            state['latest_url'] = url
            state['latest_checked_at'] = now
            self._save_hpm_state(managed_dir, state)
            return tag, url
        except Exception as error:
            self.LogWarning(f'hpm latest-version check failed: {error}')
            if cached_tag:
                return cached_tag, state.get('latest_url')
            return None, None

    def _ensure_hpm(self):
        """Make sure a managed hpm binary matching the target version exists."""
        managed_dir = self.get_hpm_managed_dir()
        os.makedirs(managed_dir, exist_ok=True)
        binary_path = os.path.join(managed_dir, _hpm_binary_name())
        suffix = _hpm_asset_suffix()
        state = self._load_hpm_state(managed_dir)
        target = self.get_hpm_version_target()

        if target == 'latest':
            tag, url = self._resolve_latest_hpm(managed_dir, state)
        else:
            tag = target if target.startswith('v') else f'v{target}'
            url = self._hpm_download_url(tag, suffix)

        if tag is None:
            if os.path.isfile(binary_path):
                self.LogWarning('Could not resolve latest hpm; using installed binary')
                return binary_path
            return self.FailRender('Could not resolve an hpm version and none is installed')

        # Up to date already?
        if os.path.isfile(binary_path) and state.get('version') == tag:
            return binary_path

        self.LogInfo(f'Bootstrapping hpm {tag} -> {url}')
        try:
            _http_download(url, binary_path)
        except Exception as error:
            if os.path.isfile(binary_path):
                self.LogWarning(f'hpm download failed ({error}); using existing binary')
                return binary_path
            return self.FailRender(f'Failed to download hpm {tag}: {error}')

        state['version'] = tag
        state['suffix'] = suffix
        self._save_hpm_state(managed_dir, state)
        return binary_path

    # ----- Diagnostics -------------------------------------------------------

    def _probe_connectivity(self):
        """Log reachability of the endpoints hpm install needs.

        Distinguishes 'reachable' (any HTTP response, incl. 4xx) from 'blocked'
        (connection/DNS failure). hpm's sync error is opaque, so this pinpoints
        which endpoint a locked-down render node can't reach.
        """
        self.LogInfo(' Probing connectivity '.center(100, '='))
        targets = [
            ('tumbletrove registry', 'https://api.tumbletrove.com/v1/registry'),
            ('tumbletrove packages', 'https://pkg.tumbletrove.com/'),
            ('pypi index', 'https://pypi.org/simple/'),
            ('pypi files', 'https://files.pythonhosted.org/'),
            ('github releases', 'https://github.com/3db-dk/hpm/releases/latest'),
        ]
        for name, url in targets:
            try:
                request = urllib.request.Request(
                    url, method='HEAD',
                    headers={'User-Agent': 'deadline-hpm-plugin'}
                )
                with urllib.request.urlopen(request, timeout=15) as response:
                    self.LogInfo(f'  reachable  {name}: HTTP {response.status}  {url}')
            except urllib.error.HTTPError as error:
                self.LogInfo(f'  reachable  {name}: HTTP {error.code} (server responded)  {url}')
            except Exception as error:
                self.LogWarning(f'  BLOCKED    {name}: {error}  {url}')

    # ----- Lifecycle ---------------------------------------------------------

    def _export_job_environment(self):
        """Set the env the task process needs (it has no WSL Runner to load it).

        - TH_FARM_DATA: the shared job data dir, so the task can resolve files
          bundled relative to it (it runs with CWD = the hpm manifest dir).
        - every KEY=VALUE from the job's EnvironmentFile (OCIO, TH_CONFIG_PATH,
          TH_PROJECT_PATH, ...): the task's `default_client()` reads these at
          import, so they must be present before `hpm run task`.
        """
        data_dir = self.get_cwd()
        if data_dir != '':
            self.SetProcessEnvironmentVariable('TH_FARM_DATA', data_dir)

        env_path = self.get_environment_path()
        if env_path == '' or not os.path.isfile(env_path):
            return
        self.LogInfo(f'Loading job environment from: {env_path}')
        with open(env_path, 'r') as handle:
            for line in handle:
                line = line.strip()
                if len(line) == 0 or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                key = key.strip()
                if len(key) == 0:
                    continue
                self.SetProcessEnvironmentVariable(key, value.strip())

    def _prepare_job_dir(self):
        """Copy the bundled manifest into a worker-local dir to install + run in.

        Worker-local (not the shared job dir): `hpm install` writes hpm.lock and
        the package-env venv keyed off this dir, and a render job's chunks run on
        many workers — installing against the shared copy would race.
        """
        manifest = self.get_manifest_path()
        if manifest == '' or not os.path.isfile(manifest):
            return self.FailRender(
                'HPM job requires a bundled Manifest (hpm.toml) with a '
                '[scripts.task] entry; none was found in the plugin info.'
            )
        self._job_dir = tempfile.mkdtemp(prefix='hpm-job-')
        shutil.copyfile(manifest, os.path.join(self._job_dir, 'hpm.toml'))
        self.LogInfo(f'Prepared worker-local job dir: {self._job_dir}')

    def _hpm_install(self):
        """Resolve the manifest into the worker's HPM store (package-env venv).

        `hpm run` with package-env is read-only and requires a prior install.
        Idempotent: on a warm store + lock it touches little network.
        """
        self.LogInfo(' Installing HPM job manifest '.center(100, '='))
        command = f'-C "{self._job_dir}" install'
        success = self.RunProcess(self._hpm_exe, command, self._job_dir, -1) == 0
        if not success:
            # hpm's sync error is opaque — probe which endpoint is unreachable.
            self._probe_connectivity()
            return self.FailRender('hpm install failed for the job manifest')

    def _prepare_task(self):
        self.LogInfo(' Preparing HPM task '.center(100, '='))
        self._hpm_exe = self.get_hpm_executable()
        self.LogInfo(f'hpm executable: {self._hpm_exe}')
        self._export_job_environment()
        self._prepare_job_dir()
        self._hpm_install()

    def _render_executable(self):
        return self._hpm_exe or self.get_hpm_executable()

    def _context_arguments(self):
        """The task's positional args, with the context path made absolute.

        The submitter passes the context path relative to the shared data dir
        (StartupDirectory). Since the task runs with CWD = the hpm manifest dir,
        absolutize the first token against the data dir so it resolves.
        """
        data_dir = self.get_cwd().rstrip('/')
        tokens = self.get_arguments().split()
        if len(tokens) != 0 and data_dir != '':
            first = tokens[0]
            is_absolute = bool(re.match(r'^[A-Za-z]:[\\/]', first)) or first.startswith('/')
            if not is_absolute:
                tokens[0] = f'{data_dir}/{first}'
        return tokens

    def _render_argument(self):
        context_tokens = self._context_arguments()
        start_frame = self.GetStartFrame()
        end_frame = self.GetEndFrame()

        command = (
            ['-C', f'"{self._job_dir}"', 'run', 'task', '--']
            + context_tokens
            + [str(start_frame), str(end_frame)]
        )

        self.LogInfo(' Running task '.center(100, '='))
        self.LogInfo(f'Job dir: {self._job_dir}')
        self.LogInfo(f'Context args: {" ".join(context_tokens)}')
        self.LogInfo(f'Frame range: {start_frame}-{end_frame}')
        return ' '.join(command)

    def _check_exit_code(self, return_code):
        # The worker-local job dir is only needed for the duration of the task.
        if self._job_dir is not None and os.path.isdir(self._job_dir):
            shutil.rmtree(self._job_dir, ignore_errors=True)
        if return_code != 0:
            self.FailRender(f'Task failed with return code: {return_code}')

    def _handle_progress(self):
        progress = float(self.GetRegexMatch(1))
        self.SetProgress(progress)
