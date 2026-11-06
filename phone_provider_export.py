import argparse
import errno
import base64
import hashlib
import json
import os
import stat
import tempfile
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path


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


def rename_without_replacement(source, destination):
    import ctypes
    import errno

    library = ctypes.CDLL(None, use_errno=True)
    source_bytes, destination_bytes = os.fsencode(source), os.fsencode(destination)
    if sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        operation = library.renameat2
        operation.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        result = operation(-100, source_bytes, -100, destination_bytes, 1)
    else:
        raise OSError(errno.ENOTSUP, "No exclusive rename is available", str(destination))
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def publish_by_exclusive_copy(partial, final, expected_id, expected_hash):
    created_id = None
    try:
        with final.open("xb") as output:
            created_id = file_identity(os.fstat(output.fileno()))
            digest = hashlib.sha256()
            with partial.open("rb") as source:
                if file_identity(os.fstat(source.fileno())) != expected_id:
                    raise RuntimeError(f"Prepared file changed: {partial}")
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    if output.write(chunk) != len(chunk):
                        raise OSError(f"Short write to {final}")
                    digest.update(chunk)
            if digest.hexdigest() != expected_hash:
                raise RuntimeError(f"Prepared file changed: {partial}")
        if not matches_output(final, created_id, expected_hash, 1):
            raise RuntimeError(f"Published file changed: {final}")
        return created_id
    except BaseException as error:
        held = None
        try:
            if created_id is not None and final.exists() and file_identity(final.stat()) == created_id:
                directory = Path(tempfile.mkdtemp(prefix=".copy-recovery-", dir=final.parent))
                held = directory / final.name
                os.rename(final, held)
                if file_identity(held.stat()) != created_id:
                    try:
                        os.link(held, final, follow_symlinks=False)
                    except OSError:
                        pass
        except OSError:
            pass
        raise RuntimeError(f"{error}; current file retained at {held or final}") from error


def publish_without_links(partial, final, expected_id, expected_hash):
    fd, name = tempfile.mkstemp(prefix=partial.name + ".recovery-", dir=partial.parent)
    recovery = Path(name)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as output, partial.open("rb") as source:
            if file_identity(os.fstat(source.fileno())) != expected_id:
                raise RuntimeError(f"Prepared file changed: {partial}")
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                output.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != expected_hash:
            raise RuntimeError(f"Prepared file changed: {partial}")
        try:
            rename_without_replacement(partial, final)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP):
                raise
            created_id = publish_by_exclusive_copy(partial, final, expected_id, expected_hash)
            directory = Path(tempfile.mkdtemp(prefix=".stage-recovery-", dir=partial.parent))
            held = directory / partial.name
            os.rename(partial, held)
            if not matches_output(held, expected_id, expected_hash, 1):
                raise RuntimeError(f"Prepared path changed; inspect {held}")
            if not matches_output(final, created_id, expected_hash, 1):
                raise RuntimeError(f"Published file changed: {final}; prepared file retained at {held}")
            held.unlink()
            if not matches_output(final, created_id, expected_hash, 1):
                raise RuntimeError(f"Published file changed: {final}")
            directory.rmdir()
            published_id = created_id
        else:
            if not matches_output(final, expected_id, expected_hash, 1):
                raise RuntimeError(f"Published file changed: {final}")
            published_id = expected_id
        with recovery.open("rb") as owned:
            recovery.unlink()
            if not matches_output(final, published_id, expected_hash, 1):
                retained = retain_open_file(owned, recovery, expected_hash)
                raise RuntimeError(f"Published file changed: {final}; prepared data retained at {retained}")
    except BaseException as error:
        raise RuntimeError(f"{error}; recovery file retained at {recovery}") from error


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
    try:
        os.link(partial, final)
    except OSError as error:
        if error.errno not in (errno.EPERM, errno.EACCES, errno.ENOSYS, errno.ENOTSUP):
            raise
        publish_without_links(partial, final, expected_id, expected_hash)
        return
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


DATASETS = {
    "sms": "content://sms",
    "mms": "content://mms",
    "mms_parts": "content://mms/part",
    "contacts": "content://com.android.contacts/contacts",
    "raw_contacts": "content://com.android.contacts/raw_contacts",
    "contact_data": "content://com.android.contacts/data",
    "contact_groups": "content://com.android.contacts/groups",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_adb(adb: str, serial: str, *remote_args: str, shell_status: bool = False) -> subprocess.CompletedProcess[bytes]:
    transport = ["shell", "-T"] if shell_status else ["exec-out"]
    return subprocess.run(
        [adb, "-s", serial, *transport, *remote_args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def is_denied_or_failed(result: subprocess.CompletedProcess[bytes]) -> bool:
    stdout_lines = result.stdout.splitlines()
    first_stdout = next((line for line in stdout_lines if line.strip()), b"")
    markers = (
        b"java.lang.SecurityException",
        b"SecurityException",
        b"Permission Denial",
        b"Error while accessing provider",
        b"Unknown URI",
    )
    return result.returncode != 0 or any(
        line.lstrip().startswith(markers) for line in [first_stdout, *result.stderr.splitlines()]
    )


def safe_error(result: subprocess.CompletedProcess[bytes]) -> str:
    combined = (result.stderr + b"\n" + result.stdout).decode("utf-8", errors="replace")
    first_line = next((line.strip() for line in combined.splitlines() if line.strip()), "unknown error")
    return first_line[:500]



def query_rows(payload: bytes) -> list[str]:
    text = payload.decode("utf-8")
    if text.strip() == "No result found.":
        return []
    if not re.match(r"^Row:[ \t]+\d+[ \t]+[^=\r\n]+=", text):
        raise ValueError("Unrecognized content query response")
    # String column values may contain newlines in the ADB text format.
    rows = [row.removesuffix("\n").removesuffix("\r")
            for row in re.split(r"(?m)^Row:[ \t]+\d+[ \t]+", text)[1:]]
    if any(not re.match(r"[^=\r\n]+=", row) for row in rows):
        raise ValueError("Unrecognized content query row")
    return rows

def vcard_reader_path(value: str) -> str:
    if not re.fullmatch(r"/data/local/tmp/[A-Za-z0-9][A-Za-z0-9_.-]*\.dex", value):
        raise argparse.ArgumentTypeError("use a .dex file under /data/local/tmp with a plain filename")
    return value


def export_vcards(adb: str, serial: str, output_dir: Path, vcard_reader: str | None = None) -> dict:
    lookup_result = run_adb(
        adb,
        serial,
        "content",
        "query",
        "--uri",
        "content://com.android.contacts/contacts",
        "--projection",
        "lookup",
    )
    if is_denied_or_failed(lookup_result):
        return {"status": "unavailable", "error": safe_error(lookup_result)}

    try:
        rows = query_rows(lookup_result.stdout)
    except ValueError as error:
        return {"status": "unavailable", "error": str(error)}
    lookups = []
    missing_lookups = 0
    for row in rows:
        match = re.fullmatch(r"lookup=(.*)", row, re.DOTALL)
        if match and match.group(1):
            lookups.append(match.group(1))
        else:
            missing_lookups += 1

    vcard_path = output_dir / "contacts.vcf"
    if vcard_path.exists():
        raise RuntimeError(f"Refusing to overwrite {vcard_path}")

    digest = hashlib.sha256()
    written = 0
    exported = 0
    failures = missing_lookups
    with CompleteFile(vcard_path) as output:
        for lookup in lookups:
            encoded_lookup = urllib.parse.quote(lookup, safe="")
            uri = f"content://com.android.contacts/contacts/as_vcard/{encoded_lookup}"
            if vcard_reader is None:
                result = run_adb(adb, serial, "content", "read", "--uri", uri)
            else:
                result = run_adb(adb, serial, "env", f"CLASSPATH={vcard_reader}", "app_process",
                                 "/system/bin", "ReadContactVCard", uri, shell_status=True)
            if is_denied_or_failed(result):
                failures += 1
                continue
            payload = result.stdout
            if vcard_reader is not None:
                try:
                    payload = base64.b64decode(payload.strip(), validate=True)
                except ValueError:
                    failures += 1
                    continue
            lines = payload.strip().splitlines()
            complete_vcard = (bool(lines) and lines[0] == b"BEGIN:VCARD"
                              and lines[-1] == b"END:VCARD"
                              and lines.count(b"BEGIN:VCARD") == 1
                              and lines.count(b"END:VCARD") == 1)
            if not complete_vcard:
                failures += 1
                continue
            if payload and not payload.endswith(b"\n"):
                payload += b"\n"
            output.write(payload)
            digest.update(payload)
            written += len(payload)
            exported += 1

    return {
        "status": "ok" if not failures else "partial",
        "lookup_count": len(rows),
        "exported_vcards": exported,
        "failed_vcards": failures,
        "bytes": written,
        "sha256": digest.hexdigest(),
        "file": vcard_path.name,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adb", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--vcard-reader", type=vcard_reader_path, help="use an existing Contacts asset reader DEX on the device")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(f"Refusing to use non-empty output directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "provider_export_manifest.json"
    if manifest_path.exists():
        raise SystemExit(f"Refusing to overwrite {manifest_path}")

    manifest = {
        "device_label": args.label,
        "serial": args.serial,
        "datasets": {},
    }

    for name, uri in DATASETS.items():
        result = run_adb(args.adb, args.serial, "content", "query", "--uri", uri)
        if is_denied_or_failed(result):
            manifest["datasets"][name] = {
                "status": "unavailable",
                "error": safe_error(result),
            }
            continue

        try:
            rows = query_rows(result.stdout)
        except ValueError as error:
            manifest["datasets"][name] = {"status": "unavailable", "error": str(error)}
            continue

        output_path = output_dir / f"{name}.txt"
        if output_path.exists():
            raise SystemExit(f"Refusing to overwrite {output_path}")
        with CompleteFile(output_path) as output:
            output.write(result.stdout)
        row_count = len(rows)
        manifest["datasets"][name] = {
            "status": "ok",
            "rows": row_count,
            "bytes": len(result.stdout),
            "sha256": sha256_bytes(result.stdout),
            "file": output_path.name,
        }

    manifest["vcards"] = export_vcards(args.adb, args.serial, output_dir, args.vcard_reader)
    with CompleteFile(manifest_path) as stream:
        stream.write((json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").replace("\n", os.linesep).encode("utf-8"))

    summary = {
        name: value.get("rows", value.get("status"))
        for name, value in manifest["datasets"].items()
    }
    print(json.dumps({"device": args.label, "datasets": summary, "vcards": manifest["vcards"]}, ensure_ascii=False))
    complete = all(value['status'] == 'ok' for value in manifest['datasets'].values())
    complete = complete and manifest['vcards']['status'] == 'ok'
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
