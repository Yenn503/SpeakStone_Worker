"""Is a worker already running with this key? Reads the lock the worker itself holds (net_worker.InstanceLock): an
exclusive lock on the first byte of `<config file>.lock` (msvcrt on Windows, flock elsewhere). The app only ever tries
the lock and lets go at once; it never holds it."""
import sys
from pathlib import Path


def is_locked(path):
    """True when another process holds the lock. False when it is free, the file does not exist, or it cannot be told."""
    p = Path(path)
    if not p.exists():
        return False
    try:
        fh = open(p, "r+b")
    except OSError:
        return False
    try:
        fh.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            return True
        return False
    finally:
        fh.close()
