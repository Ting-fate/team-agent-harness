from threading import Event


ASYNC_WORKER_TIMEOUT_SECONDS = 60


def wait_for_worker_event(
    event: Event,
    description: str,
    timeout: float = ASYNC_WORKER_TIMEOUT_SECONDS,
) -> None:
    if event.wait(timeout=timeout):
        return
    raise AssertionError(f"{description} was not observed within {timeout} seconds")
