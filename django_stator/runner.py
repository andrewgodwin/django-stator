import ctypes
import logging
import os
import signal
import threading
import time
from typing import Any

from django.conf import settings
from django.db import connections

from django_stator.exceptions import TimeoutError
from django_stator.models import StatorModel
from django_stator.timer import LoopingTimer

logger = logging.getLogger(__name__)


class StatorRunner:
    """
    Runs tasks on models that are looking for state changes.
    Designed to run either indefinitely, or just for a limited time (i.e. 30
    seconds, if called from a web view as an homage to wp-cron.php)
    """

    def __init__(
        self,
        models: list[type[StatorModel]],
        concurrency: int = getattr(settings, "STATOR_CONCURRENCY", 10),
        concurrency_per_model: int = getattr(
            settings, "STATOR_CONCURRENCY_PER_MODEL", 5
        ),
        liveness_file: str | None = None,
        watchdog_interval: int = 60,
        delete_interval: int = 30,
        task_deadline: int = getattr(settings, "STATOR_TASK_DEADLINE", 15),
    ):
        self.models = models
        self.concurrency = concurrency
        self.concurrency_per_model = concurrency_per_model
        self.liveness_file = liveness_file
        self.watchdog_interval = watchdog_interval
        self.delete_interval = delete_interval
        self.task_deadline = task_deadline
        self.minimum_loop_delay = 0.5
        self.maximum_loop_delay = 5
        # Set up SIGALRM handler
        signal.signal(signal.SIGALRM, self.alarm_handler)

    def run(self, run_for: int | None = None):
        self.handled: dict[str, int] = {}
        self.started = time.monotonic()

        self.loop_delay = self.minimum_loop_delay
        self.watchdog_timer = LoopingTimer(self.watchdog_interval)
        self.deletion_timer = LoopingTimer(self.delete_interval)
        # Spin up the worker threads
        self.workers: list[WorkerThread] = [
            WorkerThread(self) for i in range(self.concurrency)
        ]
        for worker in self.workers:
            worker.start()
        # For the first time period, launch tasks
        logger.info("Running main task loop")
        try:
            while True:
                # See if we need to handle the watchdog
                if self.watchdog_timer.check():
                    # Set up the watchdog timer (each time we do this the previous one is cancelled)
                    signal.alarm(self.watchdog_interval * 2)
                    # Write liveness file if configured
                    if self.liveness_file:
                        with open(self.liveness_file, "w") as fh:
                            fh.write(str(int(time.time())))

                # Kill any overdue workers
                self.check_worker_deadlines()

                # See if we need to add deletion tasks
                if self.deletion_timer.check():
                    self.add_deletion_tasks()

                # Fetch and run any new handlers we can fit
                self.add_transition_tasks()

                # Are we in limited run mode?
                if run_for is not None and (time.monotonic() - self.started) > run_for:
                    break

                # Prevent busylooping, but also back off delay if we have
                # no tasks
                if self.busy_workers or (
                    run_for is not None and run_for < self.maximum_loop_delay
                ):
                    self.loop_delay = self.minimum_loop_delay
                else:
                    self.loop_delay = min(
                        self.loop_delay * 1.5,
                        self.maximum_loop_delay,
                    )
                time.sleep(self.loop_delay)
        except KeyboardInterrupt:
            pass

        # Wait for tasks to finish
        logger.info("Waiting for tasks to complete")
        for worker in self.workers:
            worker.shutdown = True
        for i in range(self.task_deadline):
            if not any([w.task for w in self.workers]):
                break
            self.check_worker_deadlines()
            time.sleep(1)
        for worker in self.workers:
            worker.join()

        # We're done
        logger.info("Complete")

    def alarm_handler(self, signum, frame):
        """
        Called when SIGALRM fires, which means we missed a schedule loop.
        Just exit as we're likely deadlocked.
        """
        logger.warning("Watchdog timeout exceeded")
        os._exit(2)

    def add_transition_tasks(self, call_inline=False):
        """
        Adds a transition thread for as many instances as we can, given capacity
        and batch size limits.
        """
        # Calculate space left for tasks
        space_remaining = self.idle_workers
        # Fetch new tasks
        for model in self.models:
            if space_remaining > 0:
                for instance in model.state_get_ready(
                    number=min(space_remaining, self.concurrency_per_model),
                    lock_period=self.task_deadline,
                ):
                    self.assign_to_worker(("transition", instance))
                    space_remaining -= 1
        # Rotate models list around by one for fairness
        self.models = self.models[1:] + self.models[:1]

    def add_deletion_tasks(self, call_inline=False):
        """
        Adds a deletion thread for each model
        """
        # TODO: Make sure these always get to run and don't get starved out
        for model in self.models:
            if model.state_graph.deletion_states and self.idle_workers:
                self.assign_to_worker(("delete", model))

    @property
    def idle_workers(self) -> int:
        """
        Returns how many worker threads are currently idle and awaiting work.
        """
        return len(
            [
                worker
                for worker in self.workers
                if worker.is_alive() and worker.task is None
            ]
        )

    @property
    def busy_workers(self) -> int:
        """
        Returns how many worker threads are currently busy.
        """
        return len(self.workers) - self.idle_workers

    def assign_to_worker(self, task: tuple[str, Any]):
        """
        Assigns the given task to a worker
        """
        for worker in self.workers:
            if worker.task is None:
                worker.task = task
                worker.deadline = time.monotonic() + self.task_deadline
                break
        else:
            raise ValueError("Cannot assign task to any worker")

    def check_worker_deadlines(self):
        """
        Kills any worker tasks that are over their deadline
        """
        for worker in self.workers:
            if worker.deadline and worker.deadline < time.monotonic():
                # Inject a timeout error using a totally valid and normal API
                assert worker.ident is not None
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_long(worker.ident), ctypes.py_object(TimeoutError)
                )
                worker.deadline = None
                worker.task = None

    def log_handled(self, model_name: str, number: int):
        """
        Called from worker threads - logs that something was run
        """
        self.handled[model_name] = self.handled.get(model_name, 0) + number


class WorkerThread(threading.Thread):
    """
    Worker thread for running transitions/deletes/etc. in
    """

    def __init__(self, runner: StatorRunner):
        super().__init__()
        self.runner = runner
        self.task: tuple[str, Any] | None = None
        self.shutdown: bool = False
        self.deadline: float | None = None

    def run(self):
        try:
            while not self.shutdown or self.task:
                # Wait for a task to be assigned
                if self.task is None:
                    time.sleep(0.1)
                    continue
                # Run the correct subtask
                try:
                    if self.task[0] == "transition":
                        self.task_transition(self.task[1])
                    elif self.task[0] == "delete":
                        self.task_delete(self.task[1])
                    else:
                        logging.error(f"Unknown task type {self.task[0]}")
                except TimeoutError:
                    continue
                finally:
                    # Clear the task
                    self.task = None
                    self.deadline = None
        finally:
            connections.close_all()

    def task_transition(self, instance: StatorModel):
        """
        Runs one state transition/action.
        """
        started = time.monotonic()
        previous_state = instance.state
        result = instance.state_transition_check()
        duration = time.monotonic() - started
        if result:
            logger.info(
                f"{instance._meta.label_lower}: {instance.pk}: {previous_state} -> {result} ({duration:.2f}s)"
            )
        else:
            logger.info(
                f"{instance._meta.label_lower}: {instance.pk}: {previous_state} unchanged  ({duration:.2f}s)"
            )
        self.runner.log_handled(instance._meta.label_lower, 1)

    def task_delete(self, model: type[StatorModel]):
        """
        Runs one model deletion set.
        """
        # Loop, running deletions every second, until there are no more to do
        total_deleted = 0
        last_total = None
        while total_deleted != last_total:
            last_total = total_deleted
            total_deleted += model.state_do_deletes()
        logger.info(f"{model._meta.label_lower}: Deleted {total_deleted} stale items")
        self.runner.log_handled(model._meta.label_lower, total_deleted)
