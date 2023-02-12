"""This module implements BQSKit Runtime's Worker class."""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
import traceback
from multiprocessing.connection import Connection
from multiprocessing.connection import wait
from queue import Queue
from types import FrameType
from typing import Any
from typing import Callable
from typing import cast
from typing import List

import faulthandler
faulthandler.enable()
import logging
from bqskit.runtime.address import RuntimeAddress
from bqskit.runtime.future import RuntimeFuture
from bqskit.runtime.message import RuntimeMessage
from bqskit.runtime.result import RuntimeResult
from bqskit.runtime.task import RuntimeTask
os.environ['OMP_NUM_THREADS'] = '1'


_worker = None


class Worker:
    """BQSKit Runtime's Worker."""

    def __init__(self, id: int, conn: Connection) -> None:
        """Initialize a worker with no tasks."""
        self.id = id
        self.conn = conn
        self.tasks: dict[RuntimeAddress, RuntimeTask] = {}
        self.delayed_tasks: list[RuntimeTask] = []
        self.outgoing: list[tuple[RuntimeMessage, Any]] = []
        self.ready_tasks: Queue[RuntimeAddress] = Queue()
        self.running = False
        self.mailboxes: dict[int, list[Any]] = {}
        self.mailbox_counter = 0
        self.active_task: RuntimeTask | None = None
        self.communicating = False
        self.cancelled_tasks: set[RuntimeAddress] = set()

        # Send out every emitted log message upstream
        old_factory = logging.getLogRecordFactory()

        def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            record = old_factory(*args, **kwargs)
            active_task = get_worker().active_task
            if active_task is not None:
                tid = active_task.comp_task_id
            else:
                tid = -1
            self.outgoing.append((RuntimeMessage.LOG, (tid, record)))
            return record
        logging.setLogRecordFactory(record_factory)

        self.conn.send((RuntimeMessage.STARTED, None))

    def _loop(self) -> None:
        """Main worker event loop."""
        self.running = True
        while self.running:
            self._try_idle()
            self._handle_comms()
            self._step_next_ready_task()

    def _try_idle(self) -> None:
        """If there is nothing to do, wait until we recieve a msg."""
        empty_out_box = len(self.outgoing) == 0
        no_ready_tasks = self.ready_tasks.empty()
        no_delayed_tasks = len(self.delayed_tasks) == 0

        if empty_out_box and no_ready_tasks and no_delayed_tasks:
            print(f"Idling: {self.id}, with {len(self.mailboxes)},{len(self.tasks)} mailboxes open.")
            wait([self.conn])
            print(f"Waking: {self.id}")

    def _handle_comms(self) -> None:
        """Handle all incoming and outgoing messages."""
        self.communicating = True
        while self.conn.poll():
            msg, payload = self.conn.recv()

            if msg == RuntimeMessage.SHUTDOWN:
                self.running = False
                return

            elif msg == RuntimeMessage.SUBMIT:
                task = cast(RuntimeTask, payload)
                self._add_task(task)

            elif msg == RuntimeMessage.SUBMIT_BATCH:
                tasks = cast(List[RuntimeTask], payload)
                self._add_task(tasks.pop())
                self.delayed_tasks.extend(tasks)

            elif msg == RuntimeMessage.RESULT:
                result = cast(RuntimeResult, payload)
                self._handle_result(result)

            elif msg == RuntimeMessage.CANCEL:
                addr = cast(RuntimeAddress, payload)
                self._handle_cancel(addr)

        self.communicating = False

        for out_msg in self.outgoing:
            self.conn.send(out_msg)
        self.outgoing.clear()

    def _add_task(self, task: RuntimeTask) -> None:
        """Start a task and add it to the loop."""
        self.tasks[task.return_address] = task
        task.start()
        self.ready_tasks.put(task.return_address)

    def _handle_result(self, result: RuntimeResult) -> None:
        """Insert result into appropriate mailbox and wake waiting tasks."""
        box = self.mailboxes[result.return_address.mailbox_index]
        if box[0] is False:  # Expecting a single result
            box[0] = True
            box[1] = result.result
            if box[2] is not None:
                addr = box[2]
                self.tasks[addr].send = box.pop(1)  # Send result to the task
                self.mailboxes.pop(result.return_address.mailbox_index)
                self.ready_tasks.put(addr)  # Wake it
            return

        box[1][result.return_address.mailbox_slot] = result.result
        box[0] += 1

        if box[0] >= len(box[1]):
            box[0] = True
            if box[2] is not None:
                addr = box[2]
                self.tasks[addr].send = box.pop(1)
                self.mailboxes.pop(result.return_address.mailbox_index)
                self.ready_tasks.put(addr)

    def _handle_cancel(self, addr: RuntimeAddress) -> None:
        """Remove `addr` and its children tasks from this worker."""
        self.cancelled_tasks.add(addr)

        # Remove all tasks that are children of `addr` from initialized tasks
        to_remove: list[Any] = []
        for key, task in self.tasks.items():
            if task.is_descendant_of(addr):
                to_remove.append(key)

        for key in to_remove:
            self.tasks.pop(key)

        # Remove all tasks that are children of `addr` from delayed tasks
        to_remove.clear()
        for task in self.delayed_tasks:
            if task.is_descendant_of(addr):
                to_remove.append(task)

        for task in to_remove:
            self.delayed_tasks.remove(task)

    def _step_next_ready_task(self) -> None:
        """Select a task to run, and advance it one step."""

        # Get next ready task
        # This is done in a lockless way in regards to the cancel signal
        # handler such that the active task will never be cancelled.
        addr = None
        task = None
        while True:
            if self.ready_tasks.empty():
                if len(self.delayed_tasks) > 0:
                    self._add_task(self.delayed_tasks.pop())
                return

            _addr = self.ready_tasks.get()

            if _addr in self.cancelled_tasks:
                continue

            try:
                _task = self.tasks[_addr]
            except KeyError:
                # _addr can be removed from self.tasks by a signal handler
                continue

            if any(bcb in self.cancelled_tasks for bcb in _task.breadcrumbs):
                continue

            addr, task = _addr, _task
            break

        assert addr is not None and task is not None

        try:
            self.active_task = task

            # Check again to ensure cancelled task is not started
            if task.return_address in self.cancelled_tasks:
                return

            if any(bcb in self.cancelled_tasks for bcb in task.breadcrumbs):
                return

            # Set logging level
            if len(task.breadcrumbs) <= task.max_logging_depth:
                logging.getLogger().setLevel(task.logging_level)
            else:
                logging.getLogger().setLevel(30)

            # Step it
            result = task.step()

            # Reset send value, if set
            if task.send is not None:
                task.send = None

            # Handle an await on a RuntimeFuture
            if isinstance(result, tuple) and result[0] == 'BQSKIT_MAIL_ID':
                self.mailboxes[result[1]][2] = addr

            else:
                # If not waiting on a RuntimeFuture then ready to run again
                self.ready_tasks.put(addr)

        except StopIteration as e:
            # Task finished running, package and send out result
            task_result = RuntimeResult(addr, e.value, self.id)
            self.outgoing.append((RuntimeMessage.RESULT, task_result))

            # Start delayed task
            if len(self.delayed_tasks) > 0:
                self._add_task(self.delayed_tasks.pop())

        except RuntimeCancelException:
            # Active tasks needs to be set asap
            # to prevent double cancel issue
            self.active_task = None

            # Start delayed task
            if len(self.delayed_tasks) > 0:
                self._add_task(self.delayed_tasks.pop())

        except Exception:
            assert self.active_task is not None

            # Bubble up errors
            exc_info = sys.exc_info()
            error_str = ''.join(traceback.format_exception(*exc_info))
            self.conn.send(
                (
                    RuntimeMessage.ERROR,
                    (self.active_task.comp_task_id, error_str),
                ),
            )

        finally:
            self.active_task = None

    def _get_new_mailbox_id(self) -> int:
        """Return a new unique mailbox id."""
        new_id = self.mailbox_counter
        self.mailbox_counter += 1
        return new_id

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> RuntimeFuture:
        """Submit `fn` as a task to the runtime."""
        assert self.active_task is not None
        # Group fnargs together
        fnarg = (fn, args, kwargs)

        # Create a new mailbox
        mailbox_id = self._get_new_mailbox_id()
        self.mailboxes[mailbox_id] = [False, None, None]

        # Create the tasks
        task = RuntimeTask(
            fnarg,
            RuntimeAddress(self.id, mailbox_id, 0),
            self.active_task.comp_task_id,
            self.active_task.breadcrumbs + (self.active_task.return_address,),
            self.active_task.logging_level,
            self.active_task.max_logging_depth,
        )

        # Submit the tasks (on the next cycle)
        self.outgoing.append((RuntimeMessage.SUBMIT, task))

        # Return future pointing to the mailbox
        return RuntimeFuture(mailbox_id)

    def map(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> RuntimeFuture:
        """Map `fn` over the input arguments distributed across the runtime."""
        assert self.active_task is not None
        # Group fnargs together
        fnargs = []
        if len(args) == 1:
            for arg in args[0]:
                fnargs.append((fn, (arg,), kwargs))

        else:
            for subargs in zip(*args):
                fnargs.append((fn, subargs, kwargs))

        # Create a new mailbox
        mailbox_id = self._get_new_mailbox_id()
        self.mailboxes[mailbox_id] = [0, [None] * len(fnargs), None]

        # Create the tasks
        breadcrumbs = self.active_task.breadcrumbs
        breadcrumbs += (self.active_task.return_address,)
        tasks = [
            RuntimeTask(
                fnarg,
                RuntimeAddress(self.id, mailbox_id, i),
                self.active_task.comp_task_id,
                breadcrumbs,
                self.active_task.logging_level,
                self.active_task.max_logging_depth,
            )
            for i, fnarg in enumerate(fnargs)
        ]

        # Submit the tasks
        self.outgoing.append((RuntimeMessage.SUBMIT_BATCH, tasks))

        # Return future pointing to the mailbox
        return RuntimeFuture(mailbox_id)


class RuntimeCancelException(Exception):
    pass


def cancel_signal_handler(signum: int, frame: FrameType | None) -> Any:
    if _worker is not None:
        if _worker.communicating:
            return
        time.sleep(0)
        _worker._handle_comms()
        if _worker.active_task is not None:
            if any(
                _worker.active_task.is_descendant_of(a)
                for a in _worker.cancelled_tasks
            ):
                raise RuntimeCancelException()


def start_worker(*args: Any, **kwargs: Any) -> None:
    """Start this process' worker."""
    signal.signal(signal.SIGUSR1, cancel_signal_handler)
    global _worker
    _worker = Worker(*args, **kwargs)
    _worker._loop()


def get_worker() -> Worker:
    """Return a handle on this process' worker."""
    if _worker is None:
        raise RuntimeError('Worker has not been started.')
    return _worker
