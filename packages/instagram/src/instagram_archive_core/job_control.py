"""Cancellation of one isolated downloader process tree, never the API service."""
import os
import signal
import subprocess
import threading


def process_options():
    if os.name == 'nt':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def stop_process(process, grace=10):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        # /T covers downloader children such as FFmpeg; no shell interpolation.
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       capture_output=True, timeout=15)
    else:
        try:
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
            finally:
                # A child may keep stdout open after its parent exits.
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
        except ProcessLookupError:
            pass
    process.wait(timeout=15)


def watch_cancellation(process, requested, done):
    while not done.wait(0.25):
        if process.poll() is not None:
            return
        try:
            cancel = requested()
        except Exception:
            # A transient DB error is not a user's cancellation request.
            continue
        if cancel:
            stop_process(process)
            return


def monitor(process, requested):
    done = threading.Event()
    thread = threading.Thread(target=watch_cancellation, args=(process, requested, done), daemon=True)
    thread.start()
    return done, thread
