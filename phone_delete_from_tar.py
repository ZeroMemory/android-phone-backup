"""Plan deletion, or explicitly remove only verified archived shared-storage files."""
import argparse
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from backup_integrity import inspect_archive, normalized_member_path, sha256_file


def run(adb, serial, command):
    return subprocess.run([adb, '-s', serial, 'shell', command], capture_output=True, check=False)


def verified_backup(archive_path, manifest_path, serial):
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('serial') != serial or manifest.get('source') != '/storage/emulated/0':
        raise ValueError('Manifest device/source does not match requested device')
    if manifest.get('archive') != archive_path.name or manifest.get('archive_bytes') != archive_path.stat().st_size:
        raise ValueError('Archive identity/size does not match manifest')
    if manifest.get('sha256') != sha256_file(archive_path):
        raise ValueError('Archive SHA-256 does not match manifest; no deletion permitted')
    files, directories, _ = inspect_archive(archive_path)
    if manifest.get('files') != files:
        raise ValueError('Per-file hashes do not match manifest; regenerate a verified backup')
    return files, directories


def archived_path_check(tool, relative, name):
    # The sentinel preserves trailing newlines in path names through shell substitution.
    return (
        'cd /storage/emulated/0 || exit 10; '
        f'root=$({tool} realpath . && printf .) || exit 10; '
        f'target=$({tool} realpath {relative} && printf .) || exit 10; '
        'root=${root%??}; target=${target%??}; '
        f'[ "$target" = "$root"/{shlex.quote(name)} ] || exit 11; '
    )


def delete_verified_file(adb, serial, busybox, name, expected):
    # Prefix ./ prevents option interpretation even for filenames beginning with '-'.
    relative = shlex.quote('./' + name)
    tool = shlex.quote(busybox)
    # Hash and delete run in one shell invocation; active writers must be stopped by the user.
    command = archived_path_check(tool, relative, name) + (
        f'[ -f {relative} ] && [ ! -L {relative} ] || exit 12; '
        f'actual=$({tool} sha256sum < {relative}) || exit 13; '
        'actual=${actual%% *}; '
        f'[ "$actual" = {shlex.quote(expected["sha256"])} ] || exit 14; '
        f'{tool} rm -f -- {relative} || exit 15; '
        f'[ ! -e {relative} ] && [ ! -L {relative} ]'
    )
    return run(adb, serial, command).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb', required=True)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--manifest', help='Defaults to shared_storage_manifest.json beside archive')
    parser.add_argument('--report', required=True)
    parser.add_argument('--busybox', default='/data/local/tmp/busybox')
    parser.add_argument('--execute', action='store_true', help='Enable deletion; default is a local dry run')
    parser.add_argument('--confirm-serial', help='Must exactly match --serial when --execute is used')
    args = parser.parse_args()
    report_path = Path(args.report)
    if report_path.exists():
        parser.error('Refusing to overwrite report')
    if args.execute and args.confirm_serial != args.serial:
        parser.error('--execute requires --confirm-serial matching --serial')
    archive_path = Path(args.archive)
    manifest_path = Path(args.manifest) if args.manifest else archive_path.with_name('shared_storage_manifest.json')
    files, directories = verified_backup(archive_path, manifest_path, args.serial)
    # Reserve a writable new report before any device command; never clobber a concurrent report.
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open('x', encoding='utf-8') as report_stream:
        failures, deleted = {}, 0
        if args.execute:
            for name, expected in sorted(files.items()):
                code = delete_verified_file(args.adb, args.serial, args.busybox, name, expected)
                if code:
                    failures[name] = code
                else:
                    deleted += 1
            # Only empty directories named by the archive can be removed. Never recurse.
            for name in sorted(directories, key=lambda path: -len(PurePosixPath(path).parts)):
                relative, tool = shlex.quote('./' + name), shlex.quote(args.busybox)
                command = (archived_path_check(tool, relative, name)
                           + f'[ ! -L {relative} ] && {tool} rmdir -- {relative}')
                run(args.adb, args.serial, command)
        report = {
            'device_label': args.label, 'serial': args.serial,
            'archive': archive_path.name, 'mode': 'execute' if args.execute else 'dry_run',
            'verified_archive_sha256': sha256_file(archive_path),
            'archived_file_count': len(files), 'deleted_file_count': deleted,
            'failed_files': failures, 'completed_unix': time.time(),
        }
        report_stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'mode': report['mode'], 'archived': len(files), 'deleted': deleted, 'failed': len(failures)}))
        return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
