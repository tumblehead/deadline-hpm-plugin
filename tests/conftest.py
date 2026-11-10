"""Load HPM.py outside Deadline.

The plugin does `from Deadline.Plugins import *` and
`from Deadline.Scripting import *` at import time. These stubs provide just
the names the module-level code touches, so the pure helpers can be tested
with plain pytest. Tests that need a specific OS or path-mapping behavior
patch `SystemUtils` / `RepositoryUtils` on the loaded module.
"""
import importlib.util
import pathlib
import sys
import types

import pytest

PLUGIN_PATH = pathlib.Path(__file__).resolve().parent.parent / 'HPM.py'


class _DeadlinePlugin:
    pass


class _SystemUtils:
    @staticmethod
    def IsRunningOnWindows():
        return False


class _RepositoryUtils:
    @staticmethod
    def CheckPathMapping(path):
        return path


def _install_deadline_stubs():
    deadline = types.ModuleType('Deadline')
    plugins = types.ModuleType('Deadline.Plugins')
    plugins.DeadlinePlugin = _DeadlinePlugin
    plugins.PluginType = types.SimpleNamespace(Simple='Simple')
    plugins.__all__ = ['DeadlinePlugin', 'PluginType']
    scripting = types.ModuleType('Deadline.Scripting')
    scripting.SystemUtils = _SystemUtils
    scripting.RepositoryUtils = _RepositoryUtils
    scripting.__all__ = ['SystemUtils', 'RepositoryUtils']
    deadline.Plugins = plugins
    deadline.Scripting = scripting
    sys.modules.setdefault('Deadline', deadline)
    sys.modules.setdefault('Deadline.Plugins', plugins)
    sys.modules.setdefault('Deadline.Scripting', scripting)


def load_plugin():
    _install_deadline_stubs()
    spec = importlib.util.spec_from_file_location('HPM', PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='session')
def hpm():
    return load_plugin()
