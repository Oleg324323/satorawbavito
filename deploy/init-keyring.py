"""Create the fresh VPS key once in its private persistent Docker volume."""
import os
import secrets
import stat
from pathlib import Path

directory = Path('/run/marketplace-keys')
directory.mkdir(mode=0o700, parents=True, exist_ok=True)
if directory.is_symlink():
    raise RuntimeError('invalid_key_directory')
directory.chmod(0o700)
key = directory / '1'
try:
    descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
except FileExistsError:
    info = key.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != 32 or info.st_mode & 0o077:
        raise RuntimeError('invalid_existing_key')
else:
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(secrets.token_bytes(32))
        stream.flush()
        os.fsync(stream.fileno())
print('Credential keyring ready; existing keys are never replaced.')
