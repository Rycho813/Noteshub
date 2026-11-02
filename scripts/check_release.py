"""Verify a prepared release without reading credentials or application data."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re


def verify(root):
    manifest_path = root/'RELEASE.json'
    if manifest_path.is_symlink():
        raise ValueError('RELEASE.json must not be a symlink')
    content = manifest_path.read_bytes()
    manifest = json.loads(content)
    if not isinstance(manifest,dict):
        raise ValueError('Unsupported release manifest')
    if manifest.get('format') != 1 or not isinstance(manifest.get('sha256'),dict):
        raise ValueError('Unsupported release manifest')
    files = manifest['sha256']
    for name, checksum in files.items():
        if not isinstance(name,str):
            raise ValueError('Invalid release path')
        relative = PurePosixPath(name)
        if not name or relative.is_absolute() or '..' in relative.parts or '\\' in name or str(relative) != name:
            raise ValueError('Invalid release path')
        path = root/name
        if any((root/parent).is_symlink() for parent in relative.parents) or path.is_symlink() or not path.is_file():
            raise ValueError('Release files must be ordinary files')
        if not isinstance(checksum,str) or not re.fullmatch(r'[0-9a-f]{64}',checksum):
            raise ValueError('Invalid release checksum')
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
        if digest.hexdigest() != checksum:
            raise ValueError('Release content has changed: '+name)
    # Docker COPY includes the complete application directory; do not silently
    # build unmanifested code or follow symlinks added after extraction.
    for path in (root/'noteshub').rglob('*'):
        if path.is_symlink():
            raise ValueError('Application symlinks are not allowed')
        if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc' and path.relative_to(root).as_posix() not in files:
            raise ValueError('Unmanifested application file')
    required = {'Dockerfile','requirements.txt','constraints.txt','docker-compose.ocr-test.yml',
                'scripts/ocr_test.sh','scripts/check_release.py','noteshub/web.py','noteshub/ocr.py'}
    if not required.issubset(files):
        raise ValueError('Release is missing the test workflow')
    return hashlib.sha256(content).hexdigest(), len(files)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--digest',action='store_true')
    args=parser.parse_args()
    try:
        digest,count=verify(Path(__file__).resolve().parents[1])
    except (OSError,ValueError,TypeError,KeyError):
        parser.exit(1,'Release verification failed. Use a fresh verified archive; no changes were made.\n')
    print(digest if args.digest else f'Release OK: {count} files, manifest SHA-256 {digest}')


if __name__ == '__main__':
    main()
