import datetime
import logging
from typing import ClassVar, Self

from asgiref.sync import async_to_sync, iscoroutinefunction
from django.db import models, transaction
from django.utils import timezone
from django.utils.functional import classproperty

from django_stator.exceptions import TimeoutError, TryAgainLater
from django_stator.graph import State, StateGraph

logger = logging.getLogger(__name__)


class StateField(models.CharField):
    """
    A special field that automatically gets choices from a state graph
    """

    def __init__(self, graph: type[StateGraph], **kwargs):
        # Sensible default for state length
        kwargs.setdefault("max_length", 100)
        # Add choices and initial
        self.graph = graph
        kwargs["choices"] = self.graph.choices
        kwargs["default"] = self.graph.initial_state.name
        super().__init__(**kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        kwargs["graph"] = self.graph
        return name, path, args, kwargs

    def get_prep_value(self, value):
        if isinstance(value, State):
            return value.name
        return value


class StatorModel(models.Model):
    """
    A model base class that has a state machine backing it, with tasks to work
    out when to move the state to the next one.

    You need to provide a "state" field as an instance of StateField on the
    concrete model yourself.
    """

    STATOR_BATCH_SIZE = 500

    state: StateField

    # When the state last actually changed, or the date of instance creation
    state_changed = models.DateTimeField(auto_now_add=True, db_index=True)

    # When the next state change should be attempted
    # TODO: Ensure start_after works with fresh models
    state_next = models.DateTimeField(
        null=True, blank=True, auto_now_add=True, db_index=True
    )

    # Collection of subclasses of us
    subclasses: ClassVar[list[type["StatorModel"]]] = []

    class Meta:
        abstract = True

    def __init_subclass__(cls) -> None:
        if cls is not StatorModel:
            cls.subclasses.append(cls)

    @classproperty
    def state_graph(cls) -> type[StateGraph]:
        return cls._meta.get_field("state").graph

    @property
    def state_age(self) -> float:
        return (timezone.now() - self.state_changed).total_seconds()

    @classmethod
    def state_get_ready(cls, number: int, lock_period: int) -> list[Self]:
        """
        Finds up to `number` instances that are ready to be looked at, bumps
        their state_next by lock_period, and returns them.
        """
        with transaction.atomic():
            # Query for `number` rows that have a state_next that's in the past.
            # Rows that are for states that are not automatic SHOULD have a NULL
            # state_next date, but we can handle a few if they slip through.
            # Also sort by state_next for some semblance of FIFO ordering.
            selected = list(
                cls.objects.filter(state_next__lte=timezone.now())
                .order_by("state_next")[:number]
                .select_for_update(skip_locked=True, no_key=True)
            )
            cls.objects.filter(pk__in=[i.pk for i in selected]).update(
                state_next=timezone.now() + datetime.timedelta(seconds=lock_period * 2)
            )
        return selected

    @classmethod
    def state_do_deletes(cls) -> int:
        """
        Finds instances of this model that need to be deleted and deletes them
        in small batches. Returns how many were deleted.
        """
        deleted = 0
        for state in cls.state_graph.deletion_states:
            to_delete = cls.objects.filter(
                state_changed__lte=timezone.now()
                - datetime.timedelta(seconds=state.delete_after),
            )[: cls.STATOR_BATCH_SIZE]
            deleted += cls.objects.filter(pk__in=to_delete).delete()[0]
        return deleted

    @classmethod
    def state_count_pending(cls) -> int:
        """
        Returns how many instances are "pending", i.e. need a transition
        checked.
        """
        return cls.objects.filter(state_next__lte=timezone.now()).count()

    def state_transition_check(self) -> State | None:
        """
        Attempts to transition the current state by running its handler(s).
        Returns the new state it moved to, or None if no transition occurred.
        """
        current_state: State = self.state_graph.states[self.state]

        # If it's a manual progression state don't even try
        # We shouldn't really be here, but it could be a race condition
        if current_state.externally_progressed:
            logger.warning(
                f"Warning: trying to progress externally progressed state {self.state}!"
            )
            self.state_next = None
            self.save(update_fields=["state_next"])
            return None

        # Try running its handler function
        try:
            if iscoroutinefunction(current_state.handler):
                next_state = async_to_sync(current_state.handler)(self)
            else:
                next_state = current_state.handler(self)
        except (TryAgainLater, TimeoutError):
            pass
        except BaseException as e:
            logger.exception(e)
        else:
            if next_state:
                # Ensure it's a State object
                if isinstance(next_state, str):
                    next_state = self.state_graph.states[next_state]
                # Ensure it's a child
                if next_state not in current_state.children:
                    raise ValueError(
                        f"Cannot transition from {current_state} to {next_state} - not a declared transition"
                    )
                self.state_transition(next_state)
                return next_state

        # See if it timed out since its last state change
        if (
            current_state.timeout_state
            and current_state.timeout_after
            and current_state.timeout_after <= self.state_age
        ):
            self.state_transition(current_state.timeout_state)
            return current_state.timeout_state

        # Nothing happened, bump state_next to match retry_after
        if current_state.retry_after is None:
            raise ValueError(f"Invalid retry_after on state {current_state}!")
        self.state_next = timezone.now() + datetime.timedelta(current_state.retry_after)
        self.save(update_fields=["state_next"])
        return None

    def state_transition(self, state: State | str):
        """
        Transitions the instance to the given state name, forcibly.
        """
        self.state_transition_queryset(
            self.__class__.objects.filter(pk=self.pk),
            state,
        )
        self.refresh_from_db()

    @classmethod
    def state_transition_queryset(
        cls,
        queryset: models.QuerySet,
        state: State | str,
    ):
        """
        Transitions every instance in the queryset to the given state, forcibly.
        """
        # Really ensure we have the right state object
        if isinstance(state, State):
            state_obj = cls.state_graph.states[state.name]
        else:
            state_obj = cls.state_graph.states[state]
        assert isinstance(state, State)
        # Update the state and its next transition attempt
        if state.externally_progressed:
            queryset.update(
                state=state_obj,
                state_changed=timezone.now(),
                state_next=None,
            )
        else:
            queryset.update(
                state=state_obj,
                state_changed=timezone.now(),
                state_next=timezone.now()
                + datetime.timedelta(seconds=state.start_after),
            )
