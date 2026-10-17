"""One hard ceiling for document workers and shared model request capacity."""

MAX_CONCURRENCY = 10


def bounded_concurrency(value: str | int | None = None) -> int:
    return max(1, min(MAX_CONCURRENCY, int(MAX_CONCURRENCY if value in (None, "") else value)))
