import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('launcher', Path(__file__).resolve().parents[1] / 'run.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_windows_user_mapping(self):
        with mock.patch.object(launcher.sys, 'platform', 'win32'):
            self.assertEqual(launcher.container_user(), '0:0')

    def test_paths_with_spaces_and_argument_passing(self):
        with tempfile.TemporaryDirectory(prefix='timeline test ') as tmp:
            source, target = Path(tmp) / 'input space', Path(tmp) / 'output space'
            source.mkdir()
            target.mkdir()
            args = ['run.py', '--input', str(source), '--output', str(target), '--timezone', 'Europe/Zurich', '--timewindow', '2026-10-01..2026-10-02']
            with mock.patch.object(launcher.sys, 'argv', args), mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.main(), 0)
            cmd = run.call_args_list[0].args[0]
            env = run.call_args_list[0].kwargs['env']
            self.assertEqual(env['INPUT_DIR'], str(source.resolve()))
            self.assertEqual(env['OUTPUT_DIR'], str(target.resolve()))
            self.assertEqual((env['SUPERTIMELINE_CPUS'], env['SUPERTIMELINE_MEMORY']), ('8.0', '14g'))
            self.assertEqual(cmd[cmd.index('--timezone') + 1], 'Europe/Zurich')
            self.assertEqual(cmd[-2:], ['--timewindow', '2026-10-01..2026-10-02'])
            self.assertEqual(cmd[cmd.index('--image-drive-letter') + 1], 'C')
            self.assertEqual(run.call_args_list[1].args[0], ['docker', 'image', 'prune', '--force',
                             '--filter', 'label=com.docker.compose.project=supertimeline_creator'])
            self.assertNotIn('--workers', cmd)
            self.assertNotIn('--jobs', cmd)

    def test_resource_limits_forwarded_and_invalid_limits_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = ['run.py', '--input', tmp, '--output', tmp, '--timezone', 'UTC']
            with mock.patch.object(launcher.sys, 'argv', base + ['--cpus', '2.5', '--memory', '8G']), \
                 mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.main(), 0)
            env = run.call_args_list[0].kwargs['env']
            self.assertEqual((env['SUPERTIMELINE_CPUS'], env['SUPERTIMELINE_MEMORY']), ('2.5', '8g'))
            for option, value in [('--cpus', '0'), ('--cpus', 'nan'), ('--cpus', '-1'),
                                  ('--memory', '0'), ('--memory', 'bad'), ('--memory', '-2g')]:
                with self.subTest(option=option, value=value), \
                     mock.patch.object(launcher.sys, 'argv', base + [option, value]), \
                     contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    launcher.main()

    def test_all_time_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ['run.py', '--input', tmp, '--output', tmp, '--timezone', 'UTC', '--all-time']
            with mock.patch.object(launcher.sys, 'argv', args), mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.main(), 0)
            self.assertIn('--all-time', run.call_args_list[0].args[0])

    def test_no_filter_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ['run.py', '--input', tmp, '--output', tmp, '--timezone', 'UTC', '-nofilter']
            with mock.patch.object(launcher.sys, 'argv', args), mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.main(), 0)
            self.assertIn('--no-filter', run.call_args_list[0].args[0])
