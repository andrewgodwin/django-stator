import datetime

import pytest
from django.utils import timezone
from testapp.models import BasicModel, BasicStates

from django_stator.runner import StatorRunner


@pytest.mark.django_db(transaction=True)
def test_runner_basic():
    """
    Tests that normal progression works inside the runner
    """

    # Make one that should progress and one that should not
    instance_ready = BasicModel.objects.create(ready=True)
    instance_unready = BasicModel.objects.create()

    # Make a runner and run it once
    runner = StatorRunner([BasicModel])
    runner.run(run_for=0)

    # One should have progressed, one should not have
    instance_ready.refresh_from_db()
    assert instance_ready.state == BasicStates.done
    assert instance_ready.state_next is None
    instance_unready.refresh_from_db()
    assert instance_unready.state == BasicStates.new
    assert instance_unready.state_next is not None


@pytest.mark.django_db(transaction=True)
def test_runner_deletion():
    """
    Tests that deletion is done by the runner
    """

    # Make one that should delete and one that should not
    instance_delete = BasicModel.objects.create()
    instance_delete.state_transition(BasicStates.deleted)
    instance_delete.state_changed = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
    instance_delete.save()
    instance_nodelete = BasicModel.objects.create()
    instance_nodelete.state_transition(BasicStates.deleted)

    # Make a runner and run it once
    runner = StatorRunner([BasicModel])
    runner.run(run_for=0)

    # One should have deleted, one should not have
    assert BasicModel.objects.filter(pk=instance_delete.pk).count() == 0
    assert BasicModel.objects.filter(pk=instance_nodelete.pk).count() == 1


@pytest.mark.django_db(transaction=True)
def test_runner_deadline():
    """
    Tests that timing out tasks works, and does not render their worker threads
    useless (and that tasks get pushed back when they time out!)
    """

    # Make one that should be super slow and not be allowed to finish, and
    # another that should finish
    instance_slow = BasicModel.objects.create()
    instance_slow.state_transition(BasicStates.slow)
    instance_fast = BasicModel.objects.create(ready=True)

    # Make instance_slow have an earlier state_next so it goes first
    instance_slow.state_next = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
    instance_slow.save()

    # Make a runner with only a single worker
    runner = StatorRunner([BasicModel], concurrency=1, task_deadline=1)
    runner.run(run_for=5)

    # Slow should not have transitioned, but fast should have
    instance_slow.refresh_from_db()
    assert instance_slow.state == BasicStates.slow
    assert instance_slow.state_next > timezone.now()
    instance_fast.refresh_from_db()
    assert instance_fast.state == BasicStates.done
