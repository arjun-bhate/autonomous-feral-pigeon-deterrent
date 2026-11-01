"""Test helper: run a sensor read in a thread but let a fast one finish first. Not a test."""

import concurrent.futures
import threading


def spawn_and_wait_briefly(work):
    """Run ``work`` in a thread. Wait up to 20 ms for it, so a fast sensor has finished on return.

    A sensor that hangs simply does not finish in that time; its future stays pending.
    """
    future: concurrent.futures.Future = concurrent.futures.Future()

    def run() -> None:
        try:
            future.set_result(work())
        except BaseException as error:
            future.set_exception(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(0.02)
    return future
