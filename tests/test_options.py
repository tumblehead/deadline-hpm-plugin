"""HPM.options (the Monitor's job form) and HPM.py must name the same keys.

An option the code never reads silently does nothing (HpmPackagesDirectory
did, for a while); a key the code reads with no option cannot be set from the
Monitor.
"""
import configparser
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Shown on the job for introspection only; the plugin does not read them.
INFORMATIONAL = {'Package', 'ScriptFile'}


def option_keys():
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(ROOT / 'HPM.options', encoding='utf-8')
    return set(parser.sections())


def keys_read_by_plugin():
    source = (ROOT / 'HPM.py').read_text(encoding='utf-8')
    return set(re.findall(r"Get\w*PluginInfoEntry\w*\(\s*'([A-Za-z]+)'", source))


def test_every_option_is_read():
    assert option_keys() - INFORMATIONAL == keys_read_by_plugin()


def test_option_indices_are_unique_per_category():
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(ROOT / 'HPM.options', encoding='utf-8')
    seen = {}
    for section in parser.sections():
        key = (parser[section]['Category'], parser[section]['Index'])
        assert key not in seen, f'{section} and {seen[key]} share {key}'
        seen[key] = section
