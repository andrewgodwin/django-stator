import datetime

import pytest
from django.utils import timezone
from testapp.models import BasicModel, BasicStates


@pytest.mark.django_db
def test_state_transition_check():
    """
    Tests that normal progression works (i.e. that the transition check function
    can return either None or a state and things move or not)
    """
    instance = BasicModel.objects.create()

    # By default it should not be ready, and so won't progress
    assert instance.state_transition_check() is None
    assert instance.state_next is not None and instance.state_next > timezone.now()

    # Make it ready, and then it should
    instance.ready = True
    instance.save()
    assert instance.state_transition_check() is BasicModel.state_graph.done
    assert instance.state_next is None

    # If we manually screw it up and give it a state_next when it shouldn't
    # have one (as the done state is externally progressed), it should fix
    # itself.
    instance.state_next = timezone.now()
    instance.save()
    assert instance.state_transition_check() is None
    assert instance.state_next is None

    # Now manually transition it to pending_delete and ensure it regains state_next
    instance.state_transition(BasicStates.pending_delete)
    assert instance.state_next is not None and instance.state_next > timezone.now()

    # Finally, set a new one up to timeout and make sure it does
    instance = BasicModel.objects.create()
    instance.state_changed = timezone.now() - datetime.timedelta(days=1)
    instance.save()
    assert instance.state_transition_check() is BasicStates.timed_out
    assert instance.state_next is None
