# Copyright 2021 The Matrix.org Foundation C.I.C.
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
from typing import Any, Dict, Optional, Tuple

import attr
from synapse.module_api import EventBase, ModuleApi, run_as_background_process

logger = logging.getLogger(__name__)
ACCOUNT_DATA_DIRECT_MESSAGE_LIST = "m.direct"


@attr.s(auto_attribs=True, frozen=True)
class InviteAutoAccepterConfig:
    accept_invites_only_for_direct_messages: bool = False
    accept_invites_only_from_local_users: bool = False
    accept_invites_only_from_previously_knocked_rooms: bool = False
    worker_to_run_on: Optional[str] = None


class InviteAutoAccepter:
    def __init__(self, config: InviteAutoAccepterConfig, api: ModuleApi):
        # Keep a reference to the Module API.
        self._api = api
        self._config = config

        should_run_on_this_worker = config.worker_to_run_on == self._api.worker_name

        if not should_run_on_this_worker:
            logger.info(
                "Not accepting invites on this worker (configured: %r, here: %r)",
                config.worker_to_run_on,
                self._api.worker_name,
            )
            return

        logger.info(
            "Accepting invites on this worker (here: %r)", self._api.worker_name
        )

        # Register the callback.
        self._api.register_third_party_rules_callbacks(
            on_new_event=self.on_new_event,
        )

    @staticmethod
    def parse_config(config: Dict[str, Any]) -> InviteAutoAccepterConfig:
        """Checks that the required fields are present and at a correct value, and
        instantiates a InviteAutoAccepterConfig.

        Args:
            config: The raw configuration dict.

        Returns:
            A InviteAutoAccepterConfig generated from this configuration
        """
        accept_invites_only_for_direct_messages = config.get(
            "accept_invites_only_for_direct_messages", False
        )
        accept_invites_only_from_local_users = config.get(
            "accept_invites_only_from_local_users", False
        )
        accept_invites_only_from_previously_knocked_rooms = config.get(
            "accept_invites_only_from_previously_knocked_rooms", False
        )

        worker_to_run_on = config.get("worker_to_run_on", None)

        return InviteAutoAccepterConfig(
            accept_invites_only_for_direct_messages=accept_invites_only_for_direct_messages,
            accept_invites_only_from_local_users=accept_invites_only_from_local_users,
            accept_invites_only_from_previously_knocked_rooms=accept_invites_only_from_previously_knocked_rooms,
            worker_to_run_on=worker_to_run_on,
        )

    async def on_new_event(self, event: EventBase, *args: Any) -> None:
        """Listens for new events, and if the event is an invite for a local user then
        automatically accepts it.

        Args:
            event: The incoming event.
        """
        # Check if the event is an invite for a local user.
        is_invite_for_local_user = (
            event.type == "m.room.member"
            and event.is_state()
            and event.membership == "invite"
            and self._api.is_mine(event.state_key)
        )

        # Only accept invites for direct messages if the configuration mandates it.
        is_direct_message = event.content.get("is_direct", False)
        is_allowed_by_direct_message_rules = (
            not self._config.accept_invites_only_for_direct_messages
            or is_direct_message is True
        )

        # Only accept invites from remote users if the configuration mandates it.
        is_from_local_user = self._api.is_mine(event.sender)
        is_allowed_by_local_user_rules = (
            not self._config.accept_invites_only_from_local_users
            or is_from_local_user is True
        )

        # Only accept invitations which the requester previously knocked if the configuration mandates it.
        is_allowed_by_knock_rules = True
        if self._config.accept_invites_only_from_previously_knocked_rooms:
            is_allowed_by_knock_rules = await self._has_user_previously_knocked(
                event.sender, event.room_id
            )

        if (
            is_invite_for_local_user
            and is_allowed_by_direct_message_rules
            and is_allowed_by_local_user_rules
            and is_allowed_by_knock_rules
        ):
            # Make the user join the room. We run this as a background process to circumvent a race condition
            # that occurs when responding to invites over federation (see https://github.com/matrix-org/synapse-auto-accept-invite/issues/12)
            run_as_background_process(
                "retry_make_join",
                self._retry_make_join,
                event.state_key,
                event.state_key,
                event.room_id,
                "join",
                bg_start_span=False,
            )

            if is_direct_message:
                # Mark this room as a direct message!
                await self._mark_room_as_direct_message(
                    event.state_key, event.sender, event.room_id
                )

    async def _mark_room_as_direct_message(
        self, user_id: str, dm_user_id: str, room_id: str
    ) -> None:
        """
        Marks a room (`room_id`) as a direct message with the counterparty `dm_user_id`
        from the perspective of the user `user_id`.
        """

        # This is a dict of User IDs to tuples of Room IDs
        # (get_global will return a frozendict of tuples as it freezes the data,
        # but we should accept either frozen or unfrozen variants.)
        # Be careful: we convert the outer frozendict into a dict here,
        # but the contents of the dict are still frozen (tuples in lieu of lists,
        # etc.)
        dm_map: Dict[str, Tuple[str, ...]] = dict(
            await self._api.account_data_manager.get_global(
                user_id, ACCOUNT_DATA_DIRECT_MESSAGE_LIST
            )
            or {}
        )

        if dm_user_id not in dm_map:
            dm_map[dm_user_id] = (room_id,)
        else:
            dm_rooms_for_user = dm_map[dm_user_id]
            if not isinstance(dm_rooms_for_user, (tuple, list)):
                # Don't mangle the data if we don't understand it.
                logger.warning(
                    "Not marking room as DM for auto-accepted invitation; "
                    "dm_map[%r] is a %s not a list.",
                    type(dm_rooms_for_user),
                    dm_user_id,
                )
                return

            dm_map[dm_user_id] = tuple(dm_rooms_for_user) + (room_id,)

        await self._api.account_data_manager.put_global(
            user_id, ACCOUNT_DATA_DIRECT_MESSAGE_LIST, dm_map
        )

    async def _has_user_previously_knocked(self, user_id: str, room_id: str) -> bool:
        """
        Check if a user has previously knocked on a room by looking at room membership history.

        Args:
            user_id: The user ID to check for previous knocks
            room_id: The room ID to check knock history for

        Returns:
            True if the user's most recent membership event is a "knock", False otherwise
        """
        try:
            # Get the membership events for this user in this room
            membership_events = await self._api.get_state_events_in_room(
                room_id, types=[("m.room.member", user_id)]
            )

            if not membership_events:
                return False

            # Sort events by origin_server_ts to find the most recent one
            # Events should have an origin_server_ts attribute for ordering
            sorted_events = sorted(
                membership_events,
                key=lambda event: getattr(event, "origin_server_ts", 0),
                reverse=True,
            )

            # Check if the most recent event is a "knock"
            most_recent_event = sorted_events[0]

            if (
                hasattr(most_recent_event, "membership")
                and most_recent_event.membership == "knock"
            ):
                return True
            elif (
                hasattr(most_recent_event, "content")
                and most_recent_event.content.get("membership") == "knock"
            ):
                return True

            return False
        except Exception as e:
            # If we can't determine knock history, err on the side of caution
            logger.warning(
                "Unable to determine knock history for user %s in room %s: %s",
                user_id,
                room_id,
                e,
            )
            return False

    async def _retry_make_join(
        self, sender: str, target: str, room_id: str, new_membership: str
    ) -> None:
        """
        A function to retry sending the `make_join` request with an increasing backoff. This is
        implemented to work around a race condition when receiving invites over federation.

        Args:
            sender: the user performing the membership change
            target: the for whom the membership is changing
            room_id: room id of the room to join to
            new_membership: the type of membership event (in this case will be "join")
        """

        sleep = 0
        retries = 0
        join_event = None

        while retries < 5:
            try:
                await self._api.sleep(sleep)
                join_event = await self._api.update_room_membership(
                    sender=sender,
                    target=target,
                    room_id=room_id,
                    new_membership=new_membership,
                )
            except Exception as e:
                logger.info(
                    f"Update_room_membership raised the following exception: {e}"
                )
                sleep = 2**retries
                retries += 1

            if join_event is not None:
                break
