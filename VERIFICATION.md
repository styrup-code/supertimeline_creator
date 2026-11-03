# Verification

The automated tests can be run with:

```sh
python3 -m unittest discover -s tests
```

The tests exercise archive selection, mixed KAPE and Velociraptor batches,
unsafe ZIP paths, timestamp normalization, sparse uploads, date windows,
filters, output publication, launcher arguments and the guarded Plaso patch.
The test suite does not replace validation against the evidence being examined.

Previous Linux Docker tests included full KAPE and Velociraptor archives and
smaller samples. They exercised output generation, Plaso database reopening,
UTC bodyfile timestamps, CSV filtering, source timezone handling, sparse VSS
uploads and warning reporting. A mixed-archive Docker test used deliberately
truncated MFT samples, so it does not prove full-archive completeness.
These observations apply to the specific test inputs and image versions; they
are not a guarantee for other archives or later images. Full processing can
produce warnings or skip artifacts even when the run completes.

The image tested for the documented issues used Plaso 20261027, MFTECmd
2026.10.6 and mactime 4.12.1. The Dockerfile uses a floating Plaso base image
and package repositories, so a future build may use different versions.
Review [KNOWN_ISSUES.md](KNOWN_ISSUES.md) and each run's warning report.

The Windows launcher path and user mapping have unit coverage, but a native
Windows Docker Desktop end-to-end run was not available for validation.

A Linux Docker test processed a full KAPE VHDX with a bounded UTC export. It
used the collected drive-folder MFT, generated a nonempty CSV and Plaso
database, and checked the source image hash before and after. The tested VHDX
required an in-memory NTFS boot-signature fallback. Its warning report still
contained parser and recovery warnings; completion does not prove complete
coverage. UTC was an explicit test assumption, not a verified source timezone.
An all-time export was stopped during testing because it was taking much
longer; all-time export of this image has not been verified to completion.

Synthetic E01 and raw images containing two NTFS partitions were processed
together through Docker. Both selected the larger partition and included its
marker without including a marker from the smaller partition. A separate test
covered a single NTFS partition, a filesystem-only non-NTFS image and an
alternate configured drive letter. Synthetic input hashes were unchanged, and
the output databases passed SQLite integrity checks. A filesystem-only E01 from
a real collection was also opened read-only and its MFT extracted; that real
image was not processed end to end. These checks do not establish compatibility
with every filesystem, encrypted image, E01 segment set or Windows host.
