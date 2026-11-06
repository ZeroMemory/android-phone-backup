"""Validation shared by backup and deletion; never extracts TAR paths to disk."""
import hashlib
import tarfile
from pathlib import PurePosixPath


def normalized_member_path(name: str) -> str | None:
    while name.startswith('./'):
        name = name[2:]
    name = name.rstrip('/')
    if name in ('', '.'):
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or '\x00' in name:
        raise ValueError(f'Unsafe TAR member: {name!r}')
    return str(path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_archive(path):
    """Read every regular file fully, reject links/special files and duplicates."""
    files, directories, seen = {}, set(), set()
    member_count = 0
    with tarfile.open(path, 'r:') as archive:
        for member in archive:
            member_count += 1
            name = normalized_member_path(member.name)
            if name is None:
                if not member.isdir():
                    raise ValueError('Archive root must be a directory')
                continue
            if name in seen:
                raise ValueError(f'Duplicate TAR member: {name!r}')
            seen.add(name)
            if member.isdir():
                directories.add(name)
            elif member.isfile():
                if member.sparse is not None:
                    raise ValueError('Sparse TAR members are unsupported')
                digest, size = hashlib.sha256(), 0
                with archive.extractfile(member) as stream:
                    for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                        digest.update(chunk)
                        size += len(chunk)
                if size != member.size:
                    raise ValueError(f'Truncated TAR member: {name!r}')
                files[name] = {'size': size, 'sha256': digest.hexdigest()}
                parent = PurePosixPath(name).parent
                while str(parent) != '.':
                    directories.add(str(parent))
                    parent = parent.parent
            else:
                raise ValueError(f'Links and special files are unsupported: {name!r}')
        end_of_members = archive.offset
    # tarfile accepts archives without their EOF trailer; backup completion must not.
    size = __import__('os').stat(path).st_size
    if size % 512 or size < end_of_members + 1024:
        raise ValueError('Incomplete TAR: missing aligned two-block EOF trailer')
    with open(path, 'rb') as stream:
        stream.seek(end_of_members)
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            if any(chunk):
                raise ValueError('Invalid TAR: nonzero data after end of members')
    if set(files).intersection(directories):
        raise ValueError('Archive file collides with a directory path')
    return files, directories, member_count
