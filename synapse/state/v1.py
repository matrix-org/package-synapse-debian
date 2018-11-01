# -*- coding: utf-8 -*-
# Copyright 2018 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import hashlib
import logging

from six import iteritems, iterkeys, itervalues

from twisted.internet import defer

from synapse import event_auth
from synapse.api.constants import EventTypes
from synapse.api.errors import AuthError

logger = logging.getLogger(__name__)


POWER_KEY = (EventTypes.PowerLevels, "")


@defer.inlineCallbacks
def resolve_events_with_store(state_sets, event_map, state_map_factory):
    """
    Args:
        state_sets(list): List of dicts of (type, state_key) -> event_id,
            which are the different state groups to resolve.

        event_map(dict[str,FrozenEvent]|None):
            a dict from event_id to event, for any events that we happen to
            have in flight (eg, those currently being persisted). This will be
            used as a starting point fof finding the state we need; any missing
            events will be requested via state_map_factory.

            If None, all events will be fetched via state_map_factory.

        state_map_factory(func): will be called
            with a list of event_ids that are needed, and should return with
            a Deferred of dict of event_id to event.

    Returns
        Deferred[dict[(str, str), str]]:
            a map from (type, state_key) to event_id.
    """
    if len(state_sets) == 1:
        defer.returnValue(state_sets[0])

    unconflicted_state, conflicted_state = _seperate(
        state_sets,
    )

    needed_events = set(
        event_id
        for event_ids in itervalues(conflicted_state)
        for event_id in event_ids
    )
    needed_event_count = len(needed_events)
    if event_map is not None:
        needed_events -= set(iterkeys(event_map))

    logger.info(
        "Asking for %d/%d conflicted events",
        len(needed_events),
        needed_event_count,
    )

    # dict[str, FrozenEvent]: a map from state event id to event. Only includes
    # the state events which are in conflict (and those in event_map)
    state_map = yield state_map_factory(needed_events)
    if event_map is not None:
        state_map.update(event_map)

    # get the ids of the auth events which allow us to authenticate the
    # conflicted state, picking only from the unconflicting state.
    #
    # dict[(str, str), str]: a map from state key to event id
    auth_events = _create_auth_events_from_maps(
        unconflicted_state, conflicted_state, state_map
    )

    new_needed_events = set(itervalues(auth_events))
    new_needed_event_count = len(new_needed_events)
    new_needed_events -= needed_events
    if event_map is not None:
        new_needed_events -= set(iterkeys(event_map))

    logger.info(
        "Asking for %d/%d auth events",
        len(new_needed_events),
        new_needed_event_count,
    )

    state_map_new = yield state_map_factory(new_needed_events)
    state_map.update(state_map_new)

    defer.returnValue(_resolve_with_state(
        unconflicted_state, conflicted_state, auth_events, state_map
    ))


def _seperate(state_sets):
    """Takes the state_sets and figures out which keys are conflicted and
    which aren't. i.e., which have multiple different event_ids associated
    with them in different state sets.

    Args:
        state_sets(iterable[dict[(str, str), str]]):
            List of dicts of (type, state_key) -> event_id, which are the
            different state groups to resolve.

    Returns:
        (dict[(str, str), str], dict[(str, str), set[str]]):
            A tuple of (unconflicted_state, conflicted_state), where:

            unconflicted_state is a dict mapping (type, state_key)->event_id
            for unconflicted state keys.

            conflicted_state is a dict mapping (type, state_key) to a set of
            event ids for conflicted state keys.
    """
    state_set_iterator = iter(state_sets)
    unconflicted_state = dict(next(state_set_iterator))
    conflicted_state = {}

    for state_set in state_set_iterator:
        for key, value in iteritems(state_set):
            # Check if there is an unconflicted entry for the state key.
            unconflicted_value = unconflicted_state.get(key)
            if unconflicted_value is None:
                # There isn't an unconflicted entry so check if there is a
                # conflicted entry.
                ls = conflicted_state.get(key)
                if ls is None:
                    # There wasn't a conflicted entry so haven't seen this key before.
                    # Therefore it isn't conflicted yet.
                    unconflicted_state[key] = value
                else:
                    # This key is already conflicted, add our value to the conflict set.
                    ls.add(value)
            elif unconflicted_value != value:
                # If the unconflicted value is not the same as our value then we
                # have a new conflict. So move the key from the unconflicted_state
                # to the conflicted state.
                conflicted_state[key] = {value, unconflicted_value}
                unconflicted_state.pop(key, None)

    return unconflicted_state, conflicted_state


def _create_auth_events_from_maps(unconflicted_state, conflicted_state, state_map):
    auth_events = {}
    for event_ids in itervalues(conflicted_state):
        for event_id in event_ids:
            if event_id in state_map:
                keys = event_auth.auth_types_for_event(state_map[event_id])
                for key in keys:
                    if key not in auth_events:
                        event_id = unconflicted_state.get(key, None)
                        if event_id:
                            auth_events[key] = event_id
    return auth_events


def _resolve_with_state(unconflicted_state_ids, conflicted_state_ids, auth_event_ids,
                        state_map):
    conflicted_state = {}
    for key, event_ids in iteritems(conflicted_state_ids):
        events = [state_map[ev_id] for ev_id in event_ids if ev_id in state_map]
        if len(events) > 1:
            conflicted_state[key] = events
        elif len(events) == 1:
            unconflicted_state_ids[key] = events[0].event_id

    auth_events = {
        key: state_map[ev_id]
        for key, ev_id in iteritems(auth_event_ids)
        if ev_id in state_map
    }

    try:
        resolved_state = _resolve_state_events(
            conflicted_state, auth_events
        )
    except Exception:
        logger.exception("Failed to resolve state")
        raise

    new_state = unconflicted_state_ids
    for key, event in iteritems(resolved_state):
        new_state[key] = event.event_id

    return new_state


def _resolve_state_events(conflicted_state, auth_events):
    """ This is where we actually decide which of the conflicted state to
    use.

    We resolve conflicts in the following order:
        1. power levels
        2. join rules
        3. memberships
        4. other events.
    """
    resolved_state = {}
    if POWER_KEY in conflicted_state:
        events = conflicted_state[POWER_KEY]
        logger.debug("Resolving conflicted power levels %r", events)
        resolved_state[POWER_KEY] = _resolve_auth_events(
            events, auth_events)

    auth_events.update(resolved_state)

    for key, events in iteritems(conflicted_state):
        if key[0] == EventTypes.JoinRules:
            logger.debug("Resolving conflicted join rules %r", events)
            resolved_state[key] = _resolve_auth_events(
                events,
                auth_events
            )

    auth_events.update(resolved_state)

    for key, events in iteritems(conflicted_state):
        if key[0] == EventTypes.Member:
            logger.debug("Resolving conflicted member lists %r", events)
            resolved_state[key] = _resolve_auth_events(
                events,
                auth_events
            )

    auth_events.update(resolved_state)

    for key, events in iteritems(conflicted_state):
        if key not in resolved_state:
            logger.debug("Resolving conflicted state %r:%r", key, events)
            resolved_state[key] = _resolve_normal_events(
                events, auth_events
            )

    return resolved_state


def _resolve_auth_events(events, auth_events):
    reverse = [i for i in reversed(_ordered_events(events))]

    auth_keys = set(
        key
        for event in events
        for key in event_auth.auth_types_for_event(event)
    )

    new_auth_events = {}
    for key in auth_keys:
        auth_event = auth_events.get(key, None)
        if auth_event:
            new_auth_events[key] = auth_event

    auth_events = new_auth_events

    prev_event = reverse[0]
    for event in reverse[1:]:
        auth_events[(prev_event.type, prev_event.state_key)] = prev_event
        try:
            # The signatures have already been checked at this point
            event_auth.check(event, auth_events, do_sig_check=False, do_size_check=False)
            prev_event = event
        except AuthError:
            return prev_event

    return event


def _resolve_normal_events(events, auth_events):
    for event in _ordered_events(events):
        try:
            # The signatures have already been checked at this point
            event_auth.check(event, auth_events, do_sig_check=False, do_size_check=False)
            return event
        except AuthError:
            pass

    # Use the last event (the one with the least depth) if they all fail
    # the auth check.
    return event


def _ordered_events(events):
    def key_func(e):
        return -int(e.depth), hashlib.sha1(e.event_id.encode('ascii')).hexdigest()

    return sorted(events, key=key_func)
