import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('launcher', Path(__file__).resolve().parents[1] / 'run.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_single_plaso_file_is_selected_without_timezone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'case file.plaso'
            source.touch()
            args = ['run.py', '--input', str(source), '--output', tmp, '--timewindow', '2023-01-01..2023-01-02']
            with mock.patch.object(launcher.sys, 'argv', args), \
                 mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.main(), 0)
            cmd = run.call_args_list[0].args[0]
            self.assertIn('--input-file=case file.plaso', cmd)
            self.assertNotIn('--timezone', cmd)
            self.assertEqual(run.call_args_list[0].kwargs['env']['INPUT_DIR'], str(root.resolve()))

    def test_direct_supported_files_and_extracted_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('evidence.zip', 'disk.E01', 'disk.dd', 'image.vhdx'):
                source = root / name
                source.touch()
                with self.subTest(name=name), mock.patch.object(launcher.sys, 'argv', ['run.py', '--input', str(source), '--output', tmp]), \
                     mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(launcher.main(), 0)
                    self.assertIn('--input-file=' + name, run.call_args_list[0].args[0])
                    self.assertEqual(run.call_args_list[0].kwargs['env']['INPUT_DIR'], str(root))
            source = root / 'extracted.case'
            (source / 'C').mkdir(parents=True)
            with mock.patch.object(launcher.sys, 'argv', ['run.py', '--input', str(source), '--output', tmp]), \
                 mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.main(), 0)
                self.assertIn('--input-directory=extracted.case', run.call_args_list[0].args[0])
                self.assertEqual(run.call_args_list[0].kwargs['env']['INPUT_DIR'], str(root))

    def test_nested_extracted_folder_is_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'case'
            (source / 'wrapper' / 'C').mkdir(parents=True)
            args = ['run.py', '--input', str(source), '--output', tmp, '--timezone', 'UTC']
            with mock.patch.object(launcher.sys, 'argv', args), \
                 mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.main(), 0)
                self.assertIn('--input-directory=case', run.call_args_list[0].args[0])

    def test_zip_password_mounted_temporarily_without_docker_arguments_or_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'test.zip'
            source.touch()
            seen = {}
            def run(cmd, **kwargs):
                if 'compose' in cmd:
                    override = Path(cmd[cmd.index('run') - 1])
                    config = json.loads(override.read_text())
                    volume = config['services']['supertimeline']['volumes'][0]
                    secret = Path(volume['source'])
                    seen.update(override=override, secret=secret, value=secret.read_text(), volume=volume,
                                cmd=cmd, env=kwargs['env'])
                return mock.Mock(returncode=0)
            args = ['run.py', '--input', str(source), '--output', tmp, '--zip-password', 'special secret']
            with mock.patch.object(launcher.sys, 'argv', args), mock.patch.object(launcher.subprocess, 'run', side_effect=run), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as warning:
                self.assertEqual(launcher.main(), 0)
            self.assertEqual(seen['value'], 'special secret')
            self.assertNotIn('special secret', ' '.join(seen['cmd']))
            self.assertNotIn('special secret', seen['env'].values())
            self.assertTrue(seen['volume']['read_only'])
            self.assertFalse(seen['secret'].exists())
            self.assertFalse(seen['override'].exists())
            self.assertIn('shell history', warning.getvalue())

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

    def test_include_vss_is_opt_in_and_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = ['run.py', '--input', tmp, '--output', tmp, '--timezone', 'UTC']
            for enabled in (False, True):
                with self.subTest(enabled=enabled), \
                     mock.patch.object(launcher.sys, 'argv', base + (['--include-vss'] if enabled else [])), \
                     mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(launcher.main(), 0)
                    self.assertEqual('--include-vss' in run.call_args_list[0].args[0], enabled)

    def test_include_duplicates_flag_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = ['run.py', '--input', tmp, '--output', tmp, '--include-duplicates', '--timewindow', '2024-01-01..2024-01-01']
            with mock.patch.object(launcher.sys, 'argv', args), \
                 mock.patch.object(launcher.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.main(), 0)
            self.assertIn('--include-duplicates', run.call_args_list[0].args[0])
            self.assertEqual(run.call_args_list[0].args[0][run.call_args_list[0].args[0].index('--timewindow') + 1],
                             '2024-01-01..2024-01-01')
