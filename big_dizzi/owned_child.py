"""Supervise only our Codex process group; parent death cannot orphan a turn."""
import os
import signal
import subprocess
import sys
import time


def main():
    parent = int(sys.argv[1])
    if os.getppid() != parent:
        return 125
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    child = subprocess.Popen(sys.argv[2:], start_new_session=True)
    try:
        while child.poll() is None and not stopping and os.getppid() == parent:
            time.sleep(.1)
    finally:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try: child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=1)
    return child.returncode


if __name__ == '__main__':
    raise SystemExit(main())
