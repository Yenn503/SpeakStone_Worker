"""Single key presses without Enter, on Windows (msvcrt) and POSIX (termios)."""
import sys
import time


class KeyReader:
    def __init__(self):
        self._old = None

    def __enter__(self):
        if sys.platform != "win32" and sys.stdin.isatty():
            import termios
            import tty
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *a):
        if self._old is not None:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def poll(self, timeout=0.25):
        """Return one lower-case character, or None when nothing was pressed within `timeout` seconds."""
        if sys.platform == "win32":
            import msvcrt
            end = time.time() + timeout
            while True:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        msvcrt.getwch()
                        return None
                    if ch == "\x03":
                        raise KeyboardInterrupt
                    return ch.lower()
                if time.time() >= end:
                    return None
                time.sleep(0.02)
        if not sys.stdin.isatty():
            time.sleep(timeout)
            return None
        import select
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1).lower() if r else None

    def wait(self):
        while True:
            k = self.poll(0.5)
            if k:
                return k
