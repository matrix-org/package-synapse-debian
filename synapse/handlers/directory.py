# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
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


import logging
import string

from twisted.internet import defer

from synapse.api.constants import EventTypes
from synapse.api.errors import (
    AuthError,
    CodeMessageException,
    Codes,
    NotFoundError,
    StoreError,
    SynapseError,
)
from synapse.types import RoomAlias, UserID, get_domain_from_id

from ._base import BaseHandler

logger = logging.getLogger(__name__)


class DirectoryHandler(BaseHandler):

    def __init__(self, hs):
        super(DirectoryHandler, self).__init__(hs)

        self.state = hs.get_state_handler()
        self.appservice_handler = hs.get_application_service_handler()
        self.event_creation_handler = hs.get_event_creation_handler()
        self.config = hs.config

        self.federation = hs.get_federation_client()
        hs.get_federation_registry().register_query_handler(
            "directory", self.on_directory_query
        )

        self.spam_checker = hs.get_spam_checker()

    @defer.inlineCallbacks
    def _create_association(self, room_alias, room_id, servers=None, creator=None):
        # general association creation for both human users and app services

        for wchar in string.whitespace:
                if wchar in room_alias.localpart:
                    raise SynapseError(400, "Invalid characters in room alias")

        if not self.hs.is_mine(room_alias):
            raise SynapseError(400, "Room alias must be local")
            # TODO(erikj): Change this.

        # TODO(erikj): Add transactions.
        # TODO(erikj): Check if there is a current association.
        if not servers:
            users = yield self.state.get_current_user_in_room(room_id)
            servers = set(get_domain_from_id(u) for u in users)

        if not servers:
            raise SynapseError(400, "Failed to get server list")

        yield self.store.create_room_alias_association(
            room_alias,
            room_id,
            servers,
            creator=creator,
        )

    @defer.inlineCallbacks
    def create_association(self, requester, room_alias, room_id, servers=None,
                           send_event=True):
        """Attempt to create a new alias

        Args:
            requester (Requester)
            room_alias (RoomAlias)
            room_id (str)
            servers (list[str]|None): List of servers that others servers
                should try and join via
            send_event (bool): Whether to send an updated m.room.aliases event

        Returns:
            Deferred
        """

        user_id = requester.user.to_string()

        service = requester.app_service
        if service:
            if not service.is_interested_in_alias(room_alias.to_string()):
                raise SynapseError(
                    400, "This application service has not reserved"
                    " this kind of alias.", errcode=Codes.EXCLUSIVE
                )
        else:
            if not self.spam_checker.user_may_create_room_alias(user_id, room_alias):
                raise AuthError(
                    403, "This user is not permitted to create this alias",
                )

            if not self.config.is_alias_creation_allowed(user_id, room_alias.to_string()):
                # Lets just return a generic message, as there may be all sorts of
                # reasons why we said no. TODO: Allow configurable error messages
                # per alias creation rule?
                raise SynapseError(
                    403, "Not allowed to create alias",
                )

            can_create = yield self.can_modify_alias(
                room_alias,
                user_id=user_id
            )
            if not can_create:
                raise AuthError(
                    400, "This alias is reserved by an application service.",
                    errcode=Codes.EXCLUSIVE
                )

        yield self._create_association(room_alias, room_id, servers, creator=user_id)
        if send_event:
            yield self.send_room_alias_update_event(
                requester,
                room_id
            )

    @defer.inlineCallbacks
    def delete_association(self, requester, room_alias, send_event=True):
        """Remove an alias from the directory

        (this is only meant for human users; AS users should call
        delete_appservice_association)

        Args:
            requester (Requester):
            room_alias (RoomAlias):
            send_event (bool): Whether to send an updated m.room.aliases event.
                Note that, if we delete the canonical alias, we will always attempt
                to send an m.room.canonical_alias event

        Returns:
            Deferred[unicode]: room id that the alias used to point to

        Raises:
            NotFoundError: if the alias doesn't exist

            AuthError: if the user doesn't have perms to delete the alias (ie, the user
                is neither the creator of the alias, nor a server admin.

            SynapseError: if the alias belongs to an AS
        """
        user_id = requester.user.to_string()

        try:
            can_delete = yield self._user_can_delete_alias(room_alias, user_id)
        except StoreError as e:
            if e.code == 404:
                raise NotFoundError("Unknown room alias")
            raise

        if not can_delete:
            raise AuthError(
                403, "You don't have permission to delete the alias.",
            )

        can_delete = yield self.can_modify_alias(
            room_alias,
            user_id=user_id
        )
        if not can_delete:
            raise SynapseError(
                400, "This alias is reserved by an application service.",
                errcode=Codes.EXCLUSIVE
            )

        room_id = yield self._delete_association(room_alias)

        try:
            if send_event:
                yield self.send_room_alias_update_event(
                    requester,
                    room_id
                )

            yield self._update_canonical_alias(
                requester,
                requester.user.to_string(),
                room_id,
                room_alias,
            )
        except AuthError as e:
            logger.info("Failed to update alias events: %s", e)

        defer.returnValue(room_id)

    @defer.inlineCallbacks
    def delete_appservice_association(self, service, room_alias):
        if not service.is_interested_in_alias(room_alias.to_string()):
            raise SynapseError(
                400,
                "This application service has not reserved this kind of alias",
                errcode=Codes.EXCLUSIVE
            )
        yield self._delete_association(room_alias)

    @defer.inlineCallbacks
    def _delete_association(self, room_alias):
        if not self.hs.is_mine(room_alias):
            raise SynapseError(400, "Room alias must be local")

        room_id = yield self.store.delete_room_alias(room_alias)

        defer.returnValue(room_id)

    @defer.inlineCallbacks
    def get_association(self, room_alias):
        room_id = None
        if self.hs.is_mine(room_alias):
            result = yield self.get_association_from_room_alias(
                room_alias
            )

            if result:
                room_id = result.room_id
                servers = result.servers
        else:
            try:
                result = yield self.federation.make_query(
                    destination=room_alias.domain,
                    query_type="directory",
                    args={
                        "room_alias": room_alias.to_string(),
                    },
                    retry_on_dns_fail=False,
                    ignore_backoff=True,
                )
            except CodeMessageException as e:
                logging.warn("Error retrieving alias")
                if e.code == 404:
                    result = None
                else:
                    raise

            if result and "room_id" in result and "servers" in result:
                room_id = result["room_id"]
                servers = result["servers"]

        if not room_id:
            raise SynapseError(
                404,
                "Room alias %s not found" % (room_alias.to_string(),),
                Codes.NOT_FOUND
            )

        users = yield self.state.get_current_user_in_room(room_id)
        extra_servers = set(get_domain_from_id(u) for u in users)
        servers = set(extra_servers) | set(servers)

        # If this server is in the list of servers, return it first.
        if self.server_name in servers:
            servers = (
                [self.server_name] +
                [s for s in servers if s != self.server_name]
            )
        else:
            servers = list(servers)

        defer.returnValue({
            "room_id": room_id,
            "servers": servers,
        })
        return

    @defer.inlineCallbacks
    def on_directory_query(self, args):
        room_alias = RoomAlias.from_string(args["room_alias"])
        if not self.hs.is_mine(room_alias):
            raise SynapseError(
                400, "Room Alias is not hosted on this Home Server"
            )

        result = yield self.get_association_from_room_alias(
            room_alias
        )

        if result is not None:
            defer.returnValue({
                "room_id": result.room_id,
                "servers": result.servers,
            })
        else:
            raise SynapseError(
                404,
                "Room alias %r not found" % (room_alias.to_string(),),
                Codes.NOT_FOUND
            )

    @defer.inlineCallbacks
    def send_room_alias_update_event(self, requester, room_id):
        aliases = yield self.store.get_aliases_for_room(room_id)

        yield self.event_creation_handler.create_and_send_nonmember_event(
            requester,
            {
                "type": EventTypes.Aliases,
                "state_key": self.hs.hostname,
                "room_id": room_id,
                "sender": requester.user.to_string(),
                "content": {"aliases": aliases},
            },
            ratelimit=False
        )

    @defer.inlineCallbacks
    def _update_canonical_alias(self, requester, user_id, room_id, room_alias):
        alias_event = yield self.state.get_current_state(
            room_id, EventTypes.CanonicalAlias, ""
        )

        alias_str = room_alias.to_string()
        if not alias_event or alias_event.content.get("alias", "") != alias_str:
            return

        yield self.event_creation_handler.create_and_send_nonmember_event(
            requester,
            {
                "type": EventTypes.CanonicalAlias,
                "state_key": "",
                "room_id": room_id,
                "sender": user_id,
                "content": {},
            },
            ratelimit=False
        )

    @defer.inlineCallbacks
    def get_association_from_room_alias(self, room_alias):
        result = yield self.store.get_association_from_room_alias(
            room_alias
        )
        if not result:
            # Query AS to see if it exists
            as_handler = self.appservice_handler
            result = yield as_handler.query_room_alias_exists(room_alias)
        defer.returnValue(result)

    def can_modify_alias(self, alias, user_id=None):
        # Any application service "interested" in an alias they are regexing on
        # can modify the alias.
        # Users can only modify the alias if ALL the interested services have
        # non-exclusive locks on the alias (or there are no interested services)
        services = self.store.get_app_services()
        interested_services = [
            s for s in services if s.is_interested_in_alias(alias.to_string())
        ]

        for service in interested_services:
            if user_id == service.sender:
                # this user IS the app service so they can do whatever they like
                return defer.succeed(True)
            elif service.is_exclusive_alias(alias.to_string()):
                # another service has an exclusive lock on this alias.
                return defer.succeed(False)
        # either no interested services, or no service with an exclusive lock
        return defer.succeed(True)

    @defer.inlineCallbacks
    def _user_can_delete_alias(self, alias, user_id):
        creator = yield self.store.get_room_alias_creator(alias.to_string())

        if creator is not None and creator == user_id:
            defer.returnValue(True)

        is_admin = yield self.auth.is_server_admin(UserID.from_string(user_id))
        defer.returnValue(is_admin)

    @defer.inlineCallbacks
    def edit_published_room_list(self, requester, room_id, visibility):
        """Edit the entry of the room in the published room list.

        requester
        room_id (str)
        visibility (str): "public" or "private"
        """
        if not self.spam_checker.user_may_publish_room(
            requester.user.to_string(), room_id
        ):
            raise AuthError(
                403,
                "This user is not permitted to publish rooms to the room list"
            )

        if requester.is_guest:
            raise AuthError(403, "Guests cannot edit the published room list")

        if visibility not in ["public", "private"]:
            raise SynapseError(400, "Invalid visibility setting")

        room = yield self.store.get_room(room_id)
        if room is None:
            raise SynapseError(400, "Unknown room")

        yield self.auth.check_can_change_room_list(room_id, requester.user)

        yield self.store.set_room_is_public(room_id, visibility == "public")

    @defer.inlineCallbacks
    def edit_published_appservice_room_list(self, appservice_id, network_id,
                                            room_id, visibility):
        """Add or remove a room from the appservice/network specific public
        room list.

        Args:
            appservice_id (str): ID of the appservice that owns the list
            network_id (str): The ID of the network the list is associated with
            room_id (str)
            visibility (str): either "public" or "private"
        """
        if visibility not in ["public", "private"]:
            raise SynapseError(400, "Invalid visibility setting")

        yield self.store.set_room_is_public_appservice(
            room_id, appservice_id, network_id, visibility == "public"
        )
