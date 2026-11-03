"""Inspect raw and E01 images read-only and select the largest partition."""
from pathlib import Path
import threading


def prepare_disk(image_path, destination, drive_letter='C', log=print):
    import pytsk3

    ewf = None
    if image_path.suffix.lower() == '.e01':
        import pyewf

        segments = pyewf.glob(str(image_path))
        if not segments:
            raise ValueError('No E01 segments found')
        ewf = pyewf.handle()
        ewf.open(segments, 'r')

        class EwfImage(pytsk3.Img_Info):
            def __init__(self, handle):
                self.handle = handle
                self.lock = threading.Lock()
                super().__init__(url='', type=pytsk3.TSK_IMG_TYPE_EXTERNAL)

            def read(self, offset, size):
                with self.lock:
                    self.handle.seek(offset)
                    return self.handle.read(size)

            def get_size(self):
                return self.handle.get_media_size()

        image = EwfImage(ewf)
    else:
        image = pytsk3.Img_Info(str(image_path))

    try:
        try:
            volumes = pytsk3.Volume_Info(image)
        except OSError:
            # A filesystem-only image has no partition table.
            offset, partition, size = 0, None, image.get_size()
        else:
            # dfVFS / Plaso assign p1, p2, ... to allocated partitions in
            # TSK iterator order, excluding unallocated and metadata entries.
            allocated = [part for part in list(volumes)
                         if part.flags & pytsk3.TSK_VS_PART_FLAG_ALLOC and part.len]
            if not allocated:
                raise ValueError('Image has a partition table but no allocated partitions')
            partition, selected = max(enumerate(allocated, 1), key=lambda item: item[1].len)
            offset = int(selected.start) * int(volumes.info.block_size)
            size = int(selected.len) * int(volumes.info.block_size)
        summary = (f'Disk image: selected partition p{partition}, offset {offset}, size {size} bytes'
                   if partition else f'Disk image: filesystem image, size {size} bytes')
        log(summary)
        try:
            filesystem = pytsk3.FS_Info(image, offset=offset)
        except OSError as exc:
            raise ValueError('The selected filesystem cannot be opened; it may be encrypted or unsupported') from exc
        warnings = [summary]
        mfts = []
        is_ntfs = filesystem.info.ftype == pytsk3.TSK_FS_TYPE_NTFS
        if is_ntfs:
            try:
                mft = filesystem.open('/$MFT')
                length = mft.info.meta.size
                if length <= 0:
                    raise ValueError('Selected NTFS filesystem has an empty $MFT')
                target = destination / drive_letter / '$MFT'
                target.parent.mkdir()
                with target.open('xb') as out:
                    position = 0
                    while position < length:
                        data = mft.read_random(position, min(1024 * 1024, length - position))
                        if not data:
                            raise OSError('Short read while extracting the NTFS $MFT')
                        out.write(data)
                        position += len(data)
                mfts.append(target)
                log(f'Extracted selected NTFS $MFT; MFT paths will use drive {drive_letter}:')
                warnings.append(f'NTFS MFT path drive letter: {drive_letter}: (configured; not inferred from the image)')
            except OSError as exc:
                raise ValueError('Unable to extract the selected NTFS $MFT') from exc
        else:
            warning = 'Selected filesystem is not NTFS; MFTECmd MFT processing is skipped'
            warnings.append(warning)
            log(warning)
        return mfts, warnings, partition, is_ntfs
    finally:
        if ewf is not None:
            ewf.close()
