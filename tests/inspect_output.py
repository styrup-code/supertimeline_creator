"""Summarize and validate a completed one-day timeline without dumping evidence."""
import argparse
import collections
import csv
from pathlib import Path
import sys

NOISE = ('$file_name', '/winsxs/', '/assembly/', 'odlsent', '.odl', 'cache_data',
         'code cache', 'service worker', '/packages/', '/windowsapps/microsoft/',
         '/program files/windowsapps/', 'systemapps', '/microsoft office/',
         '/acrobat reader dc/')

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('directory', type=Path)
parser.add_argument('stem')
parser.add_argument('--date', required=True, help='Expected CSV date as MM/DD/YYYY')
args = parser.parse_args()
csv.field_size_limit(min(sys.maxsize, 2_147_483_647))
paths = (args.directory / (args.stem + '-supertimeline.csv'),
         args.directory / (args.stem + '-plaso-warnings.txt'),
         args.directory / 'plaso' / (args.stem + '.plaso'))
if not all(p.is_file() and p.stat().st_size for p in paths):
    raise SystemExit('One or more output artifacts are missing or empty')
counts = collections.Counter()
bad = collections.Counter()
with paths[0].open(newline='', encoding='utf-8') as source:
    reader = csv.DictReader(source, strict=True)
    expected = ['date', 'time', 'timezone', 'MACB', 'source', 'sourcetype',
                'type', 'user', 'host', 'short', 'desc', 'version', 'filename',
                'inode', 'notes', 'format', 'extra']
    if reader.fieldnames != expected:
        raise SystemExit('Unexpected CSV header')
    for row in reader:
        counts['rows'] += 1
        if None in row or any(v is None for v in row.values()):
            bad['malformed'] += 1
            continue
        if row['date'] != args.date or row['timezone'] != 'UTC':
            bad['outside_window'] += 1
        if row['sourcetype'] == 'WinEVTX':
            counts['evtx'] += 1
            if row['type'] != 'Creation Time':
                bad['evtx_type'] += 1
        if row['sourcetype'] == 'Bodyfile':
            counts['bodyfile'] += 1
            path = row['short'].lower().replace('\\', '/')
            if any(part in path for part in NOISE):
                bad['bodyfile_noise'] += 1
print(f'{args.stem}: rows={counts["rows"]}, bodyfile={counts["bodyfile"]}, evtx={counts["evtx"]}, errors={dict(bad)}, plaso_bytes={paths[2].stat().st_size}')
if bad or not counts['rows'] or not counts['bodyfile']:
    raise SystemExit(1)
