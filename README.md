# Supertimeline creator

Supertimeline Creator automates the DFIR workflow of turning triage collections
and disk images into a supertimeline CSV that can be opened in
[Timeline Explorer](https://ericzimmerman.github.io/) for
investigation. It supports KAPE and Velociraptor triage ZIPs, KAPE VHDX
containers, and E01 or raw disk images.

The Docker-based pipeline prepares the evidence, extracts and filters MFT
data with MFTECmd where available, ingests artifacts with Plaso, and exports
and filters an `l2tcsv` timeline. It produces a separate CSV, Plaso database
and warning report for each input. The default export window and filters can
be adjusted; review the warning report and [known limitations](KNOWN_ISSUES.md)
before drawing conclusions.

The image builds a Linux MFTECmd executable from .NET 10 source and combines it
with Plaso and Sleuth Kit's `mactime` utility.

This project was **vibe coded** with AI assistance. Review the code, test it
against your own collections, and check the warning reports before relying on
the results for an investigation.

The Python files have separate jobs: `run.py` is the host-side Docker Compose
launcher, `runner.py` processes the archives inside the Linux container, and
`patch_plaso.py` applies a guarded Plaso compatibility fix during image build.
The launcher needs only Python's standard library and does not require Plaso or
Linux-specific tools on the host.

## Requirements

- Docker Engine with the Compose plugin, or Docker Desktop using Linux
  containers, and Python 3.8 or later on Windows or Linux. The supplied
  MFTECmd build targets `linux-x64`; non-x86-64 hosts require compatible
  emulation and were not tested.
- Internet access for the image build. Docker Desktop must have sufficient CPU,
  memory, and disk allocated in its own settings.
- Sufficient disk space in **both** Docker's Linux filesystem and the output
  folder. ZIP contents, intermediate bodyfiles, and the working Plaso database
  live inside the container. Completed CSVs and databases are copied to the
  output folder. Large collections can need many gigabytes and substantial
  processing time.

## Usage

1. Start Docker and create an input and an output directory.
2. Put the images to process directly in the input directory. Supported files
   are KAPE or Velociraptor `.zip`, KAPE `.vhdx`, E01 `.E01`, and raw `.dd`,
   `.img` or `.raw`. The input directory is **not** scanned recursively.
   For segmented E01 images, keep the first `.E01` and all subsequent segments
   together; they are processed as one image.
3. From the project directory, run the command for your host below, replacing
   the paths and timezone. Both directories must already exist.
4. Review the CSV and its warning report in the output directory. A completed
   run can still have parser warnings or coverage limitations.

Linux:

```sh
python3 run.py --input /path/to/input --output /path/to/output --timezone Europe/Zurich
```

Windows PowerShell, with Docker Desktop using Linux containers:

```powershell
py -3 .\run.py --input 'C:\Cases\Input' --output 'C:\Cases\Output' --timezone Europe/Zurich
```

The input directory is mounted read-only. Different input types can be mixed,
but every input in one run shares the same timezone, export window and options.
Run separate commands when source systems have different timezones. Use an
IANA timezone name; if the source timezone is unknown, `UTC` is an assumption,
not proof of the source setting.

### Command options

| Option | Required / default | Description |
| --- | --- | --- |
| `--input DIRECTORY` | Required | Existing directory containing the input files. Files directly inside it are discovered automatically. |
| `--output DIRECTORY` | Current working directory | Existing directory for the CSV, warning report and Plaso database for each input. |
| `--timezone TIMEZONE` | Required | IANA source timezone for timestamps without timezone information, such as `Europe/Zurich` or `UTC`. CSV output is in UTC. |
| `--timewindow START..END` | Rolling three calendar months ending at the current UTC time | Export an inclusive UTC date range in `YYYY-MM-DD..YYYY-MM-DD` format. Cannot be combined with `--all-time`. |
| `--all-time` | Off | Export all dates and undated events. Cannot be combined with `--timewindow`. |
| `--no-filter`, `-nofilter` | Filters enabled | Disable both the MFT bodyfile noise filter and the CSV event-log filter. The time window and Plaso EVTX `Creation Time` selection still apply. |
| `--cpus NUMBER` | `8` | Maximum CPUs for the whole container; positive fractional values such as `2.5` are accepted. |
| `--memory SIZE` | `14g` | Maximum memory for the whole container; use a positive integer in bytes or with `b`, `k`, `m` or `g`, such as `512m` or `16g`. |
| `--image-drive-letter LETTER` | `C` | Label NTFS MFT paths from E01 and raw disk images with one letter, for example `D`. This is an assumption, not a detected original drive letter; it does not change ZIP or VHDX drive paths. |
| `-h`, `--help` | — | Show launcher options and exit. |

### Examples

Export an inclusive UTC date range while keeping the default filters:

```sh
python3 run.py --input /path/to/input --output /path/to/output --timezone Europe/Zurich --timewindow 2023-01-01..2023-01-27
```

Export all dates, disable both filters, and set container limits:

```sh
python3 run.py --input /path/to/input --output /path/to/output --timezone UTC --all-time --no-filter --cpus 4 --memory 8g
```

Process E01 or raw images and label NTFS MFT paths as drive `D:`:

```sh
python3 run.py --input /path/to/images --output /path/to/output --timezone UTC --image-drive-letter D
```

For a range such as `2023-01-01..2023-01-27`, both dates are included in UTC.
The default rolling window clamps the starting day if the earlier month has
fewer days. Bounded windows exclude undated events; `--all-time` includes them.
The window applies to the CSV export, while the saved `.plaso` database
contains all parsed dates, subject to the MFT noise filter. Legacy `l2tcsv`
output has second-level timestamp precision; undated events can appear as
`00/00/0000` with timezone `-`.

CPU and memory limits apply to the whole container, including all concurrent
inputs. Docker or the host may impose lower limits; Docker Desktop also has its
own resource allocation. Allow enough memory and disk space for your
collections. Direct Docker Compose users can set `SUPERTIMELINE_CPUS` and
`SUPERTIMELINE_MEMORY`; the Python launcher uses its command-line values and
defaults.

The runner automatically
chooses up to two concurrent input files and allocates extraction workers based on
available CPUs and memory; there are no worker or job settings to configure.
Fractional CPU limits are rounded down for worker allocation, with at least one
CPU assumed for planning. One bodyfile is processed by one
Plaso process, and each CSV export is single-process; extra CPUs cannot make
those individual stages fully parallel. Bounded exports use Plaso's time slice
for storage preselection and retain an exact event filter. Actual speed depends
on the number and type of artifacts, disk performance, and available memory.
During `log2timeline` extraction, Plaso's individual process and worker
limits are disabled; Docker's configured container limit remains in force. This
avoids per-file failures observed during testing with Plaso's default process
limit. Other Plaso commands retain their own defaults. A collection
can still exceed the container limit, so review reports for processing failures.

KAPE ZIPs, KAPE VHDX files, Velociraptor ZIPs and disk images can be processed
in the same batch: the layout is
detected independently for each archive. Runner messages are prefixed with
the archive name because concurrent runs can interleave; output from external
tools may also interleave and does not necessarily carry this prefix.

For each supported input, the output folder receives:

- `<name>-supertimeline.csv`
- `<name>-plaso-warnings.txt`
- `plaso/<name>.plaso`

Existing output files are never intentionally overwritten. Files are copied to
temporary names on the destination filesystem before publication; incomplete
copies are not presented as finished output. A failed archive is reported, and
other archives can still complete; any failure makes the overall exit nonzero.
Inputs with the same basename, even with different extensions, cannot be
processed in the same batch because their output names would collide.

After a launcher run, Docker prunes unused dangling images carrying this
Compose project's label. It does not perform a global image prune.
Docker retains images referenced by containers, so old images may remain until
those containers are removed. Builds that predate these labels or are run
outside the launcher may also require separate cleanup.

## Performance benchmarks

The following are historical Linux Docker measurements on particular triage
collections using earlier versions of the runner. They are examples, not
predictions for your host or the current version:

| Workload | Processing configuration | Elapsed time |
| --- | --- | ---: |
| Two identical KAPE triage subsets, each with a truncated MFT | Sequential archives, one extraction worker per archive | 111.07 s |
| The same two subsets | Two concurrent archives, one extraction worker per archive | 56.57 s |
| Full KAPE and Velociraptor triages together, approximately 1.4 GiB compressed in total | Two concurrent archives, three extraction workers each; both filters enabled | At least 22 min 3 s |
| Full KAPE triage, approximately 228 MiB compressed | One-day UTC CSV window; MFT noise filter enabled | 6 min 18 s |
| Full Velociraptor triage, approximately 1.2 GiB compressed, including VSS uploads | Default three-month CSV window; MFT noise filter enabled | 19 min 59 s |

The first two runs produced matching CSV row multisets after temporary paths
were normalized. Another full processing job was running at the time, so the
comparison was not a controlled benchmark. The full-archive runs used different
collections, windows and workloads; they should not be compared as a KAPE
versus Velociraptor speed test. For the concurrent full-archive run, the exact
launch time was not retained: the lower bound is the interval from the first
Plaso session start to the last CSV's filesystem modification time. It excludes
earlier extraction and startup and is not the total elapsed time. The other
full-archive and subset tests predate the current event-log CSV filter; all
of these runs predate the configurable resource options and do not establish
current-version timings.

For a benchmark on your system, place representative ZIPs in an input folder,
use a fresh output folder, and run the launcher with the desired `--cpus`,
`--memory`, time window and filters. The runner prints processing time and the
launcher prints total time including Docker startup/build. Record both, the
number and sizes of archives, artifact mix, Docker limits and image versions.
The first build can take longer, and Docker's cache and other system activity
affect results. Review warning reports as well as elapsed time.

## MFT noise filter

Before Plaso imports the MFTECmd bodyfile, GNU `grep -a -v -i -f` applies the
patterns in [timeline_noise.txt](timeline_noise.txt). Slash and backslash paths
are normalized for matching, then retained paths are written with Windows
backslashes. The report records the numbers of kept and removed bodyfile lines.
The patterns are bundled into the image; rebuilding applies changes to that
file. This intentionally drops matching **MFT events from both the CSV and the
saved Plaso database**, including `$FILE_NAME` entries and matching paths.
Review the patterns against your investigative needs before relying on the
result. Artifacts processed by other Plaso parsers are not filtered by this
bodyfile rule.

## Event-log CSV filter

By default, after `psort` exports the timeline, the runner keeps WinEVTX
records matching both an event-log
pattern and an Event ID, and keeps all non-WinEVTX records. Patterns are
case-insensitive regular expressions matched against the whole CSV record;
IDs match the exact `<EventID>number</EventID>` text. For Velociraptor upload
paths, the filename is also URL-decoded once for matching, so a collected
`%4Operational.evtx` log stored as `%254Operational.evtx` is recognized. The
CSV keeps the original path. The output puts selected
WinEVTX rows first and the other rows afterward,
so the complete CSV is not globally chronological. CSV records are parsed
properly, including quoted commas and multiline fields.

Edit `event_logs.txt` and `event_ids.txt` to adjust the selection. The default
12 log patterns and 108 event IDs are the supplied defaults. Use one log
pattern or numeric event ID per line.
The launcher rebuilds
the image when these files change. The CSV selection does not remove events
from the saved Plaso database. Counts appear in the console and warning report.

Use `--no-filter` (or `-nofilter`) to disable both the MFT noise filter and the
event-log CSV filter:

```sh
python3 run.py --input /path/to/triage-zips --timezone Europe/Zurich --no-filter
```

The time window and Plaso's EVTX `Creation Time` condition still apply. With
filtering disabled, the saved database also retains all MFT bodyfile entries.

## Collection layouts and timestamps

- KAPE: files under top-level single-letter drive directories, such as `C/`.
  Root-level collection logs are excluded. Deflate64 ZIPs are supported.
- KAPE VHDX: NTFS partitions are accessed read-only with the Plaso image's
  forensic libraries, without mounting the image or requiring a privileged
  container. Files under top-level drive-letter directories are copied into
  temporary working storage; the container filesystem's own NTFS `$MFT` is
  not used as the collected source MFT. Captured modification and access
  timestamps are applied where possible; failures appear in the report.
  A missing NTFS boot signature may be supplied to the reader in memory and
  recorded in the report; the source image is never repaired or written to.
  Differencing VHDX files with parent images are not supported. A VHDX must
  contain KAPE drive folders and at least one collected `$MFT`.
- E01 and raw disk images: allocated partitions are identified in disk order.
  If there is more than one, the largest by byte size is selected; ties use
  the first. Filesystem-only images are processed directly. The selected
  filesystem must be readable and unencrypted. Plaso reads the image directly;
  on NTFS, the selected filesystem's `$MFT` is also copied to temporary
  storage for MFTECmd and filtered using the same bodyfile rules as triage
  inputs. On other supported filesystems, MFT processing is skipped and Plaso
  chooses its parsers normally. VSS snapshots are not included for disk images;
  the selected current filesystem is processed.

  A disk image does not reliably identify its original Windows drive letter.
  NTFS MFT paths use `C:` by default; supply, for example,
  `--image-drive-letter D` to label them differently. This is a configured
  label, not a determination of the original drive letter. The selected
  partition and label are recorded in the warning report.
- Velociraptor: `uploads/auto`, `uploads/ntfs`, `uploads/file`, and optional
  `uploads/ntfs_vss` are processed in their separate paths; collection logs
  and `results` are excluded from Plaso input. Sparse uploads are reconstructed
  from their `.idx` maps, which are not themselves sent to Plaso.
  The runner joins `uploads.json` to `Windows.Triage.Targets/All_Matches_Metadata.json`
  or `Windows.KapeFiles.Targets/All File Metadata.json` by source path, then
  restores unambiguous recorded modification and access times on extracted
  files, matching the accessor where present. When observations disagree for a
  timestamp, that field is left at the extracted filesystem time and the
  conflicting values are recorded in the warning report. Missing mappings,
  unusual sparse maps, and differences between metadata, uploaded and stored
  sizes are also reported; timestamps can describe an earlier observation.
  A drive letter must be recognizable in each MFT volume path.

On Linux, original Windows creation and metadata-change timestamps cannot be
set on extracted files using `utime`; they remain in the original ZIP's JSON
metadata. Reading files can update access times according to filesystem mount
policy. Restored times are available to parsers that use file metadata; they do
not create file-stat events because the Plaso preset retains `!filestat`.
MFTECmd parses MFT record timestamps separately. Warning-report source paths
and some CSV `filename` fields include temporary container paths; the portion
after `evidence/` identifies the ZIP member. MFT bodyfile paths retain Windows
drive and path notation. MFTECmd bodyfile times are Unix timestamps, so their
Plaso import uses UTC; the requested input timezone applies to other artifacts.

The EVTX condition uses `windows:evtx:record` and `Creation Time`, because
current Plaso removed `parser` from event filters. The `mactime` preset is
currently deprecated upstream and may require revalidation after a Plaso update.
The image applies a narrowly scoped fix to Plaso's NetworkList registry plugin:
an unreadable `DefaultGatewayMac` value produces a warning and omits that MAC
instead of stopping the whole hive. The build deliberately fails if the
upstream plugin changes and the patch needs review.
Plaso can report extraction and recovery warnings while still producing output;
review the report for coverage limitations. See [KNOWN_ISSUES.md](KNOWN_ISSUES.md)
for limitations and [VERIFICATION.md](VERIFICATION.md) for test coverage.

## Publishing and evidence handling

The included `.gitignore` allows only project source, configuration and
documentation files by default. Input archives, generated timelines, databases,
reports, logs and other new files remain ignored. Review changes and staged
files before sharing: `git add -f` bypasses ignore rules, and evidence may also
be present in files that are already tracked. Add new project files to the
allowlist deliberately. Do not publish case data or private reports.

## License

This project's code and documentation are available under the [MIT License](LICENSE).
Everyone may use, modify and distribute them, including for commercial use,
subject to the license terms. Plaso, MFTECmd, Sleuth Kit and other dependencies
have their own licenses, which apply separately when using or distributing them.
