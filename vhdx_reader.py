"""Read KAPE drive folders from a VHDX without mounting or changing it."""
from pathlib import Path
import os
import re


def extract_vhdx(image_path, destination, log=print):
    # These libraries are provided by the Plaso container; ZIP-only operation
    # and host-side unit tests do not require them.
    import pytsk3
    import pyvhdi

    class Image(pytsk3.Img_Info):
        def __init__(self, source):
            self.source = source
            self.boot_signatures = set()
            super().__init__(url='', type=pytsk3.TSK_IMG_TYPE_EXTERNAL)

        def read(self, offset, size):
            data = bytearray(self.source.read_buffer_at_offset(size, offset))
            for position in self.boot_signatures:
                for index, value in enumerate(b'\x55\xaa'):
                    relative = position + index - offset
                    if 0 <= relative < len(data):
                        data[relative] = value
            return bytes(data)

        def get_size(self):
            return self.source.media_size

    def restore_times(target, metadata):
        if metadata is None:
            return
        atime = metadata.atime * 1_000_000_000 + metadata.atime_nano
        mtime = metadata.mtime * 1_000_000_000 + metadata.mtime_nano
        try:
            os.utime(target, ns=(atime, mtime), follow_symlinks=False)
        except (OverflowError, OSError, ValueError) as exc:
            warnings.append(f'WARNING: Could not restore copied file timestamps for {target.relative_to(destination)}: {exc}')

    def copy_tree(directory, target):
        nonlocal count
        for entry in directory:
            name = entry.info.name.name
            if not name or name in (b'.', b'..'):
                continue
            try:
                name = name.decode('utf-8', 'strict')
            except UnicodeDecodeError as exc:
                raise ValueError('VHDX contains a filename that cannot be decoded as UTF-8') from exc
            if name in ('.', '..') or '/' in name or '\\' in name or '\x00' in name:
                raise ValueError(f'Unsafe VHDX filename: {name!r}')
            metadata = entry.info.meta
            if metadata is None or not metadata.flags & pytsk3.TSK_FS_META_FLAG_ALLOC:
                continue
            out = target / name
            if metadata.type == pytsk3.TSK_FS_META_TYPE_DIR:
                out.mkdir()
                copy_tree(entry.as_directory(), out)
                restore_times(out, metadata)
            elif metadata.type == pytsk3.TSK_FS_META_TYPE_REG:
                with out.open('xb') as output:
                    offset = 0
                    while offset < metadata.size:
                        data = entry.read_random(offset, min(1024 * 1024, metadata.size - offset))
                        if not data:
                            raise OSError(f'Short read from VHDX file {out.relative_to(destination)}')
                        output.write(data)
                        offset += len(data)
                restore_times(out, metadata)
                count += 1
            else:
                warnings.append(f'WARNING: Skipped non-regular VHDX entry: {out.relative_to(destination)}')

    warnings, mfts, seen_drives = [], [], set()
    count = 0
    disk = pyvhdi.file()
    try:
        disk.open(str(image_path), 'r')
        if disk.parent_filename:
            raise ValueError('Differencing VHDX images with a parent are not supported')
        image = Image(disk)
        try:
            volumes = pytsk3.Volume_Info(image)
            offsets = [int(part.start) * int(volumes.info.block_size) for part in volumes
                       if part.flags & pytsk3.TSK_VS_PART_FLAG_ALLOC and part.len]
        except OSError:
            offsets = [0]
        for offset in offsets:
            boot = disk.read_buffer_at_offset(512, offset)
            if len(boot) != 512 or boot[3:11] != b'NTFS    ':
                continue
            try:
                fs = pytsk3.FS_Info(image, offset=offset, type=pytsk3.TSK_FS_TYPE_NTFS)
            except OSError:
                # DiscUtils-produced KAPE VHDX files can have a missing 55aa
                # boot signature. Overlay only those bytes in memory; never
                # write a repair to the evidence image.
                if boot[510:512] != b'\x00\x00':
                    raise
                # Start a fresh TSK image to avoid reusing cached unpatched
                # sectors from the initial filesystem-open attempt.
                patched_image = Image(disk)
                patched_image.boot_signatures.add(offset + 510)
                fs = pytsk3.FS_Info(patched_image, offset=offset, type=pytsk3.TSK_FS_TYPE_NTFS)
                warning = f'WARNING: NTFS boot signature missing at offset {offset}; used an in-memory signature for read-only access'
                warnings.append(warning)
                log(warning)
            for entry in fs.open_dir(path='/'):
                name = entry.info.name.name
                if not name or not re.fullmatch(rb'[A-Za-z]', name):
                    continue
                metadata = entry.info.meta
                if metadata is None or metadata.type != pytsk3.TSK_FS_META_TYPE_DIR or not metadata.flags & pytsk3.TSK_FS_META_FLAG_ALLOC:
                    continue
                drive = name.decode('ascii')
                if drive.casefold() in seen_drives:
                    raise ValueError(f'Duplicate KAPE drive folder in VHDX partitions: {drive}')
                seen_drives.add(drive.casefold())
                out = destination / drive
                out.mkdir()
                copy_tree(entry.as_directory(), out)
                restore_times(out, metadata)
                mfts.extend(path for path in out.rglob('*') if path.is_file() and path.name.lower() == '$mft')
        if not seen_drives:
            raise ValueError('No KAPE drive folders found in a supported NTFS VHDX partition')
        if not mfts:
            raise ValueError('No $MFT was found in the VHDX KAPE drive folders')
        log(f'Layout: KAPE VHDX drive folders; extracted {count} files; MFTs: {len(mfts)}')
        return mfts, warnings
    finally:
        disk.close()
