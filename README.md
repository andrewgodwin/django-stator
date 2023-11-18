# Django Stator


## Mechanics

Any model that is tracked by Stator must have three columns (automatically
defined when you inherit from `StatorModel`):

* A `state` column, a string, representing the state it is currently in
* A `state_changed` column, a datetime, representing when that state was entered
* A `state_next` column, a datetime, representing when it should next be checked

It may also define:

* A `state_history` column, a nullable JSON list, which will have every state
  change appended to it as a `[state, timestamp]` pair.

It must also have a defined State Graph, which outlines the valid values of
`state` and how to transition between them. Each state must either have:

 * A *transition function*, which is run when the model instance is in that
   state to see if it should move to a new state. These are expected to be on
   the state graph class itself, and be called `check_statename`.

 * `externally_progressed` set, marking it as not moving out of that state due
   to Stator; some other process will move it out if required.

It can also optionally have:

* `start_after`, the number of seconds to wait after entering the state before
  trying the *transition function* for it.

* `retry_after`, the number of seconds to wait between tries of the state
  transition function.

* `delete_after`, the number of seconds to wait before deleting an instance
  in this state.

When a Stator runner needs to find instances of the model it should run, it:

* Finds a suitable batch of instances that have `state_next <= now`, and in one
  `UPDATE RETURNING` statement, updates `state_next` to be two minutes in
  the future (or whatever double the *task deadline duration* is)

* Hands these instances to its worker threads, each of which runs one
  instance's transition function at a time to see if it should transition to a
  new state.

* If the function does trigger a transition, it updates `state` to the new
  state, `state_changed` to the current time, and `state_next` to be `start_after`
  seconds in the future, as defined on the state.

* If the function does not trigger a transition, it updates `state_next` to be
  `retry_after` seconds in the future.

* If the function takes longer than the *task deadline duration* to finish, it
  is killed and `state_next` is updated to be `retry_after` seconds in the
  future.

* An entry is added to the `StatorLog` model every so often, summarising what
