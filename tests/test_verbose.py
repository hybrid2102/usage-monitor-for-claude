"""
Verbose Diagnostics Tests
==========================

Unit tests for the --verbose diagnostic helpers.
"""
from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from usage_monitor_for_claude.verbose import (
    _credentials_status,
    _package_version,
    _redact_home,
    _row,
    _section,
    print_runtime_diagnostics,
    print_startup_diagnostics,
    setup_console,
)


def _symlinked_home(tmp: str) -> Path:
    """Build a home directory reached through a symlink inside ``tmp``.

    Skips the calling test where the platform does not let this process
    create one (Windows without Developer Mode).
    """
    real_home = Path(tmp) / 'realhome'
    real_home.mkdir()
    linked_home = Path(tmp) / 'linkhome'

    try:
        linked_home.symlink_to(real_home, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        raise unittest.SkipTest(f'symlink creation unavailable: {error}') from error

    return linked_home


class TestRedactHome(unittest.TestCase):
    """Tests for _redact_home() path sanitization."""

    def test_replaces_home_prefix(self):
        """Paths under the home directory are redacted with ~."""
        home = str(Path.home())
        self.assertEqual(
            _redact_home(f'{home}{os.sep}.claude{os.sep}.credentials.json'),
            f'~{os.sep}.claude{os.sep}.credentials.json',
        )

    def test_leaves_other_paths_unchanged(self):
        """Paths outside the home directory are not modified."""
        outside = os.sep.join(('', 'opt', 'PythonDev', 'app'))
        self.assertEqual(_redact_home(outside), outside)

    def test_empty_string(self):
        """Empty string is returned unchanged."""
        self.assertEqual(_redact_home(''), '')

    @unittest.skipUnless(sys.platform == 'win32', 'path casing only collapses on Windows')
    def test_case_insensitive_match(self):
        """Windows paths are case-insensitive - a differently-cased home prefix
        (e.g. CLAUDE_CONFIG_DIR set as c:\\users\\...) must still be redacted."""
        home = str(Path.home())
        self.assertEqual(_redact_home(f'{home.swapcase()}{os.sep}.claude{os.sep}file'), f'~{os.sep}.claude{os.sep}file')

    @unittest.skipIf(sys.platform == 'win32', 'POSIX paths are case-sensitive')
    def test_case_sensitive_on_posix(self):
        """A differently-cased prefix names a different directory on POSIX."""
        home = str(Path.home())
        path = f'{home.swapcase()}{os.sep}.claude'
        self.assertEqual(_redact_home(path), path)

    def test_prefix_boundary_not_partially_redacted(self):
        """A sibling profile whose name merely starts with the username
        (``/home/jens`` vs ``/home/jensen``) must not be partially redacted."""
        home = str(Path.home())
        sibling = f'{home}en{os.sep}file.txt'
        self.assertEqual(_redact_home(sibling), sibling)

    def test_exact_home_path_redacted(self):
        """The home directory itself is redacted to ~."""
        home = str(Path.home())
        self.assertEqual(_redact_home(home), '~')

    def test_symlinked_home_redacted_in_both_spellings(self):
        """Where the home directory is reached through a symlink or a junction,
        the linked and the resolved spelling name the same directory - a path
        that arrives already resolved (the credentials file) must be redacted
        just like one that keeps the linked spelling (``sys.executable``)."""
        with TemporaryDirectory() as tmp:
            linked_home = _symlinked_home(tmp)

            with patch.object(Path, 'home', return_value=linked_home):
                resolved = str(linked_home.resolve() / '.claude' / '.credentials.json')
                self.assertEqual(_redact_home(resolved), f'~{os.sep}.claude{os.sep}.credentials.json')
                self.assertEqual(_redact_home(str(linked_home / 'venv' / 'python')), f'~{os.sep}venv{os.sep}python')


    def test_unresolvable_home_still_redacts(self):
        """Resolving the home directory fails behind a symlink loop and on an
        unreachable network path - the diagnostics must still print, with the
        literal spelling redacted as before."""
        home = str(Path.home())
        for error in (RuntimeError('Symlink loop'), OSError('network path unavailable')):
            with self.subTest(error=type(error).__name__), patch.object(Path, 'resolve', side_effect=error):
                self.assertEqual(_redact_home(f'{home}{os.sep}.claude'), f'~{os.sep}.claude')


class TestSection(unittest.TestCase):
    """Tests for _section() header formatting."""

    def test_prints_title_and_underline(self):
        """Section prints title with matching-length underline."""
        buf = io.StringIO()
        with patch('sys.stdout', buf):
            _section('System')
        lines = buf.getvalue().split('\n')
        self.assertIn('System', lines[1])
        self.assertEqual(len('System'), len(lines[2].strip()))
        self.assertTrue(all(ch == '-' for ch in lines[2].strip()))


class TestRow(unittest.TestCase):
    """Tests for _row() key-value formatting."""

    def test_default_indent(self):
        """Row uses 4-space indent by default."""
        buf = io.StringIO()
        with patch('sys.stdout', buf):
            _row('OS', 'Windows 11')
        output = buf.getvalue()
        self.assertTrue(output.startswith('    '))
        self.assertIn('OS:', output)
        self.assertIn('Windows 11', output)

    def test_custom_indent(self):
        """Row respects custom indent parameter."""
        buf = io.StringIO()
        with patch('sys.stdout', buf):
            _row('Key', 'Value', indent=8)
        self.assertTrue(buf.getvalue().startswith('        '))

    def test_column_alignment(self):
        """Short and long labels produce aligned value columns."""
        buf1 = io.StringIO()
        buf2 = io.StringIO()
        with patch('sys.stdout', buf1):
            _row('OS', 'val1')
        with patch('sys.stdout', buf2):
            _row('Filesystem encoding', 'val2')
        # Values should start at the same column position
        pos1 = buf1.getvalue().index('val1')
        pos2 = buf2.getvalue().index('val2')
        self.assertEqual(pos1, pos2)


class TestPackageVersion(unittest.TestCase):
    """Tests for _package_version()."""

    def test_existing_package(self):
        """Known package returns its version string."""
        version = _package_version('pip')
        self.assertRegex(version, r'^\d+\.\d+')

    def test_missing_package(self):
        """Non-existent package returns 'not found'."""
        self.assertEqual(_package_version('nonexistent-pkg-12345'), 'not found')


class TestCredentialsStatus(unittest.TestCase):
    """Tests for _credentials_status()."""

    def test_found(self):
        """Reports 'found' with the redacted default path when the file exists."""
        with TemporaryDirectory() as home_tmp:
            claude_dir = Path(home_tmp) / '.claude'
            claude_dir.mkdir()
            (claude_dir / '.credentials.json').write_text('{}', encoding='utf-8')
            with patch.object(Path, 'home', return_value=Path(home_tmp)), patch.dict('os.environ', {}, clear=False):
                os.environ.pop('CLAUDE_CONFIG_DIR', None)
                result = _credentials_status()
        self.assertTrue(result.startswith('found'))
        self.assertIn(f'~{os.sep}.claude{os.sep}.credentials.json', result)

    def test_not_found(self):
        """Reports 'NOT FOUND' with the redacted default path when the file is missing."""
        with TemporaryDirectory() as home_tmp:
            (Path(home_tmp) / '.claude').mkdir()
            with patch.object(Path, 'home', return_value=Path(home_tmp)), patch.dict('os.environ', {}, clear=False):
                os.environ.pop('CLAUDE_CONFIG_DIR', None)
                result = _credentials_status()
        self.assertTrue(result.startswith('NOT FOUND'))
        self.assertIn(f'~{os.sep}.claude{os.sep}.credentials.json', result)

    def test_symlinked_home_stays_redacted(self):
        """The credentials path is resolved before it is printed - with the home
        directory reached through a symlink the row must still show ~, because
        this is the dump users paste into public issues."""
        with TemporaryDirectory() as tmp:
            linked_home = _symlinked_home(tmp)
            (linked_home / '.claude').mkdir()
            (linked_home / '.claude' / '.credentials.json').write_text('{}', encoding='utf-8')

            with patch.object(Path, 'home', return_value=linked_home), patch.dict('os.environ', {}, clear=False):
                os.environ.pop('CLAUDE_CONFIG_DIR', None)
                result = _credentials_status()

            self.assertTrue(result.startswith('found'))
            self.assertIn(f'~{os.sep}.claude{os.sep}.credentials.json', result)
            self.assertNotIn(str(linked_home.resolve()), result)

    def test_custom_config_dir(self):
        """Respects CLAUDE_CONFIG_DIR environment variable."""
        with TemporaryDirectory() as tmp:
            (Path(tmp) / '.credentials.json').write_text('{}', encoding='utf-8')
            with patch.dict('os.environ', {'CLAUDE_CONFIG_DIR': tmp}):
                result = _credentials_status()
        self.assertTrue(result.startswith('found'))
        self.assertIn(Path(tmp).name, result)


class TestPrintStartupDiagnostics(unittest.TestCase):
    """Tests for print_startup_diagnostics() output.

    The platform probes are stubbed: this checks the report skeleton, which
    is what this module owns.  Each backend's rows are tested with it.
    """

    def _run(self) -> str:
        buf = io.StringIO()
        with patch('sys.stdout', buf), \
             patch('usage_monitor_for_claude.verbose.diagnostic_system_rows', return_value=[('OS', 'TestOS')]), \
             patch('usage_monitor_for_claude.verbose.diagnostic_display_rows', return_value=[('Monitors', '2')]), \
             patch('usage_monitor_for_claude.verbose.diagnostic_runtime_rows', return_value=[('Toolkit', '1.0')]), \
             patch('usage_monitor_for_claude.verbose.DIAGNOSTIC_PACKAGES', ('requests',)):
            print_startup_diagnostics()

        return buf.getvalue()

    def test_contains_all_sections(self):
        """Every section header is present."""
        output = self._run()
        for section in ('System', 'Python', 'Locale', 'Display', 'Runtimes', 'Dependencies', 'Credentials'):
            with self.subTest(section=section):
                self.assertIn(section, output)

    def test_contains_version(self):
        """Output includes the app version."""
        from usage_monitor_for_claude import __version__

        self.assertIn(__version__, self._run())

    def test_platform_rows_are_rendered(self):
        """Rows supplied by the platform backend reach the output."""
        output = self._run()
        self.assertIn('TestOS', output)
        self.assertIn('Toolkit', output)

    def test_home_directory_is_not_leaked(self):
        """The interpreter path is redacted so the username stays private."""
        home = str(Path.home())
        with patch.object(sys, 'executable', f'{home}{os.sep}venv{os.sep}python'):
            output = self._run()
        self.assertNotIn(home, output)
        self.assertIn(f'~{os.sep}venv', output)


class TestPrintRuntimeDiagnostics(unittest.TestCase):
    """Tests for print_runtime_diagnostics() output."""

    def test_contains_renderer_info(self):
        """Output includes webview renderer and GUI backend."""
        buf = io.StringIO()
        mock_webview = MagicMock()
        mock_webview.renderer = 'edgechromium'
        mock_webview.guilib.__name__ = 'webview.platforms.winforms'

        with patch('sys.stdout', buf), \
             patch.dict('sys.modules', {'webview': mock_webview}), \
             patch('usage_monitor_for_claude.verbose.diagnostic_post_init_rows', return_value=[]):
            print_runtime_diagnostics()

        output = buf.getvalue()
        self.assertIn('edgechromium', output)
        self.assertIn('winforms', output)

    def test_platform_post_init_rows_are_rendered(self):
        """Rows supplied by the platform backend reach the output."""
        buf = io.StringIO()
        mock_webview = MagicMock()
        mock_webview.guilib.__name__ = 'webview.platforms.gtk'

        with patch('sys.stdout', buf), \
             patch.dict('sys.modules', {'webview': mock_webview}), \
             patch('usage_monitor_for_claude.verbose.diagnostic_post_init_rows',
                   return_value=[('Toolkit runtime', '3.24.52')]):
            print_runtime_diagnostics()

        self.assertIn('3.24.52', buf.getvalue())

    def test_missing_backend_reported_as_unknown(self):
        """A webview without a resolved GUI backend still prints a row."""
        buf = io.StringIO()
        mock_webview = MagicMock()
        mock_webview.renderer = None
        mock_webview.guilib = None

        with patch('sys.stdout', buf), \
             patch.dict('sys.modules', {'webview': mock_webview}), \
             patch('usage_monitor_for_claude.verbose.diagnostic_post_init_rows', return_value=[]):
            print_runtime_diagnostics()

        self.assertIn('unknown', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
