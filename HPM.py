#!/usr/bin/env python3
from __future__ import absolute_import

from Deadline.Plugins import *
from Deadline.Scripting import *

import urllib.request
import tempfile
import platform
import random
import shutil
import string
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

def GetDeadlinePlugin():
    return HPMPlugin()

def CleanupDeadlinePlugin(deadlinePlugin):
    deadlinePlugin.Cleanup()

def _to_wsl_path(path):
    raw_path = path.replace('\\', '/')
    if raw_path.startswith('/mnt/'): return raw_path
    parts = raw_path.split('/')
    drive = parts[0][:-1].lower()
    return '/'.join(['', 'mnt', drive] + parts[1:])

def _to_windows_path(path):
    raw_path = path.replace('\\', '/')
    if not raw_path.startswith('/mnt/'): return raw_path
    parts = raw_path.split('/')
    drive = f'{parts[2].upper()}:'
    return '/'.join([drive] + parts[3:])

def _random_env_name():
    return ''.join(random.choices(string.hexdigits, k=16))

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

def _split_specs(text):
    """Split a 'tumblepipe@1.11.0, tumblerig@1.5.2' string into specs."""
    return [
        part.strip()
        for part in re.split(r'[,\s]+', text)
        if len(part.strip()) != 0
    ]

def _parse_python_dependencies(manifest_text):
    """Read [python_dependencies] from an hpm.toml into pip requirement strings.

    The farm builds a fresh venv per task, so it has to reconstruct the same
    third-party deps HPM provisions in-Houdini (pymongo, dictdiffer, ...). We
    avoid a tomllib dependency (Deadline's embedded Python version varies) and
    parse just the one flat string-valued table we care about. Table-valued
    entries (`name = { version = "..." }`) are skipped with a warning upstream.
    """
    requirements = []
    in_section = False
    entry = re.compile(r'^\s*([A-Za-z0-9_.\-]+)\s*=\s*(.+?)\s*(?:#.*)?$')
    for line in manifest_text.splitlines():
        stripped = line.strip()
        if stripped.startswith('['):
            in_section = (stripped.rstrip() == '[python_dependencies]')
            continue
        if not in_section:
            continue
        match = entry.match(line)
        if match is None:
            continue
        name, raw_value = match.group(1), match.group(2).strip()
        if raw_value.startswith('{'):
            # Table form (extras/markers) — not handled by this parser.
            requirements.append((name, None))
            continue
        spec = raw_value.strip().strip('"').strip("'")
        if spec in ('', '*'):
            requirements.append((name, ''))
        else:
            requirements.append((name, spec))
    return requirements

class HPMPlugin(DeadlinePlugin):
    """Deadline plugin that runs an HPM package's task script.

    Unlike the UV plugin (which runs a fixed absolute ScriptFile baked at submit
    time from the submitter's own .hpm store), this plugin is given a package
    *identity* (`Package=tumblepipe@1.11.0`) plus a package-relative ScriptFile.
    It resolves that identity against the *worker's* local HPM store, installing
    the exact version on a cache miss, then runs the task with the package's
    python/ dir on PYTHONPATH and its declared [python_dependencies] installed
    into the venv. That makes a worker reproduce the submitter's source + env
    instead of assuming the two machines mirror each other on disk.
    """

    def __init__(self):
        super().__init__()

        # Members
        self._env_name = _random_env_name()
        self._venv_path = None

        # Callbacks
        self.InitializeProcessCallback += self._initialize_process
        self.RenderExecutableCallback += self._render_executable
        self.RenderArgumentCallback += self._render_argument
        self.PreRenderTasksCallback += self._create_python_environment
        self.CheckExitCodeCallback += self._remove_python_environment

    def Cleanup(self):
        del self.InitializeProcessCallback
        del self.RenderExecutableCallback
        del self.RenderArgumentCallback
        del self.PreRenderTasksCallback
        del self.CheckExitCodeCallback

    def _initialize_process(self):

        # Settings
        self.SingleFramesOnly = self.GetBooleanPluginInfoEntryWithDefault('SingleFramesOnly', False)
        self.PluginType = PluginType.Simple

        self.UseProcessTree = True
        self.StdoutHandling = True

        # Set up stdout handlers
        self.AddStdoutHandlerCallback(r'.*Progress: (\d+)%.*').HandleCallback += self._handle_progress

    # ----- Plugin info getters -----------------------------------------------

    def get_env(self):
        return self._env_name

    def get_cwd(self):
        cwd_path = self.GetPluginInfoEntryWithDefault('StartupDirectory', '')
        if SystemUtils.IsRunningOnWindows(): return _to_windows_path(cwd_path)
        return _to_wsl_path(cwd_path)

    def get_python_version(self):
        return self.GetPluginInfoEntryWithDefault('PythonVersion', '3.11')

    def get_environment_path(self):
        env_path = self.GetPluginInfoEntryWithDefault('EnvironmentFile', '')
        if SystemUtils.IsRunningOnWindows(): return _to_windows_path(env_path)
        return _to_wsl_path(env_path)

    def get_requirements_path(self):
        req_path = self.GetPluginInfoEntryWithDefault('RequirementsFile', '')
        if SystemUtils.IsRunningOnWindows(): return _to_windows_path(req_path)
        return _to_wsl_path(req_path)

    def get_arguments(self):
        return self.GetPluginInfoEntryWithDefault('Arguments', '')

    def get_cache_dir(self):
        cache_path = self.GetPluginInfoEntryWithDefault('CacheDirectory', '/tmp/uv-cache')
        if SystemUtils.IsRunningOnWindows(): return _to_windows_path(cache_path)
        return _to_wsl_path(cache_path)

    # ----- HPM resolution ----------------------------------------------------

    def get_hpm_executable(self):
        """Path to a usable hpm CLI.

        An explicit HpmExecutable wins (escape hatch / air-gapped workers).
        Otherwise the plugin self-bootstraps a managed binary under
        ~/.deadline/hpm so render nodes need no TumbleTrove Desktop install.
        Only ever called on a package cache miss.
        """
        override = self.GetPluginInfoEntryWithDefault('HpmExecutable', '').strip()
        if len(override) != 0:
            return override
        return self._ensure_hpm()

    def get_hpm_version_target(self):
        """Which hpm to run: HpmVersion param, else HPM_VERSION env, else the
        studio-pinned default.

        Defaults to the studio's central HPM_VERSION knob (Infisical, prod) so
        the worker runs the SAME hpm as CI and the submitter. Do NOT default to
        'latest' — a new hpm release (e.g. 0.20.0) regressed `install` sync and
        broke the farm. Keep this in lockstep with the Infisical HPM_VERSION.
        """
        param = self.GetPluginInfoEntryWithDefault('HpmVersion', '').strip()
        if len(param) != 0:
            return param
        env = os.environ.get('HPM_VERSION', '').strip()
        if len(env) != 0:
            return env
        return 'v0.18.0'

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

    def get_packages_dir(self):
        """Local HPM package store on the worker (native, forward-slashed)."""
        configured = self.GetPluginInfoEntryWithDefault('HpmPackagesDirectory', '')
        if len(configured) != 0:
            return configured.replace('\\', '/')
        home = os.path.expanduser('~')
        return os.path.join(home, '.hpm', 'packages').replace('\\', '/')

    def get_manifest_path(self):
        """Path to the hpm.toml the submitter bundled in the shared job dir.

        Authored at submit time so the resolved package set is a first-class job
        artifact (not generated ad-hoc on the worker). Empty if not provided.
        """
        manifest = self.GetPluginInfoEntryWithDefault('Manifest', '')
        if len(manifest) == 0:
            return ''
        if SystemUtils.IsRunningOnWindows(): return _to_windows_path(manifest)
        return _to_wsl_path(manifest)

    def get_primary_package(self):
        spec = self.GetPluginInfoEntryWithDefault('Package', '')
        assert len(spec) != 0, 'Package plugin info entry is required (e.g. tumblepipe@1.11.0)'
        return spec.strip()

    def get_packages(self):
        """Primary package first, then any ExtraPackages (deps the task imports)."""
        packages = [self.get_primary_package()]
        extra = self.GetPluginInfoEntryWithDefault('ExtraPackages', '')
        for spec in _split_specs(extra):
            if spec not in packages:
                packages.append(spec)
        return packages

    def _package_root_native(self, spec):
        """Native (forward-slashed) path to a resolved package in the store."""
        return f'{self.get_packages_dir()}/{spec}'

    def get_relative_script(self):
        rel = self.GetPluginInfoEntryWithDefault('ScriptFile', '')
        assert len(rel) != 0, 'ScriptFile plugin info entry is required (package-relative)'
        return rel.replace('\\', '/').lstrip('/')

    def get_script_path(self):
        """Absolute, native path to the task script inside the primary package."""
        return f'{self._package_root_native(self.get_primary_package())}/{self.get_relative_script()}'

    def get_pythonpath_entries(self):
        """python/ dir of every resolved package, native paths."""
        return [f'{self._package_root_native(spec)}/python' for spec in self.get_packages()]

    # ----- Process running helpers -------------------------------------------

    def _run_windows(self, command, cwd_path):
        command = ['--shell-type', 'login'] + command
        arguments = ' '.join(filter(lambda part: len(part) != 0, command))
        return self.RunProcess('C:/Windows/System32/wsl.exe', arguments, cwd_path, -1) == 0

    def _run_linux(self, command, cwd_path):
        arguments = ' '.join(filter(lambda part: len(part) != 0, command))
        return self.RunProcess('/usr/bin/bash', arguments, cwd_path, -1) == 0

    def _run(self, command, cwd_path):
        """Run a command in WSL (Windows worker) or bash (Linux worker)."""
        if SystemUtils.IsRunningOnWindows():
            return self._run_windows(command, cwd_path)
        return self._run_linux(command, cwd_path)

    def _run_hpm(self, hpm_args, cwd_path):
        """Run the worker-native hpm CLI (NOT inside WSL).

        The HPM store lives on the worker's native filesystem (the same store
        TumbleTrove Desktop manages), so resolution runs natively; the WSL-side
        render then reads it through /mnt/<drive>.
        """
        exe = self.get_hpm_executable()
        arguments = ' '.join(filter(lambda part: len(part) != 0, hpm_args))
        return self.RunProcess(exe, arguments, cwd_path, -1) == 0

    # ----- Lifecycle ---------------------------------------------------------

    def _synthesize_manifest(self, specs):
        """Fallback manifest for jobs that didn't bundle one (e.g. legacy jobs).

        Note the required [package].path field — hpm refuses to load a manifest
        without it ('Failed to load manifest').
        """
        dep_lines = []
        for spec in specs:
            if '@' in spec:
                name, version = spec.split('@', 1)
                # Bare version = exact registry get_version fetch. A "=" prefix
                # is sent verbatim into the registry query and 404s.
                dep_lines.append(f'{name} = "{version}"')
            else:
                dep_lines.append(f'{spec} = "*"')
        return (
            '[package]\n'
            'path = "local/deadline-hpm-job"\n'
            'name = "deadline-hpm-job"\n'
            'version = "0.0.0"\n\n'
            '[compat]\n'
            'houdini = ">=21, <99"\n\n'
            '[dependencies]\n'
            + '\n'.join(dep_lines) + '\n'
        )

    def _ensure_packages(self, cwd_path):
        """Make sure every requested package@version exists in the local store.

        Fast path: the store is content-addressed at packages/<name>@<version>,
        so a present dir means a hit and we touch no network. On a miss we run
        `hpm install` against the manifest the submitter bundled in the shared
        job dir.

        hpm writes hpm.lock + .hpm/ NEXT TO the manifest, so we never install
        directly against the shared copy (a render job's chunks install
        concurrently on different workers and would race). Instead we copy the
        manifest into a worker-local temp dir and install there.
        """
        packages = self.get_packages()
        packages_dir = self.get_packages_dir()

        missing = [
            spec for spec in packages
            if not os.path.isdir(os.path.join(packages_dir, spec))
        ]
        if len(missing) == 0:
            self.LogInfo(f'All packages present in store: {", ".join(packages)}')
            return

        self.LogInfo(f'Resolving packages via HPM: {", ".join(missing)}')

        # Worker-local install dir so hpm.lock / .hpm land off the shared drive.
        job_dir = tempfile.mkdtemp(prefix='hpm-job-')
        manifest_path = os.path.join(job_dir, 'hpm.toml')

        bundled_manifest = self.get_manifest_path()
        if bundled_manifest != '' and os.path.isfile(bundled_manifest):
            self.LogInfo(f'Using bundled job manifest: {bundled_manifest}')
            shutil.copyfile(bundled_manifest, manifest_path)
        else:
            self.LogInfo('No bundled manifest; synthesizing one for the missing packages')
            with open(manifest_path, 'w') as handle:
                handle.write(self._synthesize_manifest(missing))

        success = self._run_hpm(['install', '-m', manifest_path], job_dir)
        if not success:
            return self.FailRender(
                f'Failed to resolve packages via HPM: {", ".join(missing)}'
            )

        # Verify the install actually produced what we asked for.
        still_missing = [
            spec for spec in missing
            if not os.path.isdir(os.path.join(packages_dir, spec))
        ]
        if len(still_missing) != 0:
            return self.FailRender(
                'HPM install did not produce expected packages: '
                f'{", ".join(still_missing)}'
            )

    def _collect_package_requirements(self):
        """Union the [python_dependencies] of every resolved package."""
        requirements = {}
        for spec in self.get_packages():
            manifest_path = os.path.join(self.get_packages_dir(), spec, 'hpm.toml')
            if not os.path.isfile(manifest_path):
                self.LogWarning(f'No hpm.toml for package {spec}; skipping its python deps')
                continue
            with open(manifest_path, 'r') as handle:
                for name, version_spec in _parse_python_dependencies(handle.read()):
                    if version_spec is None:
                        self.LogWarning(
                            f'Skipping table-form python dependency "{name}" in {spec} '
                            '(only string version specs are reconstructed)'
                        )
                        continue
                    requirements[name] = version_spec
        return [
            name if spec == '' else f'{name}{spec}'
            for name, spec in requirements.items()
        ]

    def _create_python_environment(self):

        # Parameters
        cwd_path = self.get_cwd()
        cache_dir = self.get_cache_dir()

        # Set venv path in temp directory
        self._venv_path = f'/tmp/uv-venvs/{self.get_env()}'

        # 1. Ensure the submitted package set exists in the worker's HPM store.
        self.LogInfo(' Resolving HPM packages '.center(100, '='))
        self._ensure_packages(cwd_path)

        # Ensure temp and cache directories exist
        success = self._run(['mkdir', '-p', '/tmp/uv-venvs', cache_dir], cwd_path)
        if not success: return self.FailRender('Failed to create temp directories')

        # 2. Create a python environment with UV
        python_version = self.get_python_version()
        self.LogInfo(f' Creating python {python_version} environment with UV '.center(100, '='))
        success = self._run([
            'uv', 'venv',
            self._venv_path,
            '--python', python_version,
            '--cache-dir', cache_dir
        ], cwd_path)
        if not success: return self.FailRender('Failed to create python environment')

        # 3. Install base requirements (python-dotenv for Runner.py)
        self.LogInfo(' Installing base requirements '.center(100, '='))
        success = self._run([
            'uv', 'pip', 'install',
            '--python', f'{self._venv_path}/bin/python',
            '--cache-dir', cache_dir,
            'python-dotenv'
        ], cwd_path)
        if not success: return self.FailRender('Failed to install plugin requirements')

        # 4. Install the resolved packages' declared python dependencies.
        package_requirements = self._collect_package_requirements()
        if len(package_requirements) != 0:
            self.LogInfo(' Installing package python dependencies '.center(100, '='))
            self.LogInfo(f'Requirements: {", ".join(package_requirements)}')
            success = self._run([
                'uv', 'pip', 'install',
                '--python', f'{self._venv_path}/bin/python',
                '--cache-dir', cache_dir,
                *package_requirements
            ], cwd_path)
            if not success: return self.FailRender('Failed to install package python dependencies')

        # 5. Install any extra job requirements (legacy RequirementsFile).
        req_file_path = self.get_requirements_path()
        if req_file_path != '':
            self.LogInfo(' Installing job requirements '.center(100, '='))
            success = self._run([
                'uv', 'pip', 'install',
                '--python', f'{self._venv_path}/bin/python',
                '--cache-dir', cache_dir,
                '-r', _to_wsl_path(req_file_path)
            ], cwd_path)
            if not success: return self.FailRender('Failed to install job requirements')

    def _render_executable(self):
        if SystemUtils.IsRunningOnWindows():
            return 'C:/Windows/System32/wsl.exe'
        return '/usr/bin/bash'

    def _runner_script_path(self):
        return os.path.join(os.path.dirname(__file__), 'Runner.py')

    def _runner_command(self, script_path, pythonpath_entries, arguments):
        command = [
            f'{self._venv_path}/bin/python',
            _to_wsl_path(self._runner_script_path())
        ]
        env_path = self.get_environment_path()
        if env_path != '': command += ['--env', _to_wsl_path(env_path)]
        command += ['--cwd', _to_wsl_path(self.get_cwd())]
        for entry in pythonpath_entries:
            command += ['--pythonpath', _to_wsl_path(entry)]
        command += [_to_wsl_path(script_path), *arguments]
        return command

    def _render_argument(self):

        # Parameters and paths
        env_name = self.get_env()
        cwd_path = self.get_cwd()
        packages = self.get_packages()
        script_path = self.get_script_path()
        pythonpath_entries = self.get_pythonpath_entries()
        arguments = self.get_arguments()
        start_frame = self.GetStartFrame()
        end_frame = self.GetEndFrame()

        # Report settings
        self.LogInfo(' Running task '.center(100, '='))
        self.LogInfo(f'Environment name: {env_name}')
        self.LogInfo(f'Venv path: {self._venv_path}')
        self.LogInfo(f'CWD path found: {cwd_path}')
        self.LogInfo(f'Packages: {", ".join(packages)}')
        self.LogInfo(f'Script found: {script_path}')
        self.LogInfo(f'PYTHONPATH: {os.pathsep.join(pythonpath_entries)}')
        self.LogInfo(f'Arguments found: {arguments}')
        self.LogInfo(f'Frame range: {start_frame}-{end_frame}')

        # Run the script in the python environment
        return ' '.join(
            (['--shell-type', 'login'] if SystemUtils.IsRunningOnWindows() else []) +
            self._runner_command(
                script_path,
                pythonpath_entries,
                arguments.split(' ') + [str(start_frame), str(end_frame)]
            )
        )

    def _remove_python_environment(self, return_code):

        # Parameters
        cwd_path = self.get_cwd()

        # Remove the python environment
        self.LogInfo(' Removing python environment '.center(100, '='))
        success = self._run(['rm', '-rf', self._venv_path], cwd_path)
        if not success: return self.FailRender('Failed to remove python environment')

        # Handle the return code
        if return_code == 0: return
        self.FailRender(f'Failed with return code: {return_code}')

    def _handle_progress(self):
        progress = float(self.GetRegexMatch(1))
        self.SetProgress(progress)
