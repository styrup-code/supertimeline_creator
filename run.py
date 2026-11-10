#!/usr/bin/env python3
"""Launch the timeline container from Windows or Linux using Docker Compose."""
import argparse
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time


def cpu_limit(value):
    try:
        cpus = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('CPU limit must be a positive number')
    if not math.isfinite(cpus) or cpus <= 0:
        raise argparse.ArgumentTypeError('CPU limit must be a positive finite number')
    return str(cpus)


def memory_limit(value):
    if not re.fullmatch(r'[1-9][0-9]*(?:[kmg](?:b)?|b)?', value, re.IGNORECASE):
        raise argparse.ArgumentTypeError('Memory limit must be a positive integer with an optional b, k, m or g suffix (e.g. 8g)')
    return value.lower()


def container_user():
    if sys.platform == 'win32' or not hasattr(os, 'getuid'):
        return '0:0'
    return f'{os.getuid()}:{os.getgid()}'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='Input folder or an individual .plaso file')
    parser.add_argument('--output', default=os.getcwd(), help='Output folder (default: current directory)')
    parser.add_argument('--timezone', help='IANA timezone of the source system (required for images)')
    parser.add_argument('--cpus', type=cpu_limit, default='8', help='Maximum container CPUs (default: 8; fractional values allowed)')
    parser.add_argument('--memory', type=memory_limit, default='14g', help='Maximum container memory, e.g. 8g or 512m (default: 14g)')
    parser.add_argument('--image-drive-letter', default='C', type=str.upper, choices=list('ABCDEFGHIJKLMNOPQRSTUVWXYZ'),
                        help='Drive letter used for NTFS MFT paths from E01/raw images (default: C)')
    parser.add_argument('--no-filter', '-nofilter', action='store_true', help='Disable MFT noise and CSV event-log filters')
    parser.add_argument('--include-vss', action='store_true',
                        help='Include available VSS snapshots in NTFS E01/raw images')
    parser.add_argument('--include-duplicates', action='store_true',
                        help='Include duplicate CSV rows instead of deduplicating by timestamp and long description')
    windows = parser.add_mutually_exclusive_group()
    windows.add_argument('--timewindow', help='Inclusive UTC dates: YYYY-MM-DD..YYYY-MM-DD')
    windows.add_argument('--all-time', action='store_true', help='Export all events, including undated ones')
    args = parser.parse_args()
    source = Path(args.input).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    input_file = None
    if source.is_file() and source.suffix.lower() == '.plaso':
        input_file = source.name
        source = source.parent
    elif not source.is_dir():
        parser.error(f'Input must be an existing directory or .plaso file: {source}')
    if not output.is_dir():
        parser.error(f'Output directory does not exist: {output}')
    env = os.environ.copy()
    env.update(INPUT_DIR=str(source), OUTPUT_DIR=str(output),
               SUPERTIMELINE_CPUS=args.cpus, SUPERTIMELINE_MEMORY=args.memory)
    env['RUN_USER'] = container_user()
    compose = Path(__file__).resolve().with_name('docker-compose.yml')
    cmd = ['docker', 'compose', '--project-name', 'supertimeline_creator', '-f', str(compose), 'run', '--build', '--rm', 'supertimeline',
           '--image-drive-letter', args.image_drive_letter]
    if args.timezone:
        cmd.extend(('--timezone', args.timezone))
    if input_file:
        cmd.append(f'--input-file={input_file}')
    if args.timewindow:
        cmd.extend(('--timewindow', args.timewindow))
    if args.all_time:
        cmd.append('--all-time')
    if args.no_filter:
        cmd.append('--no-filter')
    if args.include_vss:
        cmd.append('--include-vss')
    if args.include_duplicates:
        cmd.append('--include-duplicates')
    started = time.monotonic()
    try:
        try:
            result = subprocess.run(cmd, env=env).returncode
        finally:
            # Use a single project label: Docker combines filters with the
            # same key using OR. This project contains only this service.
            try:
                cleanup = subprocess.run(['docker', 'image', 'prune', '--force',
                                          '--filter', 'label=com.docker.compose.project=supertimeline_creator'], env=env)
                if cleanup.returncode:
                    print('Warning: could not remove old unused project images.', file=sys.stderr)
            except FileNotFoundError:
                pass
        return result
    except FileNotFoundError:
        print('Docker was not found. Install Docker and the Compose plugin.', file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130
    finally:
        elapsed = int(time.monotonic() - started)
        print(f'Total elapsed time (including Docker startup/build): {elapsed // 3600:02d}:{(elapsed % 3600) // 60:02d}:{elapsed % 60:02d}', flush=True)


if __name__ == '__main__':
    sys.exit(main())
