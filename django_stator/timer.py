import time


class LoopingTimer:
    """
    Triggers check() to be true once every `interval`.
    """

    next_run: float | None = None

    def __init__(self, interval: float, trigger_at_start=True):
        self.interval = interval
        self.trigger_at_start = trigger_at_start

    def check(self) -> bool:
        # See if it's our first time being called
        if self.next_run is None:
            # Set up the next call based on trigger_at_start
            if self.trigger_at_start:
                self.next_run = time.monotonic()
            else:
                self.next_run = time.monotonic() + self.interval
        # See if it's time to run the next call
        if time.monotonic() >= self.next_run:
            self.next_run = time.monotonic() + self.interval
            return True
        return False
