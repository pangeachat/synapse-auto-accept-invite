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
import asyncio
from asyncio import Future
from typing import Any, Awaitable, Dict, Optional, TypeVar
from unittest.mock import Mock

import attr
from synapse.module_api import ModuleApi

from synapse_auto_accept_invite import InviteAutoAccepter


@attr.s(auto_attribs=True)
class MockEvent:
    """Mocks an event. Only exposes properties the module uses."""

    sender: str
    type: str
    content: Dict[str, Any]
    room_id: str = "!someroom"
    state_key: Optional[str] = None
    origin_server_ts: Optional[int] = None

    def is_state(self) -> bool:
        """Checks if the event is a state event by checking if it has a state key."""
        return self.state_key is not None

    @property
    def membership(self) -> str:
        """Extracts the membership from the event. Should only be called on an event
        that's a membership event, and will raise a KeyError otherwise.
        """
        membership: str = self.content["membership"]
        return membership


T = TypeVar("T")
TV = TypeVar("TV")


async def make_awaitable(value: T) -> T:
    return value


def make_multiple_awaitable(result: TV) -> Awaitable[TV]:
    """
    Makes an awaitable, suitable for mocking an `async` function.
    This uses Futures as they can be awaited multiple times so can be returned
    to multiple callers. Stolen from synapse.
    """
    future: Future[TV] = Future()
    future.set_result(result)
    return future


def create_module(
    config_override: Dict[str, Any] = {}, worker_name: Optional[str] = None
) -> InviteAutoAccepter:
    # Create a mock based on the ModuleApi spec, but override some mocked functions
    # because some capabilities are needed for running the tests.
    module_api = Mock(spec=ModuleApi)
    module_api.is_mine.side_effect = lambda a: a.split(":")[1] == "test"
    module_api.worker_name = worker_name
    module_api.sleep.return_value = make_multiple_awaitable(None)
    # Add get_room_state_events method to the mock (not in the original spec)
    module_api.get_room_state_events = Mock()
    module_api.get_room_state_events.return_value = make_awaitable([])

    config = InviteAutoAccepter.parse_config(config_override)

    run_as_background_process = Mock()
    run_as_background_process.side_effect = (
        lambda desc, func, *args, bg_start_span, **kwargs: asyncio.create_task(
            func(*args, **kwargs)
        )
    )

    return InviteAutoAccepter(config, module_api)
