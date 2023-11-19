import time

from django.db import models

from django_stator.graph import State, StateGraph
from django_stator.models import StateField, StatorModel


class BasicStates(StateGraph):
    new = State(retry_after=5)
    slow = State(retry_after=5)
    done = State(externally_progressed=True)
    timed_out = State(delete_after=10)
    pending_delete = State(retry_after=5, start_after=5)
    deleted = State(delete_after=10)

    new.transitions_to(done)
    new.transitions_to(slow)
    new.transitions_to(pending_delete)
    new.timeout_to(timed_out, seconds=10)
    slow.transitions_to(done)
    done.transitions_to(pending_delete)
    pending_delete.transitions_to(deleted)

    @classmethod
    def check_new(cls, instance):
        if instance.ready:
            return cls.done

    @classmethod
    def check_slow(cls, instance):
        time.sleep(2)
        return cls.done

    @classmethod
    def check_pending_delete(cls, instance):
        if instance.ready:
            return cls.deleted


class BasicModel(StatorModel):
    state = StateField(BasicStates)
    ready = models.BooleanField(default=False)
