import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile
import time
import tempfile
import shlex
from pathlib import Path
from backup_integrity import inspect_archive


def file_identity(info):
    return info.st_dev, info.st_ino


def matches_output(path, expected_id, expected_hash, links):
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            if file_identity(info) != expected_id or info.st_nlink != links:
                return False
            if expected_hash is not None:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                if digest.hexdigest() != expected_hash:
                    return False
            opened = os.fstat(stream.fileno())
            current = os.lstat(path)
            return (file_identity(opened) == expected_id == file_identity(current)
                    and opened.st_nlink == current.st_nlink == links
                    and stat.S_ISREG(current.st_mode))
    except (FileNotFoundError, IsADirectoryError):
        return False


def retain_open_file(stream, partial, expected_hash):
    if partial.exists():
        info = os.fstat(stream.fileno())
        if matches_output(partial, file_identity(info), expected_hash, info.st_nlink):
            return partial
        fd, name = tempfile.mkstemp(prefix=partial.name + ".recovery-", dir=partial.parent)
        destination = Path(name)
        output = os.fdopen(fd, "wb")
    else:
        destination = partial
        output = partial.open("xb")
    stream.seek(0)
    digest = hashlib.sha256()
    with output:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            output.write(chunk)
            digest.update(chunk)
    if digest.hexdigest() != expected_hash:
        raise RuntimeError(f"Retained data changed; inspect {destination}")
    return destination


def open_windows_shared(path):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    handle = create(str(path), 0x80000000, 7, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle(handle)
        raise
    return os.fdopen(fd, "rb")


def publish_complete(partial, final, expected_id, expected_hash):
    if not matches_output(partial, expected_id, expected_hash, 1):
        raise RuntimeError(f"Prepared file changed; retained at {partial}")
    if os.name == "nt":
        with open_windows_shared(partial) as owned:
            if file_identity(os.fstat(owned.fileno())) != expected_id:
                raise RuntimeError(f"Prepared file changed; inspect {partial}")
            try:
                os.rename(partial, final)
                if not matches_output(final, expected_id, expected_hash, 1):
                    raise RuntimeError(f"Published file changed: {final}")
            except BaseException as error:
                retained = retain_open_file(owned, partial, expected_hash)
                error_type = OSError if isinstance(error, OSError) else RuntimeError
                raise error_type(f"{error}; prepared data retained at {retained}") from error
        return
    os.link(partial, final)
    if not (matches_output(partial, expected_id, expected_hash, 2)
            and matches_output(final, expected_id, None, 2)):
        raise RuntimeError(f"Published file changed: {final}; inspect prepared data at {partial}")
    directory = Path(tempfile.mkdtemp(prefix=".publish-", dir=partial.parent))
    reserve = directory / "original"
    held = directory / "pending"
    try:
        os.link(partial, reserve)
        if not matches_output(reserve, expected_id, None, 3):
            raise RuntimeError(f"Prepared file changed; inspect {reserve} and {partial}")
        os.rename(partial, held)
        if not matches_output(held, expected_id, expected_hash, 3):
            try:
                os.link(held, partial, follow_symlinks=False)
            except OSError:
                pass
            raise RuntimeError(f"Prepared path changed; moved file retained at {held}")
        if not matches_output(final, expected_id, None, 3):
            raise RuntimeError(f"Published file changed: {final}")
        held.unlink()
        with reserve.open("rb") as owned:
            try:
                reserve.unlink()
                if not matches_output(final, expected_id, expected_hash, 1):
                    raise RuntimeError(f"Published file changed: {final}")
            except BaseException as error:
                retained = retain_open_file(owned, partial, expected_hash)
                raise RuntimeError(f"{error}; prepared data retained at {retained}") from error
        directory.rmdir()
    except BaseException as error:
        retained = None
        if reserve.exists():
            try:
                info = reserve.stat()
                if matches_output(reserve, expected_id, expected_hash, info.st_nlink):
                    retained = reserve
                    try:
                        os.link(reserve, partial)
                    except OSError:
                        pass
            except OSError:
                pass
        raise RuntimeError(f"{error}; prepared data: {retained}; recovery directory: {directory}") from error


class CompleteFile:
    def __init__(self, final):
        self.final = final
        self.directory = Path(tempfile.mkdtemp(prefix="." + final.name + ".", dir=final.parent))
        self.partial = self.directory / final.name
        self.digest = hashlib.sha256()

    def __enter__(self):
        self.stream = self.partial.open("xb")
        self.expected_id = file_identity(os.fstat(self.stream.fileno()))
        return self

    def write(self, data):
        count = self.stream.write(data)
        if count != len(data):
            raise OSError(f"Short write to {self.partial}")
        self.digest.update(data)
        return count

    def __exit__(self, kind, value, traceback):
        try:
            self.stream.close()
            if kind is not None:
                print(f"Incomplete output retained at: {self.partial}", file=sys.stderr)
                return False
            publish_complete(self.partial, self.final, self.expected_id, self.digest.hexdigest())
            self.directory.rmdir()
        except BaseException:
            print(f"Output recovery directory: {self.directory}", file=sys.stderr)
            raise
        return False


CHUNK_SIZE = 4 * 1024 * 1024

CACHE_EXCLUDES = [
    "*cache*",
    "*Cache*",
    "*CACHE*",
    "*/cache",
    "*/cache/*",
    "*/code_cache",
    "*/code_cache/*",
    "*/.cache",
    "*/.cache/*",
    "*/.thumbnails",
    "*/.thumbnails/*",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--busybox", default="/data/local/tmp/busybox", help="Existing BusyBox binary on the device")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    final_path = output_dir / "shared_storage.tar"
    partial_path = output_dir / "shared_storage.tar.partial"
    error_path = output_dir / "shared_storage_tar.stderr.log"
    hash_path = output_dir / "shared_storage.tar.sha256"
    manifest_path = output_dir / "shared_storage_manifest.json"

    for path in (final_path, partial_path, error_path, hash_path, manifest_path):
        if path.exists():
            raise SystemExit(f"Refusing to overwrite existing backup: {path}")

    remote_args = [
        args.busybox,
        "tar",
        "-cf",
        "-",
        *[f"--exclude={pattern}" for pattern in CACHE_EXCLUDES],
        "-C",
        "/storage/emulated/0",
        ".",
    ]
    command = [args.adb, '-s', args.serial, 'exec-out', ' '.join(shlex.quote(value) for value in remote_args)]

    digest = hashlib.sha256()
    copied = 0
    started = time.time()
    last_report = started

    print(f"START {args.label}", flush=True)
    with error_path.open("xb") as error_file, partial_path.open("xb") as output_file:
        archive_id = file_identity(os.fstat(output_file.fileno()))
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error_file)
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(CHUNK_SIZE)
            if not chunk:
                break
            output_file.write(chunk)
            digest.update(chunk)
            copied += len(chunk)
            now = time.time()
            if now - last_report >= 15:
                gib = copied / (1024 ** 3)
                speed = copied / max(now - started, 0.001) / (1024 ** 2)
                print(f"PROGRESS {args.label} {gib:.2f} GiB {speed:.1f} MiB/s", flush=True)
                last_report = now
        process.stdout.close()
        return_code = process.wait()
        output_file.flush()
        os.fsync(output_file.fileno())

    if return_code != 0:
        print(f"FAILED {args.label} adb/tar exit={return_code}; partial retained", flush=True)
        return return_code or 1

    # A zero exit alone does not prove that stdout contains a complete TAR.
    files, directories, member_count = inspect_archive(partial_path)
    sha256 = digest.hexdigest()
    publish_complete(partial_path, final_path, archive_id, sha256)
    with CompleteFile(hash_path) as stream:
        stream.write(f"{sha256}  {final_path.name}\n".replace("\n", os.linesep).encode("ascii"))

    file_count = len(files)
    logical_bytes = sum(info['size'] for info in files.values())

    completed = time.time()
    manifest = {
        "device_label": args.label,
        "serial": args.serial,
        "source": "/storage/emulated/0",
        "archive": final_path.name,
        "archive_bytes": final_path.stat().st_size,
        "sha256": sha256,
        "tar_member_count": member_count,
        "regular_file_count": file_count,
        "logical_file_bytes": logical_bytes,
        "started_unix": started,
        "completed_unix": completed,
        "duration_seconds": round(completed - started, 3),
        "tar_exit_code": return_code,
        "excluded_patterns": CACHE_EXCLUDES,
        "files": files,
    }
    with CompleteFile(manifest_path) as stream:
        stream.write((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").replace("\n", os.linesep).encode("utf-8"))

    print(
        f"COMPLETE {args.label} archive={manifest['archive_bytes']} bytes "
        f"files={file_count} sha256={sha256}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
