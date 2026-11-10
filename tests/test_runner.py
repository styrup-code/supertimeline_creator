import datetime
import csv
import importlib.util
import json
import errno
import shutil
import sqlite3
import re
from unittest import mock
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('runner', Path(__file__).resolve().parents[1] / 'runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class RunnerTests(unittest.TestCase):
    def test_plaso_reexport_preserves_database_and_filters_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'case.plaso'
            source.write_bytes(b'original database')
            output = root / 'output'
            output.mkdir()
            window = runner.parse_window('2023-01-01..2023-01-02')
            header = 'date,time,timezone,MACB,source,sourcetype,type,user,host,short,desc,version,filename,inode,notes,format,extra\n'
            event = '01/01/2023,00:00:00,UTC,....,EVT,WinEVTX,Creation Time,-,-,event,event,2,Security.evtx,-,-,winevtx,<EventID>4624</EventID>\n'
            rejected = event.replace('4624', '9999')
            calls = []
            def fake_command(*args):
                calls.append(args)
                if args[0] == 'pinfo.py':
                    Path(args[args.index('-w') + 1]).write_text('original Plaso warnings\n')
                elif args[0] == 'psort.py':
                    Path(args[args.index('-w') + 1]).write_text(header + event + rejected)
                else:
                    self.fail(f'Unexpected ingestion command: {args[0]}')
            selection = (re.compile('Security.evtx', re.I), frozenset(('4624',)))
            with mock.patch.object(runner, 'OUTPUT', output), mock.patch.object(runner, 'command', side_effect=fake_command):
                runner.process(source, None, window, 1, event_filter=selection)
                with self.assertRaises(FileExistsError):
                    runner.process(source, None, window, 1, event_filter=selection)
                runner.process(source, None, None, 1, filters_enabled=False, include_vss=True, include_duplicates=True)
            prefix = 'case-supertimeline-2023-01-01_to_2023-01-02'
            self.assertEqual((output / (prefix + '.csv')).read_text(), header + event)
            self.assertEqual((output / 'case-supertimeline-all-time.csv').read_text(), header + event + rejected)
            self.assertIn('cannot restore excluded events', (output / (prefix + '-plaso-warnings.txt')).read_text())
            self.assertEqual(source.read_bytes(), b'original database')
            self.assertFalse((output / 'plaso').exists())
            psort_calls = [c for c in calls if c[0] == 'psort.py']
            self.assertIn('--slice', psort_calls[0])
            self.assertNotIn('--slice', psort_calls[1])
            self.assertEqual(psort_calls[0][-2], source)

    def test_plaso_selection_and_timezone_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = Path(tmp)
            (inputs / 'case.plaso').touch()
            with mock.patch.object(runner, 'INPUT', inputs), \
                 mock.patch.object(runner, 'resource_plan', return_value=(4, 8 * 1024**3, 1, 1)), \
                 mock.patch.object(runner, 'process') as process, \
                 mock.patch('sys.argv', ['runner.py', '--no-filter']):
                self.assertEqual(runner.main(), 0)
                self.assertEqual(process.call_args.args[0].name, 'case.plaso')
                (inputs / 'image.zip').touch()
                with self.assertRaises(SystemExit):
                    runner.main()
            with mock.patch.object(runner, 'INPUT', inputs), \
                 mock.patch.object(runner, 'resource_plan', return_value=(4, 8 * 1024**3, 1, 1)), \
                 mock.patch.object(runner, 'process') as process, \
                 mock.patch('sys.argv', ['runner.py', '--no-filter', '--input-file=case.plaso']):
                self.assertEqual(runner.main(), 0)
                self.assertEqual(process.call_args.args[0].name, 'case.plaso')
            with mock.patch.object(runner, 'INPUT', inputs), \
                 mock.patch.object(runner, 'resource_plan', return_value=(4, 8 * 1024**3, 1, 1)), \
                 mock.patch.object(runner, 'process') as process, \
                 mock.patch('sys.argv', ['runner.py', '--no-filter', '--timezone=UTC']):
                self.assertEqual(runner.main(), 0)
                self.assertEqual({call.args[0].name for call in process.call_args_list}, {'case.plaso', 'image.zip'})
            with mock.patch.object(runner, 'INPUT', inputs), \
                 mock.patch('sys.argv', ['runner.py', '--input-file=../case.plaso']), \
                 self.assertRaises(SystemExit):
                runner.main()

    def test_plaso_export_failure_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'case.plaso'
            source.write_bytes(b'evidence')
            output = root / 'output'
            output.mkdir()
            def fail_psort(*args):
                if args[0] == 'pinfo.py':
                    Path(args[args.index('-w') + 1]).write_text('warnings')
                else:
                    raise RuntimeError('psort failed')
            with mock.patch.object(runner, 'OUTPUT', output), mock.patch.object(runner, 'command', side_effect=fail_psort), \
                 self.assertRaises(RuntimeError):
                runner.process(source, None, None, 1, filters_enabled=False)
            self.assertEqual(source.read_bytes(), b'evidence')
            self.assertEqual(list(output.iterdir()), [])

    def test_input_batch_selects_supported_images_and_rejects_colliding_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = Path(tmp)
            (inputs / 'zip-host.zip').touch()
            (inputs / 'image-host.VHDX').touch()
            (inputs / 'disk.E01').touch()
            (inputs / 'disk.E02').touch()
            (inputs / 'raw.dd').touch()
            with mock.patch.object(runner, 'INPUT', inputs), \
                 mock.patch.object(runner, 'resource_plan', return_value=(4, 8 * 1024**3, 2, 1)), \
                 mock.patch.object(runner, 'process') as process, \
                 mock.patch('sys.argv', ['runner.py', '--timezone', 'UTC', '--no-filter']):
                self.assertEqual(runner.main(), 0)
                self.assertEqual({call.args[0].name for call in process.call_args_list},
                                 {'zip-host.zip', 'image-host.VHDX', 'disk.E01', 'raw.dd'})
                (inputs / 'zip-host.vhdx').touch()
                with self.assertRaises(SystemExit):
                    runner.main()

    def test_mixed_batch_detects_each_archive_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = root / 'input'
            inputs.mkdir()
            with zipfile.ZipFile(inputs / 'kape.zip', 'w') as z:
                z.writestr('D/$MFT', b'kape-mft')
                z.writestr('D/Windows/file', b'kape-file')
            with zipfile.ZipFile(inputs / 'velo.zip', 'w') as z:
                z.writestr('uploads/ntfs/%5C%5C.%5CE%3A/$MFT', b'velo-mft')
                z.writestr('uploads/auto/E%3A/Windows/file', b'velo-file')
                metadata = [dict(SourceFile='mft', Modified='2021-01-01T00:00:00Z'),
                            dict(SourceFile='file', Modified='2021-01-01T00:00:00Z')]
                uploads = [dict(vfs_path='mft', _Components=['uploads', 'ntfs', '\\\\.\\E:', '$MFT'], file_size=8),
                           dict(vfs_path='file', _Components=['uploads', 'auto', 'E:', 'Windows', 'file'], file_size=9)]
                z.writestr('results/Windows.Triage.Targets%2FAll_Matches_Metadata.json', ''.join(json.dumps(row) + '\n' for row in metadata))
                z.writestr('uploads.json', ''.join(json.dumps(row) + '\n' for row in uploads))
            barrier = threading.Barrier(2)
            found = {}

            def extract(archive, *args, **kwargs):
                dest = root / archive.stem
                dest.mkdir()
                barrier.wait(timeout=10)
                mfts, _ = runner.extract_evidence(archive, dest)
                found[archive.name] = (runner.drive_letter(mfts[0], dest), mfts[0].read_bytes())

            with mock.patch.object(runner, 'INPUT', inputs), \
                 mock.patch.object(runner, 'resource_plan', return_value=(8, 14 * 1024**3, 2, 3)), \
                 mock.patch.object(runner, 'process', side_effect=extract), \
                 mock.patch('sys.argv', ['runner.py', '--timezone', 'UTC', '--no-filter']):
                self.assertEqual(runner.main(), 0)
            self.assertEqual(found, {'kape.zip': ('D', b'kape-mft'), 'velo.zip': ('E', b'velo-mft')})

    def test_inclusive_date_range_and_bad_dates(self):
        self.assertEqual(runner.parse_window('2023-01-01..2023-01-27'),
                         (datetime.datetime(2023, 1, 1, tzinfo=datetime.timezone.utc),
                          datetime.datetime(2023, 1, 28, tzinfo=datetime.timezone.utc)))
        for value in ('2023-02-30..2023-03-01', '2023-02-02..2023-02-01', '2023-1-01..2023-01-02'):
            with self.subTest(value=value), self.assertRaises(Exception):
                runner.parse_window(value)

    def test_kape_selects_drive_and_excludes_collection_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'a.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('CopyLog.csv', 'collection log')
                z.writestr('D/$MFT', 'mft')
                z.writestr('D/Windows/file', 'evidence')
            target = root / 'out'
            target.mkdir()
            mfts, _ = runner.extract_evidence(archive, target)
            self.assertEqual(runner.drive_letter(mfts[0], target), 'D')
            self.assertFalse((target / 'CopyLog.csv').exists())
            self.assertTrue((target / 'D/Windows/file').exists())

    def test_velociraptor_selects_uploads_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'a.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('log.json', 'collection log')
                z.writestr('results/collection.json', 'collection results')
                z.writestr('uploads/ntfs/%5C%5C.%5CD%3A/$MFT', 'mft')
                z.writestr('uploads/auto/D%3A/Windows/file', 'evidence')
                meta = [dict(SourceFile='mft-source', Modified='2001-02-03T04:05:06.1234567Z', LastAccessed='2021-05-11T01:02:03.1234567Z'),
                        dict(SourceFile='file-source', Modified='2022-06-07T01:02:03.1111111Z', LastAccessed='2022-06-08T01:02:03.2222222Z')]
                uploads = [dict(vfs_path='mft-source', _Components=['uploads','ntfs','\\\\.\\D:','$MFT'], file_size=3),
                           dict(vfs_path='file-source', _Components=['uploads','auto','D:','Windows','file'], file_size=8)]
                z.writestr('results/Windows.Triage.Targets%2FAll_Matches_Metadata.json', ''.join(json.dumps(x)+'\n' for x in meta))
                z.writestr('uploads.json', ''.join(json.dumps(x)+'\n' for x in uploads))
            target = root / 'out'
            target.mkdir()
            mfts, report = runner.extract_evidence(archive, target)
            self.assertEqual(runner.drive_letter(mfts[0], target), 'D')
            self.assertIn('matched 2/2', report[0])
            self.assertEqual(mfts[0].stat().st_mtime_ns, runner.iso_timestamp_ns('2001-02-03T04:05:06.1234567Z'))
            self.assertEqual(mfts[0].stat().st_atime_ns, runner.iso_timestamp_ns('2021-05-11T01:02:03.1234567Z'))
            self.assertFalse((target / 'log.json').exists())
            self.assertFalse((target / 'results').exists())
            self.assertTrue((target / 'uploads/auto/D%3A/Windows/file').exists())

    def test_velociraptor_conflicts_and_sparse_vss_are_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'vss.zip'
            metadata = [
                dict(SourceFile='mft', Accessor='ntfs', Modified='2021-01-01T00:00:00Z', LastAccessed='2021-01-02T00:00:00Z'),
                dict(SourceFile='snapshot', Accessor='ntfs_vss', Size=6, Modified='2022-01-01T00:00:00Z', LastAccessed='2022-01-02T00:00:00Z'),
                dict(SourceFile='snapshot', Accessor='ntfs_vss', Size=6, Modified='2022-01-01T00:00:00Z', LastAccessed='2022-01-03T00:00:00Z'),
                dict(SourceFile='snapshot', Accessor='auto', Modified='2023-01-01T00:00:00Z', LastAccessed='2023-01-02T00:00:00Z'),
                dict(SourceFile='unrelated', Modified='2022-01-01T00:00:00Z'),
                dict(SourceFile='unrelated', Modified='2023-01-01T00:00:00Z'),
            ]
            mft = ['uploads', 'ntfs', '\\\\.\\C:', '$MFT']
            snapshot = ['uploads', 'ntfs_vss', '\\\\.\\C:', 'Windows', 'file']
            uploads = [dict(vfs_path='mft', _Components=mft, file_size=3),
                       dict(vfs_path='snapshot', _Components=snapshot, file_size=7),
                       dict(vfs_path='snapshot.idx', _Components=snapshot, Type='idx', file_size=0)]
            index = {'ranges': [dict(file_length=3, length=3),
                                dict(file_offset=3, original_offset=3, length=2),
                                dict(file_offset=3, original_offset=5, file_length=2, length=2)]}
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('uploads/ntfs/%5C%5C.%5CC%3A/$MFT', b'mft')
                z.writestr('uploads/ntfs_vss/%5C%5C.%5CC%3A/Windows/file', b'ABCDE')
                z.writestr('uploads/ntfs_vss/%5C%5C.%5CC%3A/Windows/file.idx', json.dumps(index))
                z.writestr('results/Windows.Triage.Targets%2FAll_Matches_Metadata.json', ''.join(json.dumps(x)+'\n' for x in metadata))
                z.writestr('uploads.json', ''.join(json.dumps(x)+'\n' for x in uploads))
            target = root / 'out'
            target.mkdir()
            _, report = runner.extract_evidence(archive, target)
            restored = target / 'uploads/ntfs_vss/%5C%5C.%5CC%3A/Windows/file'
            self.assertEqual(restored.read_bytes(), b'ABC\0\0DE')
            self.assertFalse(restored.with_name('file.idx').exists())
            self.assertEqual(restored.stat().st_mtime_ns, runner.iso_timestamp_ns('2022-01-01T00:00:00Z'))
            self.assertTrue(any('conflicting LastAccessed' in item and 'not restored' in item for item in report))
            self.assertTrue(any('Metadata observation sizes [6]' in item and 'uploaded source size 7' in item for item in report))
            self.assertTrue(any('sparse files reconstructed: 1' in item for item in report))
            self.assertFalse(any('unrelated' in item for item in report))

    def test_sparse_map_validation_preserves_original_and_reports_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, index = Path(tmp) / 'file', Path(tmp) / 'file.idx'
            path.write_bytes(b'ABCDEF')
            index.write_text(json.dumps({'ranges': [dict(file_length=5, length=3)]}))
            with self.assertRaisesRegex(ValueError, 'Inconsistent sparse range'):
                runner.restore_sparse_file(path, index)
            self.assertEqual(path.read_bytes(), b'ABCDEF')
            self.assertEqual(list(Path(tmp).glob('.sparse-*')), [])
            index.write_text(json.dumps({'ranges': [dict(file_length=3, length=3),
                                                     dict(original_offset=3, file_length=3, length=3),
                                                     dict(file_offset=3, original_offset=6, file_length=3, length=3)]}))
            warnings = runner.restore_sparse_file(path, index)
            self.assertEqual(path.read_bytes(), b'ABCABCDEF')
            self.assertTrue(any('overlapping' in item for item in warnings))

    def test_rejects_zip_path_traversal_and_missing_mft(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'out'
            target.mkdir()
            archive = root / 'bad.zip'
            for name in ('../escape', 'C/../../escape'):
                with zipfile.ZipFile(archive, 'w') as z:
                    z.writestr(name, 'bad')
                with self.subTest(name=name), self.assertRaises(ValueError):
                    runner.extract_evidence(archive, target)
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('C/Windows/file', 'evidence')
            with self.assertRaisesRegex(ValueError, 'No \\$MFT'):
                runner.extract_evidence(archive, target)

    def test_default_window_calendar_months_and_leap_day(self):
        now = datetime.datetime(2024, 5, 31, 15, 30, 45, 123456, tzinfo=datetime.timezone.utc)
        start, end = runner.default_window(now)
        self.assertEqual(start, datetime.datetime(2024, 2, 29, 15, 30, 45, 123456, tzinfo=datetime.timezone.utc))
        self.assertEqual(end, now + datetime.timedelta(microseconds=1))

    def test_time_slice_is_a_superset_including_epoch(self):
        for window in [runner.parse_window('2023-01-01..2023-01-27'),
                       runner.parse_window('1969-12-31..1970-01-01'),
                       runner.default_window(datetime.datetime(2026,10,29,9,12,34,123456,tzinfo=datetime.timezone.utc))]:
            args = runner.slice_arguments(window)
            center = datetime.datetime.fromisoformat(args[1])
            extent = datetime.timedelta(minutes=int(args[3]))
            self.assertLessEqual(center-extent, window[0])
            self.assertGreaterEqual(center+extent, window[1])
            self.assertNotEqual(center.timestamp(), 0)

    def test_offset_timestamp_nanoseconds(self):
        self.assertEqual(runner.iso_timestamp_ns('2024-06-01T14:00:00.123456789+02:00'),
                         runner.iso_timestamp_ns('2024-06-01T12:00:00.123456789Z'))
        with self.assertRaises(ValueError):
            runner.iso_timestamp_ns('2024-06-01T12:00:00')

    def test_kape_metadata_name_and_missing_upload_manifest(self):
        self.assertEqual(runner.velo_metadata_name(['results/Windows.KapeFiles.Targets%2FAll%20File%20Metadata.json']),
                         'results/Windows.KapeFiles.Targets%2FAll%20File%20Metadata.json')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / 'a.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('uploads/auto/C%3A/$MFT', 'mft')
                z.writestr('results/Windows.KapeFiles.Targets%2FAll%20File%20Metadata.json', '{}\n')
            target = root / 'out'
            target.mkdir()
            with self.assertRaisesRegex(ValueError, 'uploads.json is missing'):
                runner.extract_evidence(archive, target)

    @unittest.skipUnless(shutil.which('grep'), 'GNU grep is available inside the Docker image')
    def test_noise_filter_patterns_and_windows_path_conversion(self):
        patterns = Path(__file__).resolve().parents[1] / 'timeline_noise.txt'
        noise = [r'C:\Windows\WinSxS\a', 'c:/Windows/assembly/a', 'odlsent', 'example.odl',
                 'Cache_Data', 'Code Cache', 'Service Worker', 'C:/Packages/a',
                 'C:/WindowsApps/Microsoft/a', 'C:/Program Files/WindowsApps/a',
                 'SystemApps', 'C:/Microsoft Office/a', 'C:/Acrobat Reader DC/a',
                 'keep ($FILE_NAME)']
        with tempfile.TemporaryDirectory() as tmp:
            body, output = Path(tmp) / 'input.body', Path(tmp) / 'output.body'
            entries = [f'0|{n}|1|r|0|0|1|1|1|1|1\n'.encode() for n in noise]
            entries += [b'0|C:/safe/AssemblyLine|1|r|0|0|1|1|1|1|1\n', b'0|C:/safe/Aodl|1|r|0|0|1|1|1|1|1\n']
            body.write_bytes(b''.join(entries))
            total, kept = runner.filter_bodyfile(body, output, patterns)
            self.assertEqual((total, kept), (len(entries), 2))
            self.assertEqual(output.read_bytes(), b''.join(entries[-2:]).replace(b'/', b'\\'))
            body.write_bytes(b''.join(entries[:-2]))
            self.assertEqual(runner.filter_bodyfile(body, output, patterns), (len(noise), 0))
            self.assertEqual(output.read_bytes(), b'')
            total, kept = runner.filter_bodyfile(body, output, patterns, enabled=False)
            self.assertEqual((total, kept), (len(noise), len(noise)))
            self.assertEqual(output.read_bytes(), b''.join(entries[:-2]).replace(b'/', b'\\'))

    def test_event_filter_matches_ids_logs_and_preserves_other_csv_records(self):
        header = ['date', 'time', 'timezone', 'MACB', 'source', 'sourcetype', 'type', 'user', 'host', 'short', 'desc', 'version', 'filename', 'inode', 'notes', 'format', 'extra']
        def event(sourcetype, name, event_id, note=''):
            row = [''] * len(header)
            row[5], row[10], row[12], row[16] = sourcetype, note, name, f'<EventID>{event_id}</EventID>'
            return row
        other = event('Bodyfile', 'file', '9999', 'quoted, value\nsecond line')
        accepted = event('WinEVTX', 'C:/logs/Security.evtx', '4624')
        rejected_id = event('WinEVTX', 'C:/logs/Security.evtx', '9999')
        rejected_log = event('WinEVTX', 'C:/logs/Other.evtx', '4624')
        lower_case = event('winevtx', 'C:/logs/system.evtx', '1')
        partial_id = event('WinEVTX', 'C:/logs/System.evtx', '46240')
        velo_event = event('WinEVTX', 'OS:/evidence/uploads/ntfs_vss/C/Windows/Microsoft-Windows-WMI-Activity%254Operational.evtx', '1')
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / 'input.csv', Path(tmp) / 'output.csv'
            with source.open('w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f); writer.writerow(header)
                writer.writerows([other, accepted, rejected_id, rejected_log, lower_case, partial_id, velo_event])
            counts = runner.filter_timeline_csv(source, output, (re.compile(r'Security\.evtx|System\.evtx|WMI-Activity%4Operational\.evtx', re.I), frozenset(('1', '4624'))))
            with output.open(newline='', encoding='utf-8') as f:
                self.assertEqual(list(csv.reader(f)), [header, accepted, lower_case, velo_event, other])
            self.assertEqual(counts, (7, 3, 3))

    def test_default_event_id_configuration(self):
        root = Path(__file__).resolve().parents[1]
        _, configured = runner.load_event_filter(root / 'event_logs.txt', root / 'event_ids.txt')
        self.assertTrue({'4624', '4625', '4104'}.issubset(configured))

    def test_invalid_event_filter_configuration_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs, ids = Path(tmp) / 'logs.txt', Path(tmp) / 'ids.txt'
            logs.write_text('Security.evtx\n')
            ids.write_text('')
            with self.assertRaisesRegex(ValueError, 'must not be empty'):
                runner.load_event_filter(logs, ids)
            ids.write_text('4624\nnot-a-number\n')
            with self.assertRaisesRegex(ValueError, 'must not be empty or invalid'):
                runner.load_event_filter(logs, ids)
            ids.write_text('4624\n')
            logs.write_text('[\n')
            with self.assertRaisesRegex(ValueError, 'Invalid event log filter regex'):
                runner.load_event_filter(logs, ids)

    def test_publish_fallback_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / 'source', root / 'output'
            source.write_bytes(b'data')
            with mock.patch.object(runner.os, 'link', side_effect=OSError(errno.EPERM, 'unsupported')):
                runner.publish_file(source, target)
            self.assertEqual(target.read_bytes(), b'data')
            with self.assertRaises(FileExistsError):
                runner.publish_file(source, target)
            self.assertEqual(target.read_bytes(), b'data')
            self.assertEqual(list(root.glob('*.partial')), [])
            target2 = root / 'without-fsync'
            with mock.patch.object(runner.os, 'fsync', side_effect=OSError(errno.EINVAL, 'unsupported')):
                runner.publish_file(source, target2)
            self.assertEqual(target2.read_bytes(), b'data')
            target3 = root / 'failed-copy'
            with mock.patch.object(runner.shutil, 'copyfileobj', side_effect=OSError('copy failed')):
                with self.assertRaises(OSError):
                    runner.publish_file(source, target3)
            self.assertFalse(target3.exists())
            self.assertEqual(list(root.glob('*.partial')), [])

    def test_automatic_resources_respect_cpu_and_memory_limits(self):
        def plan(cpu_max, memory_max, affinity=8):
            data = {'/sys/fs/cgroup/cpu.max': cpu_max,
                    '/sys/fs/cgroup/memory.max': str(memory_max),
                    '/proc/meminfo': 'MemTotal: 33554432 kB\n'}
            with mock.patch.object(runner.os, 'sched_getaffinity', return_value=set(range(affinity)), create=True), \
                 mock.patch.object(runner, 'Path', side_effect=lambda p: mock.Mock(read_text=lambda: data[str(p)])):
                return runner.resource_plan(5)
        self.assertEqual(plan('800000 100000', 14 * 1024**3), (8, 14 * 1024**3, 2, 3))
        self.assertEqual(plan('200000 100000', 4 * 1024**3), (2, 4 * 1024**3, 1, 1))
        self.assertEqual(plan('1600000 100000', 32 * 1024**3, 16), (16, 32 * 1024**3, 2, 7))

    def test_skipped_path_warnings_are_counted(self):
        sample = ' Message : unable to process path specification with error: bad\n Message : another warning\n Message : unable to process path specification with error: \n'
        self.assertEqual(runner.skipped_path_count(sample), 2)

    @unittest.skipUnless(shutil.which('grep'), 'GNU grep is available inside the Docker image')
    def test_archive_workflow_preserves_timezones_and_outputs(self):
        patterns = Path(__file__).resolve().parents[1] / 'timeline_noise.txt'
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive, output = root / 'host.zip', root / 'output'
            output.mkdir()
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('C/$MFT', 'evidence')
            calls = []
            bodies = []
            header = 'date,time,timezone,MACB,source,sourcetype,type,user,host,short,desc,version,filename,inode,notes,format,extra\n'
            other = '01/01/2026,00:00:00,UTC,...B,FILE,Bodyfile,Creation Time,-,-,file,file,2,file,-,-,bodyfile,-\n'
            event = '01/01/2026,00:00:00,UTC,....,EVT,WinEVTX,Creation Time,-,-,event,event,2,Security.evtx,-,-,winevtx,<EventID>4624</EventID>\n'
            rejected = event.replace('4624', '9999')
            def fake_command(*args):
                calls.append(args)
                if args[0].endswith('/MFTECmd'):
                    (Path(args[args.index('--body') + 1]) / args[args.index('--bodyf') + 1]).write_bytes(
                        b'0|C:/ok|1|r|0|0|1|1|1|1|1\n'
                        b'0|C:/Windows/WinSxS/noise|2|r|0|0|1|1|1|1|1\n')
                elif args[0] == 'log2timeline.py':
                    if args[args.index('--parsers') + 1] == 'mactime':
                        bodies.append(Path(args[-1]).read_bytes())
                    Path(args[args.index('--storage-file') + 1]).touch()
                elif args[0] == 'pinfo.py':
                    Path(args[args.index('-w') + 1]).write_text('report\n')
                elif args[0] == 'psort.py':
                    Path(args[args.index('-w') + 1]).write_text(header + other + event + rejected)
            real_filter = runner.filter_bodyfile
            selection = (re.compile('Security.evtx', re.I), frozenset(('4624',)))
            with mock.patch.object(runner, 'OUTPUT', output), mock.patch.object(runner, 'command', side_effect=fake_command), \
                 mock.patch.object(runner, 'filter_bodyfile', side_effect=lambda body, dest, **kw: real_filter(body, dest, patterns, **kw)):
                runner.process(archive, 'Europe/Zurich', runner.parse_window('2026-10-23..2026-10-23'), 6, event_filter=selection)
                with self.assertRaises(FileExistsError):
                    runner.process(archive, 'Europe/Zurich', None, 6, event_filter=selection)
                unfiltered_archive = root / 'unfiltered.zip'
                shutil.copyfile(archive, unfiltered_archive)
                runner.process(unfiltered_archive, 'Europe/Zurich', None, 6, filters_enabled=False, include_vss=True, include_duplicates=True)
            imports = [c for c in calls if c[0] == 'log2timeline.py']
            self.assertEqual([c[c.index('--timezone') + 1] for c in imports], ['UTC', 'Europe/Zurich'] * 2)
            self.assertEqual([c[c.index('--parsers') + 1] for c in imports], ['mactime', 'win7,!filestat'] * 2)
            self.assertTrue(all('--vss_stores' not in c for c in imports))
            export = next(c for c in calls if c[0] == 'psort.py')
            self.assertIn('--slice', export)
            self.assertEqual(export[export.index('-o') + 1], 'l2tcsv')
            self.assertEqual((output / 'host-supertimeline.csv').read_text(), header + event + other)
            self.assertEqual((output / 'unfiltered-supertimeline.csv').read_text(), header + other + event + rejected)
            self.assertNotIn(b'WinSxS', bodies[0])
            self.assertIn(b'WinSxS', bodies[1])
            self.assertTrue((output / 'plaso/host.plaso').is_file())
            self.assertIn('kept 1; removed 1', (output / 'host-plaso-warnings.txt').read_text())
            self.assertIn('CSV event-log filter disabled', (output / 'unfiltered-plaso-warnings.txt').read_text())

    def test_publication_rolls_back_prior_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, first = root / 'source', root / 'first'
            source.write_bytes(b'data')
            with self.assertRaises(FileNotFoundError):
                runner.publish_outputs(((source, first), (source, root / 'missing' / 'second')))
            self.assertFalse(first.exists())

    def test_vss_selection_preserves_partition_current_volume_and_export_dedup(self):
        # Exercise the whole runner orchestration while mocking forensic tools.
        for suffix, is_ntfs, enabled, partition, expected in (
                ('.E01', True, False, 2, 'none'),
                ('.E01', True, True, 2, 'all'),
                ('.raw', True, True, None, 'all'),
                ('.E01', False, True, 1, 'none')):
            with self.subTest(suffix=suffix, ntfs=is_ntfs, enabled=enabled), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / ('image' + suffix)
                source.write_bytes(b'unchanged image')
                output = root / 'output'
                output.mkdir()
                calls = []
                def fake_command(*args):
                    calls.append(args)
                    if args[0] == 'log2timeline.py':
                        Path(args[args.index('--storage-file') + 1]).write_bytes(b'database')
                    elif args[0] == 'pinfo.py':
                        Path(args[args.index('-w') + 1]).write_text('warnings\n')
                    elif args[0] == 'psort.py':
                        Path(args[args.index('-w') + 1]).write_text('date,time,timezone,desc\n')
                with mock.patch.object(runner, 'OUTPUT', output), \
                     mock.patch('disk_reader.prepare_disk', return_value=([], [], partition, is_ntfs)), \
                     mock.patch.object(runner, 'command', side_effect=fake_command):
                    runner.process(source, 'UTC', None, 2, filters_enabled=False, include_vss=enabled)
                ingest = next(c for c in calls if c[0] == 'log2timeline.py')
                self.assertEqual(ingest[ingest.index('--vss_stores') + 1], expected)
                self.assertNotIn('--vss_only', ingest)
                self.assertEqual(ingest[-1], source)
                if partition is None:
                    self.assertNotIn('--partitions', ingest)
                else:
                    self.assertEqual(ingest[ingest.index('--partitions') + 1], str(partition))
                if is_ntfs:
                    self.assertEqual(ingest[ingest.index('--parsers') + 1], 'win7,!filestat')
                export = next(c for c in calls if c[0] == 'psort.py')
                self.assertIn('--include-all', export)
                report = (output / 'image-plaso-warnings.txt').read_text()
                self.assertIn('VSS:', report)
                self.assertIn('CSV timestamp and long-description deduplication: 0 input rows', report)
                self.assertEqual(source.read_bytes(), b'unchanged image')

    def test_include_vss_passed_through_batch_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = Path(tmp)
            (inputs / 'image.E01').touch()
            with mock.patch.object(runner, 'INPUT', inputs), \
                 mock.patch.object(runner, 'resource_plan', return_value=(4, 8 * 1024**3, 1, 1)), \
                 mock.patch.object(runner, 'process') as process, \
                 mock.patch('sys.argv', ['runner.py', '--timezone', 'UTC', '--no-filter', '--include-vss']):
                self.assertEqual(runner.main(), 0)
                self.assertTrue(process.call_args.kwargs['include_vss'])

    def test_csv_vss_deduplication_uses_timestamp_and_long_description(self):
        header = ['date', 'time', 'timezone', 'desc', 'filename', 'type']
        rows = [
            ['01/01/2024', '12:00:00', 'UTC', 'quoted, text\nsecond line', 'current', 'a'],
            ['01/01/2024', '12:00:00', 'UTC', 'quoted, text\nsecond line', 'vss1', 'b'],
            ['01/01/2024', '12:00:01', 'UTC', 'quoted, text\nsecond line', 'vss1', 'a'],
            ['01/01/2024', '12:00:00', 'UTC', 'different', 'vss1', 'a'],
            ['01/01/2024', '12:00:00', 'CET', 'quoted, text\nsecond line', 'vss1', 'a'],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / 'source.csv', root / 'output.csv'
            with source.open('w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f); writer.writerow(header); writer.writerows(rows)
            total, kept = runner.deduplicate_timeline_csv(source, output, root)
            with output.open(encoding='utf-8', newline='') as f:
                self.assertEqual(list(csv.reader(f)), [header, rows[0], rows[2], rows[3], rows[4]])
            self.assertEqual((total, kept), (5, 4))

    def test_plaso_reexport_uses_csv_dedup_and_requests_all_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'case.plaso'
            source.write_bytes(b'original')
            output = root / 'out'; output.mkdir()
            window = runner.parse_window('2024-01-01..2024-01-01')
            name = 'case-supertimeline-2024-01-01_to_2024-01-01'
            header = 'date,time,timezone,MACB,source,sourcetype,type,user,host,short,desc,version,filename,inode,notes,format,extra\n'
            row = '01/01/2024,12:00:00,UTC,....,LOG,Other,Written Time,-,-,short,description,2,live,-,-,parser,-\n'
            calls = []
            def fake_command(*args):
                calls.append(args)
                if args[0] == 'pinfo.py':
                    Path(args[args.index('-w') + 1]).write_text('warnings')
                elif args[0] == 'psort.py':
                    Path(args[args.index('-w') + 1]).write_text(header + row + row.replace(',live,', ',vss1,'))
            with mock.patch.object(runner, 'OUTPUT', output), mock.patch.object(runner, 'command', side_effect=fake_command):
                runner.process(source, None, window, 1, filters_enabled=False)
            self.assertIn('--include-all', next(c for c in calls if c[0] == 'psort.py'))
            self.assertEqual((output / (name + '.csv')).read_text(), header + row)
            self.assertIn('kept 1; removed 1', (output / (name + '-plaso-warnings.txt')).read_text())
            self.assertEqual(source.read_bytes(), b'original')
            other_output = root / 'include'; other_output.mkdir()
            with mock.patch.object(runner, 'OUTPUT', other_output), mock.patch.object(runner, 'command', side_effect=fake_command):
                runner.process(source, None, window, 1, filters_enabled=False, include_duplicates=True)
            self.assertEqual((other_output / (name + '.csv')).read_text(),
                             header + row + row.replace(',live,', ',vss1,'))
            self.assertIn('disabled by --include-duplicates',
                          (other_output / (name + '-plaso-warnings.txt')).read_text())
            self.assertTrue(all('--include-all' in c for c in calls if c[0] == 'psort.py'))
            self.assertTrue(all('--slice' in c for c in calls if c[0] == 'psort.py'))
            self.assertEqual(source.read_bytes(), b'original')

    def test_event_filter_runs_before_csv_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = root / 'warnings.txt'; report.write_text('warnings')
            header = 'date,time,timezone,MACB,source,sourcetype,type,user,host,short,desc,version,filename,inode,notes,format,extra\n'
            row = '01/01/2024,12:00:00,UTC,....,EVT,WinEVTX,Creation Time,-,-,event,description,2,Other.evtx,-,-,winevtx,<EventID>4624</EventID>\n'
            def fake_command(*args):
                Path(args[args.index('-w') + 1]).write_text(header + row + row.replace('Other.evtx', 'Security.evtx'))
            with mock.patch.object(runner, 'command', side_effect=fake_command):
                result = runner.export_csv(root / 'case.plaso', root, report, None, True,
                                           (re.compile('Security.evtx', re.I), frozenset({'4624'})))
            self.assertEqual(result.read_text(), header + row.replace('Other.evtx', 'Security.evtx'))
            self.assertIn('1 input rows; kept 1; removed 0', report.read_text())

    def test_csv_deduplication_failure_publishes_no_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'case.plaso'; source.write_bytes(b'original')
            output = root / 'out'; output.mkdir()
            def fake_command(*args):
                Path(args[args.index('-w') + 1]).write_text('data')
            with mock.patch.object(runner, 'OUTPUT', output), \
                 mock.patch.object(runner, 'command', side_effect=fake_command), \
                 mock.patch.object(runner, 'deduplicate_timeline_csv', side_effect=sqlite3.OperationalError('disk full')), \
                 self.assertRaises(sqlite3.OperationalError):
                runner.process(source, None, None, 1, filters_enabled=False)
            self.assertEqual(list(output.iterdir()), [])
            self.assertEqual(source.read_bytes(), b'original')

    def test_include_duplicates_flag_passed_through_batch_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = Path(tmp); (inputs / 'case.plaso').touch()
            with mock.patch.object(runner, 'INPUT', inputs), \
                 mock.patch.object(runner, 'resource_plan', return_value=(4, 8 * 1024**3, 1, 1)), \
                 mock.patch.object(runner, 'process') as process, \
                 mock.patch('sys.argv', ['runner.py', '--no-filter', '--include-duplicates', '--timewindow', '2024-01-01..2024-01-01']):
                self.assertEqual(runner.main(), 0)
                self.assertTrue(process.call_args.kwargs['include_duplicates'])
                self.assertEqual(process.call_args.args[2], runner.parse_window('2024-01-01..2024-01-01'))


if __name__ == '__main__':
    unittest.main()
