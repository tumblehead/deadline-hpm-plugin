"""Property-based tests for the plugin's pure helpers.

Each property is checked against an independent oracle where one exists:
tomllib for manifest parsing, and a from-scratch implementation of the Windows
CommandLineToArgvW rules for the command lines handed to RunProcess.
"""
import contextlib
import os
import string
import tempfile

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

try:
    import tomllib
except ImportError:  # Python < 3.11
    tomllib = None


# ----- Strategies -------------------------------------------------------------

drive_letters = st.sampled_from(string.ascii_letters)
segment = st.text(
    alphabet=string.ascii_letters + string.digits + '._- ',
    min_size=1, max_size=12,
).filter(lambda s: s.strip() == s and s not in ('.', '..'))
segments = st.lists(segment, min_size=0, max_size=5)


@st.composite
def drive_paths(draw):
    """C:, C:/, C:/a/b, or the same with backslashes."""
    letter = draw(drive_letters)
    parts = draw(segments)
    sep = draw(st.sampled_from(['/', '\\']))
    if len(parts) == 0:
        return letter + ':' + draw(st.sampled_from(['', sep]))
    return letter + ':' + sep + sep.join(parts)


@st.composite
def posix_paths(draw):
    """Absolute POSIX paths that are not a WSL drive mount (/mnt/<letter>)."""
    parts = draw(st.lists(segment, min_size=1, max_size=5))
    path = '/' + '/'.join(parts)
    assume(not _is_wsl_drive_mount(path))
    return path


@st.composite
def unc_paths(draw):
    parts = draw(st.lists(segment, min_size=2, max_size=5))
    sep = draw(st.sampled_from(['/', '\\']))
    return sep * 2 + sep.join(parts)


def _is_wsl_drive_mount(path):
    parts = path.split('/')
    return len(parts) >= 3 and parts[1] == 'mnt' and len(parts[2]) == 1 and parts[2].isalpha()


# Arguments and dirs for command lines: anything but a double quote (the
# plugin does not support them) and NUL (cannot reach a process).
cmd_text = st.text(
    alphabet=st.characters(blacklist_characters='"\x00', blacklist_categories=('Cs',)),
    max_size=30,
)
tokens_no_ws = st.lists(
    st.text(
        alphabet=st.characters(blacklist_characters='"\x00', blacklist_categories=('Cs', 'Zs', 'Cc')),
        min_size=1, max_size=20,
    ).filter(lambda s: s.split() == [s]),
    max_size=6,
)
frames = st.integers(min_value=-100000, max_value=100000)


# ----- Oracles ----------------------------------------------------------------

def split_windows_command_line(cmd):
    """CommandLineToArgvW (msvcrt 2008+ rules), written independently of the
    plugin's quoting so it can serve as the round-trip oracle."""
    args = []
    i, n = 0, len(cmd)
    while True:
        while i < n and cmd[i] in ' \t':
            i += 1
        if i >= n:
            return args
        arg = []
        in_quotes = False
        while i < n:
            c = cmd[i]
            if c == '\\':
                j = i
                while j < n and cmd[j] == '\\':
                    j += 1
                count = j - i
                if j < n and cmd[j] == '"':
                    arg.append('\\' * (count // 2))
                    if count % 2 == 1:
                        arg.append('"')
                        i = j + 1
                    else:
                        i = j
                else:
                    arg.append('\\' * count)
                    i = j
            elif c == '"':
                if in_quotes and i + 1 < n and cmd[i + 1] == '"':
                    arg.append('"')
                    i += 2
                else:
                    in_quotes = not in_quotes
                    i += 1
            elif c in ' \t' and not in_quotes:
                break
            else:
                arg.append(c)
                i += 1
        args.append(''.join(arg))


@contextlib.contextmanager
def worker(hpm, windows, mapping=lambda p: p):
    """Run with the plugin believing it is on a Windows or POSIX worker, with
    the given repository path-mapping function."""
    saved = hpm.SystemUtils, hpm.RepositoryUtils

    class System:
        @staticmethod
        def IsRunningOnWindows():
            return windows

    class Repository:
        @staticmethod
        def CheckPathMapping(path):
            return mapping(path)

    hpm.SystemUtils, hpm.RepositoryUtils = System, Repository
    try:
        yield
    finally:
        hpm.SystemUtils, hpm.RepositoryUtils = saved


# ----- Path conversion --------------------------------------------------------

@given(drive_paths())
def test_wsl_path_of_drive_path_is_its_mount(hpm, path):
    letter, rest = path[0], path[2:].replace('\\', '/')
    assert hpm._to_wsl_path(path) == '/mnt/' + letter.lower() + rest


@given(st.one_of(posix_paths(), unc_paths()))
@example('/shared/jobs/x')
@example('/Volumes/farm/x')
def test_wsl_path_leaves_non_drive_paths_alone(hpm, path):
    # Regression: native Linux/macOS paths were rewritten to /mnt//<path>.
    assert hpm._to_wsl_path(path) == path.replace('\\', '/')


@given(st.one_of(drive_paths(), posix_paths(), unc_paths()))
def test_wsl_path_is_idempotent(hpm, path):
    once = hpm._to_wsl_path(path)
    assert hpm._to_wsl_path(once) == once
    assert '/mnt//' not in once


@given(drive_letters, segments)
def test_windows_path_of_mount_is_its_drive(hpm, letter, parts):
    rest = ''.join('/' + p for p in parts)
    assert hpm._to_windows_path('/mnt/' + letter + rest) == letter.upper() + ':' + rest


@given(st.one_of(posix_paths(), unc_paths()))
@example('/mnt/nas/farm/job')
def test_windows_path_leaves_non_mounts_alone(hpm, path):
    # Regression: any /mnt/<name> became <NAME>:, so /mnt/nas became NAS:.
    assert hpm._to_windows_path(path) == path.replace('\\', '/')


@given(st.one_of(drive_paths(), posix_paths(), unc_paths()))
def test_windows_path_is_idempotent(hpm, path):
    once = hpm._to_windows_path(path)
    assert hpm._to_windows_path(once) == once


@given(drive_paths())
def test_drive_path_round_trips_through_wsl(hpm, path):
    normalized = path[0].upper() + path[1:].replace('\\', '/')
    assert hpm._to_windows_path(hpm._to_wsl_path(path)) == normalized


@given(st.booleans(), st.one_of(drive_paths(), posix_paths(), unc_paths()))
def test_native_path_applies_path_mapping_first(hpm, windows, path):
    mapped_to = '/mapped' + path.replace('\\', '/').replace(':', '')
    seen = []

    def mapping(p):
        seen.append(p)
        return mapped_to

    with worker(hpm, windows, mapping):
        result = hpm._native_path(path)
    convert = hpm._to_windows_path if windows else hpm._to_wsl_path
    assert seen == [path]
    assert result == convert(mapped_to)


@given(st.booleans(), st.one_of(drive_paths(), posix_paths(), unc_paths()))
def test_native_path_without_mapping_rules(hpm, windows, path):
    with worker(hpm, windows):
        result = hpm._native_path(path)
    convert = hpm._to_windows_path if windows else hpm._to_wsl_path
    assert result == convert(path)


# ----- Manifest registries ----------------------------------------------------

url_text = st.text(
    alphabet=string.ascii_letters + string.digits + ':/.-_?=&%#~',
    min_size=1, max_size=40,
)


@st.composite
def manifests(draw):
    """hpm.toml-shaped text in the one-key-per-line form submitters write,
    with registries interleaved among other tables (some with url keys of
    their own), header spacing and trailing comments varied. Returns
    (text, registry urls in order)."""
    lines = []
    expected = []
    for _ in range(draw(st.integers(min_value=0, max_value=6))):
        kind = draw(st.sampled_from(['registry', 'package', 'other']))
        if kind == 'registry':
            url = draw(url_text)
            header = draw(st.sampled_from([
                '[[registries]]', '[[ registries ]]', '[[registries]]  # mirror',
            ]))
            quote = draw(st.sampled_from(['"', "'"]))
            spacing = draw(st.sampled_from(['=', ' = ', '  =  ']))
            lines.append(header)
            lines.append(f'name = "r{len(expected)}"')
            lines.append(f'url{spacing}{quote}{url}{quote}' + draw(st.sampled_from(['', '  # main'])))
            lines.append('type = "api"')
            expected.append(url)
        elif kind == 'package':
            lines.append('[package]')
            lines.append('name = "deadline-hpm-job"')
            lines.append(f'url = "{draw(url_text)}"')
        else:
            lines.append(draw(st.sampled_from(['[dependencies]', '[scripts.task]', '[compat]'])))
        lines.append('')
    # TOML forbids a second [package]/[dependencies]/...; keep the first of each.
    seen, kept, skipping = set(), [], False
    for line in lines:
        if line.startswith('[') and not line.startswith('[['):
            skipping = line in seen
            seen.add(line)
        elif line.startswith('[['):
            skipping = False
        if not skipping:
            kept.append(line)
    return '\n'.join(kept), expected


@given(manifests())
def test_scanner_finds_registry_urls(hpm, manifest):
    text, expected = manifest
    assert hpm._scan_registry_urls(text) == expected


@pytest.mark.skipif(tomllib is None, reason='needs tomllib (Python 3.11+)')
@given(manifests())
def test_scanner_agrees_with_tomllib(hpm, manifest):
    text, _ = manifest
    oracle = [r['url'] for r in tomllib.loads(text).get('registries', [])]
    assert hpm._scan_registry_urls(text) == oracle


@given(manifests())
def test_manifest_registry_urls_reads_the_file(hpm, manifest):
    text, expected = manifest
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'hpm.toml')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)
        assert hpm._manifest_registry_urls(path) == expected


def test_manifest_registry_urls_of_missing_file(hpm):
    assert hpm._manifest_registry_urls('/nonexistent/hpm.toml') == []


# ----- Env file ---------------------------------------------------------------

env_keys = st.text(
    alphabet=string.ascii_letters + string.digits + '_', min_size=1, max_size=20,
).filter(lambda k: not k.startswith('#'))
env_values = st.text(
    alphabet=st.characters(blacklist_categories=('Cs', 'Cc', 'Zl', 'Zp')),
    max_size=40,
).map(str.strip)


@given(st.lists(st.tuples(env_keys, env_values), max_size=10), st.data())
def test_env_lines_round_trip(hpm, pairs, data):
    lines = []
    for key, value in pairs:
        # Noise the parser must skip, between real entries.
        lines.extend(data.draw(st.lists(st.sampled_from(['', '   ', '# comment', '#K=V', 'no equals sign']), max_size=2)))
        pad = data.draw(st.sampled_from(['', ' ', '  ']))
        lines.append(f'{pad}{key}{pad}={pad}{value}{pad}')
    assert hpm._parse_env_lines('\n'.join(lines)) == pairs


@given(env_keys, st.lists(env_values, min_size=2, max_size=4))
def test_env_value_may_contain_equals(hpm, key, parts):
    value = '='.join(parts).strip()
    assert hpm._parse_env_lines(f'{key}={value}') == [(key, value)]


@given(st.text(max_size=200))
def test_env_parser_never_yields_empty_keys(hpm, text):
    for key, _ in hpm._parse_env_lines(text):
        assert key != '' and key == key.strip() and not key.startswith('#')


@given(st.text(max_size=100))
def test_read_env_text_decodes_utf8_exactly(hpm, text):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'job.env')
        with open(path, 'wb') as handle:
            handle.write(text.encode('utf-8'))
        assert hpm._read_env_text(path) == text


@given(st.binary(max_size=100))
def test_read_env_text_never_raises(hpm, data):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'job.env')
        with open(path, 'wb') as handle:
            handle.write(data)
        assert isinstance(hpm._read_env_text(path), str)


# ----- Task arguments ---------------------------------------------------------

relative_paths = st.lists(segment.filter(lambda s: ' ' not in s), min_size=1, max_size=4).map('/'.join)
data_dirs = st.one_of(drive_paths(), posix_paths(), unc_paths())


@given(data_dirs, relative_paths, tokens_no_ws, st.sampled_from(['', '/']))
@example('//server/Jobs/Shot 010', 'context.json', [], '')
def test_relative_context_is_absolutized(hpm, data_dir, first, rest, trailing):
    tokens = hpm._context_tokens(' '.join([first] + rest), data_dir + trailing)
    assert tokens == [data_dir.rstrip('/') + '/' + first] + rest


# A bare `C:` is drive-relative on Windows, not absolute, so it is excluded.
absolute_paths = st.one_of(drive_paths().filter(lambda p: len(p) > 2), posix_paths(), unc_paths())


@given(data_dirs, absolute_paths, tokens_no_ws)
@example('C:/jobs', r'\\server\share\context.json', [])
def test_absolute_context_is_left_alone(hpm, data_dir, first, rest):
    # Regression: a backslash UNC context path was prefixed with the data dir.
    assume(first.split() == [first])
    assert hpm._context_tokens(' '.join([first] + rest), data_dir) == [first] + rest


@given(st.text(max_size=60))
def test_context_without_data_dir_is_a_plain_split(hpm, arguments):
    assert hpm._context_tokens(arguments, '') == arguments.split()


@given(cmd_text, st.lists(cmd_text, max_size=6), frames, frames)
@example('C:/Users/me/AppData/Local/Temp/hpm-job-x', ['//server/Jobs/Shot 010/context.json'], 1001, 1001)
@example('C:\\Temp\\hpm-job-x\\', ['C:\\dir with space\\'], 1, 2)
def test_task_command_round_trips(hpm, job_dir, tokens, first, last):
    # Regression: args were joined unquoted, so a data dir with a space split
    # the context path into two arguments.
    command = hpm._task_command(job_dir, tokens, first, last)
    expected = ['-C', job_dir, 'run', 'task', '--'] + tokens + [str(first), str(last)]
    assert split_windows_command_line(command) == expected


@given(cmd_text)
def test_install_command_round_trips(hpm, job_dir):
    assert split_windows_command_line(hpm._install_command(job_dir)) == ['-C', job_dir, 'install']


@given(tokens_no_ws.filter(lambda ts: all('\\' not in t for t in ts)))
def test_plain_tokens_are_not_quoted(hpm, tokens):
    # Keeps the command line identical to the pre-quoting plugin for the
    # common case, so existing farms see no change.
    assert all(hpm._quote_arg(t) == t for t in tokens)


# ----- hpm version ------------------------------------------------------------

versions = st.from_regex(r'\A[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\Z')


@given(versions, st.booleans())
def test_normalize_tag(hpm, version, prefixed):
    tag = hpm._normalize_tag(('v' if prefixed else '') + version)
    assert tag == 'v' + version
    assert hpm._normalize_tag(tag) == tag


def test_default_version_is_a_tag(hpm):
    assert hpm._normalize_tag(hpm.DEFAULT_HPM_VERSION) == hpm.DEFAULT_HPM_VERSION


blank_or_text = st.one_of(st.just(''), st.just('   '), st.text(alphabet=string.printable, max_size=10))


@given(blank_or_text, blank_or_text)
def test_version_target_precedence(hpm, param, env):
    plugin = object.__new__(hpm.HPMPlugin)
    plugin.GetPluginInfoEntryWithDefault = lambda key, default: param if key == 'HpmVersion' else default
    saved = os.environ.get('HPM_VERSION')
    os.environ['HPM_VERSION'] = env.replace('\x00', '')
    try:
        target = plugin.get_hpm_version_target()
    finally:
        if saved is None:
            os.environ.pop('HPM_VERSION', None)
        else:
            os.environ['HPM_VERSION'] = saved
    if param.strip():
        assert target == param.strip()
    elif env.replace('\x00', '').strip():
        assert target == env.replace('\x00', '').strip()
    else:
        assert target == hpm.DEFAULT_HPM_VERSION
