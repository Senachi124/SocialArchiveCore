"""Cancel the complete capture child process group on pause or shutdown."""
import os
import signal
import subprocess
import time


def stop_child(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def run_child(args, cancel=None, tick=None):
    if cancel and cancel():
        raise InterruptedError('Job cancelled')
    process = subprocess.Popen(args, start_new_session=True, stderr=subprocess.DEVNULL)
    try:
        while process.poll() is None:
            if cancel and cancel():
                raise InterruptedError('Job cancelled')
            if tick:
                tick()
            time.sleep(0.2)
        if cancel and cancel():
            raise InterruptedError('Job cancelled')
        return process.returncode
    finally:
        stop_child(process)
