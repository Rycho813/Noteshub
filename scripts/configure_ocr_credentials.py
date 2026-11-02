"""Interactive root-only writer. Never prints keys or overwrites existing files."""
import getpass
import json
import os
from pathlib import Path
import stat
import sys

TARGET = Path('/etc/noteshub-v2-ocr-test/tencent-ocr.json')


def write_credentials(target, secret_id, secret_key):
    if not all(isinstance(value,str) and value.strip() for value in (secret_id,secret_key)):
        raise ValueError('Both fields must be nonempty')
    payload=json.dumps({'SecretId':secret_id,'SecretKey':secret_key}).encode('utf-8')
    if len(payload)>16384:
        raise ValueError('Credential file too large')
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o400)
    try:
        with os.fdopen(fd,'wb') as output:
            os.fchown(output.fileno(),10001,10001)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if target.exists():
            target.unlink()
        raise


def main():
    if os.geteuid()!=0 or not sys.stdin.isatty():
        raise SystemExit('Run with sudo in an interactive SSH terminal; no values were read.')
    try:
        TARGET.parent.mkdir(mode=0o700,parents=False,exist_ok=True)
        directory=TARGET.parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid!=0 or directory.st_mode&0o077:
            raise ValueError('Credential directory must be private and root-owned')
        if TARGET.exists() or TARGET.is_symlink():
            raise FileExistsError()
        secret_id=getpass.getpass('SecretId (hidden): ')
        secret_key=getpass.getpass('SecretKey (hidden): ')
        write_credentials(TARGET,secret_id,secret_key)
    except FileExistsError:
        raise SystemExit('Credential file already exists; nothing was read or overwritten.')
    except (OSError,ValueError):
        raise SystemExit('Credential setup failed; no key values were printed.')
    print('Private credential file created for UID 10001, mode 0400. No API request made.')


if __name__=='__main__':
    main()
