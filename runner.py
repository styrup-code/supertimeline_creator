#!/usr/bin/env python3
"""Turn supported KAPE and Velociraptor collections into Plaso CSVs."""
import argparse
import calendar
import concurrent.futures
from contextlib import closing
import csv
import datetime as dt
import errno
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

INPUT = Path('/input')
OUTPUT = Path('/output')
NOISE = Path('/opt/supertimeline/timeline_noise.txt')
EVENT_LOGS = Path('/opt/supertimeline/event_logs.txt')
EVENT_IDS = Path('/opt/supertimeline/event_ids.txt')
VELO_ACCESSORS = frozenset(('auto', 'ntfs', 'file', 'ntfs_vss'))
_log_context = threading.local()
_log_lock = threading.Lock()


def log(message):
    archive = getattr(_log_context, 'archive', None)
    with _log_lock:
        print(f'[{archive}] {message}' if archive else message, flush=True)


def command(*args):
    log('+ ' + ' '.join(map(str, args)))
    subprocess.run(list(map(str, args)), check=True)


def resource_plan(archive_count):
    """Allocate workers across archives within the container CPU/memory limits."""
    cpus = len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else (os.cpu_count() or 1)
    try:
        quota, period = Path('/sys/fs/cgroup/cpu.max').read_text().split()
        if quota != 'max':
            cpus = min(cpus, max(1, math.floor(int(quota) / int(period))))
    except (OSError, ValueError, ZeroDivisionError):
        try:
            quota = int(Path('/sys/fs/cgroup/cpu/cpu.cfs_quota_us').read_text())
            period = int(Path('/sys/fs/cgroup/cpu/cpu.cfs_period_us').read_text())
            if quota > 0:
                cpus = min(cpus, max(1, math.floor(quota / period)))
        except (OSError, ValueError, ZeroDivisionError):
            pass
    cpus = max(1, cpus)
    memory = None
    try:
        value = Path('/sys/fs/cgroup/memory.max').read_text().strip()
        if value != 'max':
            memory = int(value)
    except (OSError, ValueError):
        try:
            memory = int(Path('/sys/fs/cgroup/memory/memory.limit_in_bytes').read_text())
        except (OSError, ValueError):
            pass
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemTotal:'):
                host_memory = int(line.split()[1]) * 1024
                memory = min(memory, host_memory) if memory is not None else host_memory
                break
    except (OSError, ValueError, IndexError):
        pass
    if memory is None:
        memory = 14 * 1024**3
    jobs = min(archive_count, 2, max(1, cpus // 2), max(1, memory // (6 * 1024**3)))
    memory_workers = max(1, (memory - jobs * 2 * 1024**3) // (jobs * (1536 * 1024**2)))
    workers = max(1, min(max(1, (cpus - jobs) // jobs), memory_workers))
    return int(cpus), int(memory), int(jobs), int(workers)


def filter_bodyfile(body, output, patterns=NOISE, enabled=True):
    """Apply the supplied GNU BRE patterns before converting paths to Windows style."""
    if not enabled:
        total = 0
        with body.open('rb') as source, output.open('wb') as dest:
            for line in source:
                total += 1
                dest.write(line.replace(b'/', b'\\'))
        return total, total
    slash_body = output.with_suffix('.slashes')
    total = 0
    with body.open('rb') as source, slash_body.open('wb') as dest:
        for line in source:
            total += 1
            dest.write(line.replace(b'\\', b'/'))
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    kept = 0
    with output.open('wb') as dest, subprocess.Popen(
            ['grep', '-a', '-v', '-i', '-f', str(patterns), str(slash_body)],
            stdout=subprocess.PIPE, env=env) as grep:
        for line in grep.stdout:
            kept += 1
            dest.write(line.replace(b'/', b'\\'))
        status = grep.wait()
    slash_body.unlink()
    if status not in (0, 1):
        raise RuntimeError(f'Noise filter failed with exit code {status}')
    return total, kept


def load_event_filter(logs_path=EVENT_LOGS, ids_path=EVENT_IDS):
    """Read the case-insensitive log regexes and exact event IDs from configuration."""
    logs = [line.strip() for line in logs_path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    ids = [line.strip() for line in ids_path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if not logs or not ids or any(not re.fullmatch(r'[0-9]+', value) for value in ids):
        raise ValueError('Event log and numeric event ID filter lists must not be empty or invalid')
    try:
        return re.compile('|'.join(logs), re.IGNORECASE), frozenset(ids)
    except re.error as exc:
        raise ValueError(f'Invalid event log filter regex: {exc}') from exc


def filter_timeline_csv(source_path, output_path, event_filter):
    """Keep matching EVTX rows first, then all other rows."""
    log_regex, event_ids = event_filter
    event_id_regex = re.compile(r'>([0-9]+)</EventID>', re.IGNORECASE)
    csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
    total = kept_events = removed_events = 0
    with source_path.open('r', encoding='utf-8-sig', newline='') as source, \
         output_path.open('w', encoding='utf-8', newline='') as output, \
         tempfile.TemporaryFile(mode='w+', encoding='utf-8', newline='') as other:
        reader = csv.reader(source, strict=True)
        writer, other_writer = csv.writer(output, lineterminator='\n'), csv.writer(other, lineterminator='\n')
        header = next(reader, None)
        if not header or len(header) < 6 or header[5].casefold() != 'sourcetype' or 'filename' not in header:
            raise ValueError('Unexpected supertimeline CSV header')
        filename_index = header.index('filename')
        writer.writerow(header)
        for row in reader:
            if len(row) != len(header):
                raise ValueError(f'Malformed supertimeline CSV record near line {reader.line_num}')
            total += 1
            if row[5].casefold() != 'winevtx':
                other_writer.writerow(row)
                continue
            # Match the whole CSV record, not only the filename or XML.
            record = ','.join(row)
            filename = row[filename_index]
            velo_path = '/uploads/' in filename.replace('\\', '/').casefold()
            matches_log = log_regex.search(record) or (velo_path and log_regex.search(urllib.parse.unquote(filename)))
            if matches_log and any(m.group(1) in event_ids for m in event_id_regex.finditer(record)):
                writer.writerow(row)
                kept_events += 1
            else:
                removed_events += 1
        other.seek(0)
        shutil.copyfileobj(other, output, 1024 * 1024)
    return total, kept_events, removed_events


def deduplicate_timeline_csv(source_path, output_path, work):
    """Keep the first row for each exact displayed timestamp and description."""
    csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
    total = kept = 0
    with closing(sqlite3.connect(str(work / 'csv-dedup.sqlite'))) as database:
        database.execute('PRAGMA journal_mode=OFF')
        database.execute('PRAGMA synchronous=OFF')
        database.execute('PRAGMA cache_size=-65536')
        database.execute('CREATE TABLE seen (date TEXT NOT NULL, time TEXT NOT NULL, '
                         'timezone TEXT NOT NULL, description TEXT NOT NULL, '
                         'PRIMARY KEY (date, time, timezone, description)) WITHOUT ROWID')
        with source_path.open('r', encoding='utf-8-sig', newline='') as source, \
             output_path.open('w', encoding='utf-8', newline='') as output:
            reader = csv.reader(source, strict=True)
            writer = csv.writer(output, lineterminator='\n')
            header = next(reader, None)
            required = ('date', 'time', 'timezone', 'desc')
            if not header or any(header.count(name) != 1 for name in required):
                raise ValueError('Unexpected supertimeline CSV header for deduplication')
            indexes = [header.index(name) for name in required]
            writer.writerow(header)
            for row in reader:
                if len(row) != len(header):
                    raise ValueError(f'Malformed supertimeline CSV record near line {reader.line_num}')
                total += 1
                result = database.execute('INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?)',
                                          tuple(row[index] for index in indexes))
                if result.rowcount:
                    writer.writerow(row)
                    kept += 1
                if total % 100000 == 0:
                    database.commit()
                    log(f'CSV deduplication: processed {total:,} rows; kept {kept:,}')
    return total, kept


def publish_file(source, destination):
    """Copy to the target filesystem then publish without replacing an existing file."""
    stage = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.' + destination.name + '.', suffix='.partial', dir=destination.parent, delete=False) as tmp:
            stage = Path(tmp.name)
            with source.open('rb') as src:
                shutil.copyfileobj(src, tmp, 4 * 1024 * 1024)
            tmp.flush()
            try:
                os.fsync(tmp.fileno())
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS):
                    raise
                log(f'WARNING: destination does not support fsync: {destination.parent}')
    except BaseException:
        if stage is not None:
            stage.unlink(missing_ok=True)
        raise
    try:
        try:
            os.link(stage, destination)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EACCES, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.ENOSYS):
                raise
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            try:
                os.replace(stage, destination)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
    finally:
        stage.unlink(missing_ok=True)


def parse_window(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}', value):
        raise argparse.ArgumentTypeError('Expected YYYY-MM-DD..YYYY-MM-DD')
    try:
        start, end = (dt.date.fromisoformat(x) for x in value.split('..'))
        if end < start:
            raise ValueError('end date is before start date')
        return (dt.datetime.combine(start, dt.time(), dt.timezone.utc),
                dt.datetime.combine(end + dt.timedelta(days=1), dt.time(), dt.timezone.utc))
    except (ValueError, OverflowError) as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def default_window(now):
    """Return a rolling three-calendar-month interval ending at the given instant."""
    now = now.astimezone(dt.timezone.utc)
    month_index = now.year * 12 + now.month - 1 - 3
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    start = now.replace(year=year, month=month, day=day)
    return start, now + dt.timedelta(microseconds=1)


def iso_timestamp_ns(value):
    """Parse an offset-bearing ISO timestamp without losing sub-microsecond precision."""
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)', value)
    if not match:
        raise ValueError(f'Invalid or timezone-free metadata timestamp: {value!r}')
    date = dt.datetime.fromisoformat(match.group(1) + ('+00:00' if match.group(3) == 'Z' else match.group(3)))
    delta = date.astimezone(dt.timezone.utc) - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int((match.group(2) or '').ljust(9, '0') or '0')


def velo_metadata_name(names):
    candidates = []
    for name in names:
        parts = tuple(urllib.parse.unquote(p) for p in name.split('/'))
        if len(parts) != 2 or parts[0].lower() != 'results':
            continue
        normalized = parts[1].lower().replace('_', ' ')
        prefix, sep, leaf = normalized.partition('/')
        if sep and prefix in ('windows.triage.targets', 'windows.kapefiles.targets') and leaf in ('all matches metadata.json', 'all file metadata.json'):
            candidates.append(name)
    if len(candidates) != 1:
        raise ValueError(f'Expected one Velociraptor triage metadata JSONL file; found {len(candidates)}')
    return candidates[0]


def metadata_size_warning(rows, uploaded_size, filename):
    """Identify observations whose size differs from the later upload."""
    sizes = {row['Size'] for row in rows if type(row.get('Size')) is int and row['Size'] >= 0}
    if type(uploaded_size) is int and sizes and sizes != {uploaded_size}:
        return (f'WARNING: Metadata observation sizes {sorted(sizes)} differ from '
                f'uploaded source size {uploaded_size}; timestamps may describe an earlier '
                f'observation: {filename}')
    return None


def normalize_velociraptor(z, selected, dest):
    """Restore mtime and atime by joining uploads.json to collection metadata."""
    metadata_name = velo_metadata_name(z.namelist())
    if 'uploads.json' not in z.namelist():
        raise ValueError('Velociraptor uploads.json is missing; cannot restore timestamps')
    metadata = {}
    with z.open(metadata_name) as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            key = row.get('SourceFile')
            if not key:
                continue
            metadata.setdefault(key, []).append(row)
    by_components = {}
    for info, parts in selected:
        if info.is_dir():
            continue
        decoded = tuple(urllib.parse.unquote(p) for p in parts)
        if decoded in by_components:
            raise ValueError(f'Ambiguous decoded Velociraptor upload path: {decoded!r}')
        by_components[decoded] = (info, parts)
    matched = set()
    restored_count = 0
    details = []
    with z.open('uploads.json') as uploads:
        for line in uploads:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get('Type') == 'idx':
                continue
            components = tuple(row.get('_Components') or ())
            entry = by_components.get(components)
            if not entry:
                continue
            if components in matched:
                raise ValueError(f'Duplicate upload metadata for {components!r}')
            matched.add(components)
            info, parts = entry
            source = row.get('vfs_path')
            candidates = metadata.get(source, [])
            accessor = components[1]
            matching = [r for r in candidates if r.get('Accessor') == accessor]
            # Older collectors may omit the Accessor field altogether.
            if not matching:
                matching = [r for r in candidates if not r.get('Accessor')]
            if not matching:
                details.append(f'WARNING: Missing modification/access metadata for {info.filename}')
                continue
            reported = row.get('file_size')
            size_warning = metadata_size_warning(matching, reported, info.filename)
            if size_warning:
                details.append(size_warning)
            path = dest.joinpath(*parts)
            current = path.stat()
            times = []
            fully_restored = True
            for field, previous in (('Modified', current.st_mtime_ns), ('LastAccessed', current.st_atime_ns)):
                values = [row.get(field) for row in matching if row.get(field)]
                try:
                    parsed = {iso_timestamp_ns(value) for value in values}
                except (TypeError, ValueError):
                    parsed = set()
                if len(parsed) == 1 and len(values) == len(matching):
                    times.append(parsed.pop())
                else:
                    fully_restored = False
                    reason = 'conflicting' if len(parsed) > 1 else 'missing or invalid'
                    details.append(f'WARNING: {reason} {field} metadata for {info.filename}; '
                                   f'not restored (observed values: {sorted(set(map(str, values)))})')
                    times.append(previous)
            modified, accessed = times
            os.utime(path, ns=(accessed, modified))
            if fully_restored:
                restored_count += 1
            restored = path.stat()
            if restored.st_mtime_ns != modified or restored.st_atime_ns != accessed:
                details.append(f'WARNING: Filesystem could not represent exact timestamps for {info.filename}')
            if isinstance(reported, int) and reported != restored.st_size:
                details.append(f'WARNING: Stored size {restored.st_size} differs from reported source size {reported}: {info.filename}')
    missing = set(by_components) - matched
    for components in sorted(missing):
        details.append(f'WARNING: No uploads.json mapping for {by_components[components][0].filename}')
    if not matched:
        raise ValueError('Velociraptor uploads.json did not match any selected upload')
    details.insert(0, f'Velociraptor timestamp metadata: {metadata_name}; matched {len(matched)}/{len(by_components)} uploads; restored {restored_count}; warnings {len(details)}')
    for message in details:
        log(message)
    return details


def member_path(info):
    name = info.filename
    if '\\' in name or '\x00' in name or name.startswith('/'):
        raise ValueError(f'Unsafe ZIP member: {name!r}')
    parts = PurePosixPath(name).parts
    if not parts or any(p in ('.', '..') for p in parts):
        raise ValueError(f'Unsafe ZIP member: {name!r}')
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
        raise ValueError(f'Unsupported ZIP member type: {name!r}')
    return parts


def sparse_uploads(z, selected):
    """Identify Velociraptor index sidecars using the upload manifest."""
    entries = {}
    for info, parts in selected:
        if info.is_dir():
            continue
        decoded = tuple(urllib.parse.unquote(p) for p in parts)
        if decoded in entries:
            raise ValueError(f'Ambiguous decoded Velociraptor upload path: {decoded!r}')
        entries[decoded] = parts
    indexes = {}
    with z.open('uploads.json') as manifest:
        for line in manifest:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get('Type') != 'idx':
                continue
            components = tuple(row.get('_Components') or ())
            if len(components) < 3 or components[0] != 'uploads' or components[1] not in VELO_ACCESSORS:
                continue
            index_components = components[:-1] + (components[-1] + '.idx',)
            if components not in entries or index_components not in entries:
                raise ValueError(f'Missing sparse file or index for {components!r}')
            if components in indexes:
                raise ValueError(f'Duplicate sparse index for {components!r}')
            indexes[components] = (entries[components], entries[index_components])
    return list(indexes.values())


def restore_sparse_file(path, index_path, label=None):
    """Reconstruct a collector sparse file using its original offsets."""
    index = json.loads(index_path.read_text(encoding='utf-8'))
    if not isinstance(index, dict) or not isinstance(index.get('ranges'), list):
        raise ValueError(f'Invalid sparse index: {index_path}')
    packed_size = path.stat().st_size
    logical_end = 0
    packed_ranges = []
    stage = None
    try:
        with path.open('rb') as source, tempfile.NamedTemporaryFile(dir=path.parent, prefix='.sparse-', delete=False) as target:
            stage = Path(target.name)
            for entry in index['ranges']:
                if not isinstance(entry, dict):
                    raise ValueError(f'Invalid sparse range in {index_path}')
                values = [entry.get(key, 0) for key in ('file_offset', 'original_offset', 'file_length', 'length')]
                if any(type(value) is not int or value < 0 for value in values):
                    raise ValueError(f'Invalid sparse range values in {index_path}')
                file_offset, offset, file_length, length = values
                if (offset < logical_end or offset + length > (1 << 63) - 1 or
                        file_length not in (0, length) or file_offset + file_length > packed_size):
                    raise ValueError(f'Inconsistent sparse range in {index_path}')
                if file_length:
                    packed_ranges.append((file_offset, file_offset + file_length))
                    source.seek(file_offset)
                    target.seek(offset)
                    remaining = file_length
                    while remaining:
                        chunk = source.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise ValueError(f'Truncated sparse data for {index_path}')
                        target.write(chunk)
                        remaining -= len(chunk)
                logical_end = offset + length
            target.truncate(logical_end)
        # Overlapping or unreferenced packed bytes do not prevent following the
        # index, but are important evidence of unusual collection data.
        covered = 0
        overlap = False
        for start, end in sorted(packed_ranges):
            if start < covered:
                overlap = True
            if start > covered:
                break
            covered = max(covered, end)
        warnings = []
        if overlap or covered != packed_size:
            warnings.append(f'WARNING: Sparse index has overlapping or unreferenced packed bytes; '
                            f'offsets were applied as recorded: {label or index_path.name}')
        os.replace(stage, path)
        stage = None
        return warnings
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


def extract_evidence(archive, dest):
    with zipfile.ZipFile(archive) as z:
        entries = [(info, member_path(info)) for info in z.infolist()]
        has_velo = any(len(p) >= 3 and p[0] == 'uploads' and p[1] in VELO_ACCESSORS for _, p in entries)
        if has_velo:
            selected = [(i, p) for i, p in entries if len(p) >= 3 and p[0] == 'uploads' and p[1] in VELO_ACCESSORS]
            layout = 'Velociraptor uploads (auto/ntfs/file/ntfs_vss)'
        else:
            selected = [(i, p) for i, p in entries if len(p) >= 2 and re.fullmatch(r'[A-Za-z]', p[0])]
            layout = 'KAPE drive folders'
        if not selected:
            raise ValueError('No supported KAPE drive folders or Velociraptor uploads found')
        seen = set()
        for info, parts in selected:
            key = '/'.join(parts)
            if key in seen and not info.is_dir():
                raise ValueError(f'Duplicate ZIP member: {key!r}')
            seen.add(key)
        indexes = sparse_uploads(z, selected) if has_velo and 'uploads.json' in z.namelist() else []
        # 7-Zip supports Deflate64 (ZIP method 9), used by some KAPE
        # archives. All members have been checked before invoking it.
        if any(i.compress_type == 9 for i, _ in selected):
            roots = sorted({'/'.join(p[:2]) if has_velo else p[0] for _, p in selected})
            includes = ['-i!' + root + '/*' for root in roots]
            command('7z', 'x', '-y', '-bd', '-bso0', '-bsp0', '-o' + str(dest), *includes, archive)
        else:
            for info, parts in selected:
                target = dest.joinpath(*parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(info) as src, target.open('xb') as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
        sparse_warnings = []
        for data_parts, index_parts in indexes:
            sparse_warnings.extend(restore_sparse_file(dest.joinpath(*data_parts), dest.joinpath(*index_parts), '/'.join(index_parts)))
            dest.joinpath(*index_parts).unlink()
        index_paths = {index_parts for _, index_parts in indexes}
        selected = [(i, p) for i, p in selected if p not in index_paths]
        normalization = normalize_velociraptor(z, selected, dest) if has_velo else []
        if indexes:
            summary = f'Velociraptor sparse files reconstructed: {len(indexes)}'
            log(summary)
            normalization.append(summary)
        for warning in sparse_warnings:
            log(warning)
            normalization.append(warning)
        mfts = [dest.joinpath(*p) for i, p in selected if not i.is_dir() and p[-1].lower() == '$mft']
        log(f'Layout: {layout}; extracted {sum(not i.is_dir() for i, _ in selected)} files; MFTs: {len(mfts)}')
        if not mfts:
            raise ValueError('No $MFT was found in the selected evidence; cannot create the requested MFT timeline')
        return mfts, normalization


def drive_letter(mft, root):
    parts = mft.relative_to(root).parts
    if len(parts) == 2 and re.fullmatch('[A-Za-z]', parts[0]):
        return parts[0].upper()
    if len(parts) >= 4 and parts[0] == 'uploads':
        volume = urllib.parse.unquote(parts[2])
        match = re.search(r'([A-Za-z]):(?:[\\/]|$)', volume)
        if match:
            return match.group(1).upper()
    raise ValueError(f'Cannot determine volume drive letter for {mft.relative_to(root)}')


def make_filter(window):
    expression = "(data_type != 'windows:evtx:record' or timestamp_desc == 'Creation Time')"
    if window:
        start, end = window
        expression += f" and date >= DATETIME('{start.strftime('%Y-%m-%dT%H:%M:%S.%f')}') and date < DATETIME('{end.strftime('%Y-%m-%dT%H:%M:%S.%f')}')"
    return expression


def slice_arguments(window):
    """Use a superset time slice for SQLite preselection; the filter remains exact."""
    if not window:
        return []
    start, end = window
    center = (start + (end - start) / 2).replace(microsecond=0)
    if center.timestamp() == 0:
        center += dt.timedelta(seconds=1)
    span = max(center - start, end - center)
    minutes = (span.days * 86400 + span.seconds + 59) // 60 + 1
    return ['--slice', center.isoformat(timespec='seconds'), '--slice-size', str(minutes)]


def skipped_path_count(warnings):
    return len(re.findall(r'(?m)^\s*Message\s*:\s*unable to process path specification with error:', warnings, re.IGNORECASE))


def import_mfts(mfts, evidence, work, storage, common, filters_enabled=True):
    """Generate, filter and import each volume's MFT bodyfile."""
    summaries, logs = [], []
    for index, mft in enumerate(mfts):
        drive = drive_letter(mft, evidence)
        body = work / f'mft-{index}.body'
        log(f'Processing MFT: {mft.relative_to(evidence)} (drive {drive}:)')
        command('/opt/mftecmd/MFTECmd', '-f', mft, '--body', work, '--bodyf', body.name, '--blf', '--bdl', drive)
        if not body.is_file() or body.stat().st_size == 0:
            raise ValueError(f'MFTECmd did not create a nonempty bodyfile for {mft}')
        normalized = work / f'mft-{index}-windows.body'
        if filters_enabled:
            total, kept = filter_bodyfile(body, normalized)
        else:
            total, kept = filter_bodyfile(body, normalized, enabled=False)
        body.unlink()
        summary = f'{mft.relative_to(evidence)}: {total} bodyfile lines; kept {kept}; removed {total - kept}'
        if not filters_enabled:
            summary += ' (MFT noise filter disabled)'
        summaries.append(summary)
        log('MFT bodyfile: ' + summary)
        if not kept:
            log(f'All bodyfile entries were filtered for {mft.relative_to(evidence)}')
            continue
        log_file = work / f'mft-{index}-plaso.log'
        logs.append(log_file)
        # Bodyfile timestamps are Unix timestamps; Plaso otherwise treats them as local.
        command('log2timeline.py', *common, '--log-file', log_file, '--timezone', 'UTC', '--parsers', 'mactime', '--storage-file', storage, normalized)
        normalized.unlink()
    return summaries, logs


def create_warning_report(archive, storage, warning_file, summaries, normalization, logs):
    command('pinfo.py', '--sections', 'warnings', '-w', warning_file, storage)
    skipped = skipped_path_count(warning_file.read_text(encoding='utf-8', errors='replace'))
    if skipped:
        log(f'WARNING: Plaso could not process {skipped} paths in {archive.name}; review its warning report')
    with warning_file.open('a', encoding='utf-8') as report_file:
        report_file.write('\n\n===== MFT bodyfile noise filter =====\n' + '\n'.join(summaries) + '\n')
        if normalization:
            report_file.write('\n\n===== Evidence extraction and timestamp normalization =====\n' + '\n'.join(normalization) + '\n')
        for log_file in logs:
            report_file.write(f'\n\n===== Plaso processing log: {log_file.name} =====\n')
            if log_file.exists():
                report_file.write(log_file.read_text(encoding='utf-8', errors='replace'))


def publish_outputs(pairs):
    """Publish all outputs, rolling back this archive if publication fails."""
    published = []
    try:
        for source, target in pairs:
            publish_file(source, target)
            published.append(target)
    except BaseException:
        for target in published:
            target.unlink(missing_ok=True)
        raise


def export_name(source, window):
    """Keep image output names stable and distinguish Plaso re-export windows."""
    name = source.stem + '-supertimeline'
    if source.suffix.lower() != '.plaso':
        return name
    if window is None:
        return name + '-all-time'
    start, end = window
    if start.time() == dt.time() and end.time() == dt.time():
        return name + f'-{start:%Y-%m-%d}_to_{end - dt.timedelta(days=1):%Y-%m-%d}'
    return name + f'-{start:%Y%m%dT%H%M%S%fZ}_to_before_{end:%Y%m%dT%H%M%S%fZ}'


def export_csv(storage, work, warning_file, window, filters_enabled, event_filter, include_duplicates=False):
    """Export and filter a CSV from a Plaso storage file."""
    csv_path = work / 'timeline.csv'
    # Request all events before either applying the CSV rule or keeping duplicates.
    command('psort.py', '--unattended', '--status-view', 'linear', '--status-view-interval', '10', '--output-time-zone', 'UTC', '--include-all', *slice_arguments(window), '-o', 'l2tcsv', '-w', csv_path, storage, make_filter(window))
    if not csv_path.is_file() or csv_path.stat().st_size == 0:
        raise ValueError('psort did not create a CSV')
    if filters_enabled:
        filtered = work / 'timeline-filtered.csv'
        total, kept, removed = filter_timeline_csv(csv_path, filtered, event_filter)
        csv_path.unlink()
        csv_path = filtered
        filter_summary = f'CSV event-log filter: {total} input rows; kept {kept} WinEVTX rows; removed {removed} WinEVTX rows'
    else:
        filter_summary = 'CSV event-log filter disabled'
    log(filter_summary)
    if not include_duplicates:
        deduplicated = work / 'timeline-deduplicated.csv'
        total, kept = deduplicate_timeline_csv(csv_path, deduplicated, work)
        csv_path.unlink()
        csv_path = deduplicated
        dedup_summary = f'CSV timestamp and long-description deduplication: {total} input rows; kept {kept}; removed {total - kept}'
    else:
        dedup_summary = 'CSV timestamp and long-description deduplication disabled by --include-duplicates; psort --include-all requested'
    log(dedup_summary)
    with warning_file.open('a', encoding='utf-8') as report_file:
        report_file.write('\n\n===== CSV deduplication =====\n' + dedup_summary + '\n')
        report_file.write('\n\n===== CSV event-log filter =====\n' + filter_summary + '\n')
    return csv_path


def process_plaso(source, window, filters_enabled, event_filter, include_duplicates=False):
    """Re-export an existing database directly from the read-only input mount."""
    name = export_name(source, window)
    output = OUTPUT / (name + '.csv')
    report = OUTPUT / (name + '-plaso-warnings.txt')
    for target in (output, report):
        if target.exists():
            raise FileExistsError(f'Output already exists: {target}')
    with tempfile.TemporaryDirectory(prefix='supertimeline-') as temp:
        work = Path(temp)
        warning_file = work / 'warnings.txt'
        create_warning_report(source, source, warning_file,
                              ['MFT filtering was determined when this database was created; re-export cannot restore excluded events.'], [], [])
        csv_path = export_csv(source, work, warning_file, window, filters_enabled, event_filter, include_duplicates)
        publish_outputs(((warning_file, report), (csv_path, output)))
        for target in (output, report):
            log(f'Created {target}')


def process(archive, timezone, window, workers, filters_enabled=True, event_filter=None, image_drive_letter='C', include_vss=False, include_duplicates=False):
    disk_image = archive.suffix.lower() in ('.e01', '.dd', '.img', '.raw')
    if include_vss and not disk_image:
        log('--include-vss applies only to E01/raw disk images; this input uses its existing processing path')
    if filters_enabled and event_filter is None:
        event_filter = load_event_filter()
    if archive.suffix.lower() == '.plaso':
        return process_plaso(archive, window, filters_enabled, event_filter, include_duplicates)
    output = OUTPUT / (archive.stem + '-supertimeline.csv')
    report = OUTPUT / (archive.stem + '-plaso-warnings.txt')
    plaso_dir = OUTPUT / 'plaso'
    plaso_dir.mkdir(exist_ok=True)
    plaso_output = plaso_dir / (archive.stem + '.plaso')
    for target in (output, report, plaso_output):
        if target.exists():
            raise FileExistsError(f'Output already exists: {target}')
    with tempfile.TemporaryDirectory(prefix='supertimeline-') as temp:
        work = Path(temp)
        evidence = work / 'evidence'
        evidence.mkdir()
        partition, is_ntfs = None, True
        if disk_image:
            from disk_reader import prepare_disk
            mfts, normalization, partition, is_ntfs = prepare_disk(archive, evidence, image_drive_letter, log)
        elif archive.suffix.lower() == '.vhdx':
            from vhdx_reader import extract_vhdx
            mfts, normalization = extract_vhdx(archive, evidence, log)
        else:
            mfts, normalization = extract_evidence(archive, evidence)
        storage = work / 'timeline.plaso'
        # Docker enforces the aggregate cap. Plaso's default 4 GiB per-process
        # address-space limit caused silent per-file failures with six workers.
        common = ['--unattended', '--status-view', 'linear', '--status-view-interval', '10', '--workers', str(workers), '--worker-memory-limit', '0', '--process-memory-limit', '0', '--temporary-directory', str(work)]
        summaries, logs = import_mfts(mfts, evidence, work, storage, common, filters_enabled)
        log_file = work / 'artifacts-plaso.log'
        logs.append(log_file)
        parser_options = ['--parsers', 'win7,!filestat'] if is_ntfs else []
        partition_options = ['--partitions', str(partition)] if partition is not None else []
        if disk_image:
            use_vss = include_vss and is_ntfs
            partition_options.extend(['--vss_stores', 'all' if use_vss else 'none'])
            if use_vss:
                vss_summary = ('VSS: all available snapshots requested on the selected NTFS filesystem; '
                               'current volume included. '
                               'The MFTECmd bodyfile covers the current volume only; '
                               'snapshot artifacts use win7,!filestat.')
            elif include_vss:
                vss_summary = 'VSS: --include-vss has no effect on the selected non-NTFS filesystem'
            else:
                vss_summary = 'VSS: excluded (use --include-vss to include snapshots on NTFS E01/raw images)'
            log(vss_summary)
            normalization.append(vss_summary)
        source = archive if disk_image else evidence
        command('log2timeline.py', *common, '--log-file', log_file, '--timezone', timezone, *parser_options, *partition_options, '--storage-file', storage, source)
        warning_file = work / 'warnings.txt'
        create_warning_report(archive, storage, warning_file, summaries, normalization, logs)
        csv_path = export_csv(storage, work, warning_file, window, filters_enabled, event_filter, include_duplicates)
        publish_outputs(((storage, plaso_output), (warning_file, report), (csv_path, output)))
        for target in (output, report, plaso_output):
            log(f'Created {target}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timezone', help='IANA timezone of timestamps without timezone information (required for images)')
    parser.add_argument('--input-file', help=argparse.SUPPRESS)
    parser.add_argument('--no-filter', '-nofilter', action='store_true', help='Disable MFT noise and CSV event-log filters')
    parser.add_argument('--include-vss', action='store_true',
                        help='Include available VSS snapshots in NTFS E01/raw images')
    parser.add_argument('--include-duplicates', action='store_true',
                        help='Include duplicate CSV rows instead of deduplicating by timestamp and long description')
    parser.add_argument('--image-drive-letter', type=str.upper, choices=list('ABCDEFGHIJKLMNOPQRSTUVWXYZ'), default='C',
                        help='Drive letter for NTFS MFT paths from E01/raw images (default: C)')
    windows = parser.add_mutually_exclusive_group()
    windows.add_argument('--timewindow', type=parse_window, help='Inclusive UTC dates, YYYY-MM-DD..YYYY-MM-DD')
    windows.add_argument('--all-time', action='store_true', help='Export all timestamps, including undated events')
    args = parser.parse_args()
    if args.timezone:
        try:
            ZoneInfo(args.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            parser.error(f'Unknown IANA timezone: {args.timezone}')
    window = None if args.all_time else (args.timewindow or default_window(dt.datetime.now(dt.timezone.utc)))
    log('Export window: ' + (f'{window[0].isoformat()} <= event time < {window[1].isoformat()} (UTC)' if window else 'all timestamps'))
    if args.input_file:
        if Path(args.input_file).name != args.input_file or Path(args.input_file).suffix.lower() != '.plaso':
            parser.error('Input file must be a .plaso filename within the input directory')
        source = INPUT / args.input_file
        if not source.is_file():
            parser.error(f'Input file does not exist: {source}')
        archives = [source]
    else:
        archives = sorted((p for p in INPUT.iterdir() if p.is_file() and p.suffix.lower() in ('.zip', '.vhdx', '.e01', '.dd', '.img', '.raw', '.plaso')), key=lambda p: p.name)
    if not archives:
        parser.error('No supported ZIP, VHDX, E01, raw image or Plaso files found in the input folder')
    if not args.timezone and any(a.suffix.lower() != '.plaso' for a in archives):
        parser.error('--timezone is required when processing images')
    try:
        event_filter = None if args.no_filter else load_event_filter()
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    names = [export_name(a, window) + '.csv' for a in archives]
    if len(names) != len({n.casefold() for n in names}):
        parser.error('Input archive names produce colliding output names')
    cpus, memory, jobs, workers = resource_plan(len(archives))
    log(f'Automatic resources: {cpus} CPUs, {memory / 1024**3:.1f} GiB; {jobs} concurrent inputs, up to {workers} extraction workers per image')
    def run_archive(archive):
        _log_context.archive = archive.name
        try:
            log(f'=== {archive.name} ===')
            process(archive, args.timezone, window, workers, not args.no_filter, event_filter, args.image_drive_letter,
                    include_vss=args.include_vss, include_duplicates=args.include_duplicates)
        except (OSError, ValueError, RuntimeError, NotImplementedError, csv.Error, sqlite3.Error, zipfile.BadZipFile, subprocess.CalledProcessError) as exc:
            log(f'FAILED {archive.name}: {exc}')
            return archive.name
        finally:
            del _log_context.archive
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        failures = [name for name in pool.map(run_archive, archives) if name is not None]
    if failures:
        log('Failed inputs: ' + ', '.join(failures))
        return 1
    return 0


if __name__ == '__main__':
    started = time.monotonic()
    try:
        status = main()
    finally:
        elapsed = int(time.monotonic() - started)
        log(f'Total processing time: {elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}')
    sys.exit(status)
