# Known limitations and warning guide

Review each run's `<archive>-plaso-warnings.txt` before relying on a timeline.
Successful output does not establish that every artifact or event was parsed.
These observations come from tests with Plaso 20261027; behavior can change
when the floating Plaso image is updated. Inspect the actual report and Plaso
session metadata for each run.

## Jump Lists: DestList version 6

Plaso 20261027 accepts DestList format versions 1 through 4 and warns
`unsupported format version: 6` for version 6. It skips that DestList stream,
but independently attempts to parse the numbered embedded LNK streams. The
result can therefore contain some Jump List-related events while lacking
DestList-derived events and metadata. A warning alone does not establish
corruption or the number of events missing. This is a Plaso parser limitation,
not evidence that the entire Jump List was rejected.

Testing included nonempty version 6 files. This does not establish which
Windows release first created version 6. We found no reliable first-release
information for versions 5 or 6. The upstream parser as inspected also rejects
version 5, but no version 5 sample was verified in these tests.

An independent parser that supports version 6 may help recover the DestList
information; compare its entry counts, paths, timestamps and diagnostics with
the original files before relying on it. Wincent documents version 6 support
and uses its newer-entry parser for versions 3, 4 and 6. It was not tested
against our collections. Do not assume that merely changing Plaso's version
check is sufficient without independent, representative validation.

References: [Plaso Automatic Destinations parser](https://github.com/log2timeline/plaso/blob/main/plaso/parsers/olecf_plugins/automatic_destinations.py),
[Wincent parser](https://docs.rs/wincent/latest/src/wincent/destlist/parser.rs.html),
[libyal Jump List format documentation](https://github.com/libyal/dtformats/blob/main/documentation/Jump%20lists%20format.asciidoc).

## Registry transaction logs: replay is not implemented

The inspected Plaso 20261027 registry path opens the collected hive without
replaying its `.LOG`, `.LOG1` or `.LOG2` transaction logs. This runner does not
add a replay step. Collecting those files alongside a hive therefore does not
mean their pending changes have been incorporated into the parsed registry.
Dirty hives can contain an incomplete state, affecting recent keys, values and
their associated timestamps. Mismatched hive-header sequence numbers indicate
a recovery concern; they do not establish the number of missing events or
prove malicious activity.

This is a recognized upstream feature gap. As checked on 2026-11-06,
[Plaso issue #4952](https://github.com/log2timeline/plaso/issues/4952), opened on
2025-02-05, remains open. A maintainer invited a contribution; the issue does
not document an existing replay option or a scheduled fix.

### Possible future feature: registry replay preprocessing

Status: proposal only; not implemented, enabled or scheduled. No replay option
is currently available in the launcher.

Preprocessing could recover a derived hive from its
matching transaction logs, then let Plaso parse that hive. YARP provides
`recover_auto()` and `save_recovered_hive()`; Regipy provides the
`regipy-process-transaction-logs` command. Both are candidates for the Linux
container, but neither has been integrated or validated against this project's
collections. Recovery must keep the originals intact, pair logs from the same
source hive and snapshot, validate the recovered file, and record provenance
and failures. A recovered hive represents the state reconstructed from usable
logs; it is not a complete history of every registry change. Re-exporting an
existing `.plaso` file cannot add registry data that was never ingested.

Proposed workflow:

1. After evidence extraction, identify hives and their matching logs within
   the same collection, volume and snapshot.
2. Use a validated recovery library to apply eligible log records to a working
   copy, preserving the original hive and logs.
3. Validate the derived hive and pass it to Plaso with an explicit mapping to
   its source. Avoid ingesting original and recovered copies as indistinguishable
   duplicate evidence.
4. Report whether recovery was unnecessary, successful, unavailable or failed,
   including the logs used and hashes of the source and derived files.

ZIP and KAPE VHDX inputs could use their existing extraction stage. E01/raw
inputs would need a separate hive/log extraction or equivalent integration,
because Plaso currently reads those images directly.

Before implementation is accepted, compare recovered keys, values and timestamps
with an independent parser on representative collections. Cover clean and dirty
hives, missing or invalid logs, sequence ordering, snapshot separation and
reopening recovered hives in Plaso. Verify unchanged source hashes and measure
the additional processing time. Dependency choice and failure behavior remain
design decisions pending that validation.

References: [Plaso registry parser](https://github.com/log2timeline/plaso/blob/main/plaso/parsers/winreg_parser.py),
[REGF format and sequence numbers](https://github.com/libyal/libregf/blob/main/documentation/Windows%20NT%20Registry%20File%20(REGF)%20format.asciidoc),
[YARP recovery API](https://github.com/msuhanov/yarp/blob/master/yarp/Registry.py),
[Regipy recovery usage](https://github.com/mkorman90/regipy#recover-a-registry-hive-using-transaction-logs).

## SRUM and ActivitiesCache: upstream preset coverage

The `win7,!filestat` selection used for triage collections and NTFS images does
not enable `esedb/srum` or `sqlite/windows_timeline` in the inspected Plaso
20261027 preset. SRUM (`SRUDB.dat`) and Windows Timeline (`ActivitiesCache.db`)
have dedicated Plaso plugins, but those plugins are outside this preset.

This matches upstream `win7` coverage; the project's noise filters do not
discard already-parsed SRUM or ActivitiesCache events. Their dedicated events
are not generated by this selection, even if the databases were collected.
`--no-filter` does not change parser selection. Non-NTFS disk images use Plaso's
normal parser selection instead. Review the actual preset and session metadata
when changing Plaso versions.

References: [Plaso presets](https://github.com/log2timeline/plaso/blob/main/plaso/data/presets.yaml),
[SRUM plugin](https://github.com/log2timeline/plaso/blob/main/plaso/parsers/esedb_plugins/srum.py),
[Windows Timeline plugin](https://github.com/log2timeline/plaso/blob/main/plaso/parsers/sqlite_plugins/windows_timeline.py).

## Event-log extraction and recovery

Plaso handles ordinary EVTX records and candidate recovered records in separate
passes. A `Recovery warning` says a candidate could not be decoded; it is not
automatically an ordinary live event or a complete lost event. Candidates may
be residual data in EVTX chunk free space, but recovery can also encounter
records after damage or interrupted writes. Do not assume every candidate was
deleted, or that a warning proves deletion or tampering. Some candidate offsets
in tested Security, PowerShell and Terminal Services logs were in chunk free
space; this observation does not classify every warning in every collection.

An `Extraction warning` for `unable to parse event record` concerns the normal
record pass and should be investigated separately. One tested PowerShell log
contained such a warning. Its event ID and contents were not established by
the warning. For important logs, use an independent EVTX parser or recovery
tool and compare records and diagnostics. The `Creation Time` selection and
optional CSV event filter also affect what appears in the final CSV.

References: [Plaso EVTX parser](https://github.com/log2timeline/plaso/blob/main/plaso/parsers/winevtx.py),
[libevtx EVTX format and recovery notes](https://github.com/libyal/libevtx/blob/main/documentation/Windows%20XML%20Event%20Log%20(EVTX).asciidoc).

## Registry hives, Jump Lists and other parser warnings

- An out-of-range month caused an `unable to process path specification`
  warning for a user `UsrClass.dat` hive in testing. Some registry and BagMRU
  events were present, but completeness was not established. Examine the
  original hive with an independent registry or ShellBag parser if relevant.
- Unreadable registry values, unparsed SetupAPI lines and WebCache decoding
  warnings can omit individual values or events. They do not by themselves
  prove that the entire source file failed. Review the path and message.
- A Plaso worker termination was observed in one completed run. The retained
  report did not establish the cause or associate the warning with a specific
  lost artifact. A completed session alone does not prove complete processing.
  If necessary, rerun an isolated source with detailed logging and compare
  results; consult [Plaso troubleshooting](https://plaso.readthedocs.io/en/latest/sources/Troubleshooting.html).

## Collection metadata and filters

The MFT and EVTX filters are intentional noise reduction for triage review.
Adjust `timeline_noise.txt`, `event_logs.txt` and `event_ids.txt` for the
investigation, or use `--no-filter`. High-volume event categories are not all
included by default. See the [default filtering summary](README.md#default-filtering)
for the exact scope and the difference between ingestion and export filters.

- Velociraptor uploads may lack timestamp metadata, contain conflicting
  observations, have size discrepancies, or include unusual sparse maps.
  The report identifies affected paths. Conflicting or missing timestamps are
  not replaced with a guessed value. Size differences can indicate different
  observations, but do not independently establish their cause.
- Extracted Linux files cannot preserve original Windows creation and metadata
  change timestamps through `utime`. Those values remain in the collection's
  metadata when available; MFT bodyfile timestamps are a separate source.
- By default the MFT noise filter removes lines before Plaso ingestion, so
  removed MFT events are absent from both the CSV and saved database. The
  event-log filter removes selected rows from the CSV after export; parsed
  events remain in the database. Use `--no-filter` to disable both and review
  the default time window. Reprocessing is needed to recover filtered MFT data.
- The rolling three-month window is a CSV export restriction, not a guarantee
  that all source dates were successfully parsed. Use `--all-time` or an
  explicit window when appropriate. All archives in a batch share the supplied
  timezone; an unknown source timezone remains an assumption.
- The mactime preset is deprecated upstream. The NetworkList patch deliberately
  fails the image build if the expected upstream source changes. The floating
  base image, package versions and upstream dependencies can change; record
  versions and revalidate before relying on a new build.
