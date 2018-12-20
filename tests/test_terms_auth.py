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

import json

import six
from mock import Mock

from twisted.test.proto_helpers import MemoryReactorClock

from synapse.rest.client.v2_alpha.register import register_servlets
from synapse.util import Clock

from tests import unittest


class TermsTestCase(unittest.HomeserverTestCase):
    servlets = [register_servlets]

    def prepare(self, reactor, clock, hs):
        self.clock = MemoryReactorClock()
        self.hs_clock = Clock(self.clock)
        self.url = "/_matrix/client/r0/register"
        self.registration_handler = Mock()
        self.auth_handler = Mock()
        self.device_handler = Mock()
        hs.config.enable_registration = True
        hs.config.registrations_require_3pid = []
        hs.config.auto_join_rooms = []
        hs.config.enable_registration_captcha = False

    def test_ui_auth(self):
        self.hs.config.user_consent_at_registration = True
        self.hs.config.user_consent_policy_name = "My Cool Privacy Policy"
        self.hs.config.public_baseurl = "https://example.org/"
        self.hs.config.user_consent_version = "1.0"

        # Do a UI auth request
        request, channel = self.make_request(b"POST", self.url, b"{}")
        self.render(request)

        self.assertEquals(channel.result["code"], b"401", channel.result)

        self.assertTrue(channel.json_body is not None)
        self.assertIsInstance(channel.json_body["session"], six.text_type)

        self.assertIsInstance(channel.json_body["flows"], list)
        for flow in channel.json_body["flows"]:
            self.assertIsInstance(flow["stages"], list)
            self.assertTrue(len(flow["stages"]) > 0)
            self.assertEquals(flow["stages"][-1], "m.login.terms")

        expected_params = {
            "m.login.terms": {
                "policies": {
                    "privacy_policy": {
                        "en": {
                            "name": "My Cool Privacy Policy",
                            "url": "https://example.org/_matrix/consent?v=1.0",
                        },
                        "version": "1.0"
                    },
                },
            },
        }
        self.assertIsInstance(channel.json_body["params"], dict)
        self.assertDictContainsSubset(channel.json_body["params"], expected_params)

        # We have to complete the dummy auth stage before completing the terms stage
        request_data = json.dumps(
            {
                "username": "kermit",
                "password": "monkey",
                "auth": {
                    "session": channel.json_body["session"],
                    "type": "m.login.dummy",
                },
            }
        )

        self.registration_handler.check_username = Mock(return_value=True)

        request, channel = self.make_request(b"POST", self.url, request_data)
        self.render(request)

        # We don't bother checking that the response is correct - we'll leave that to
        # other tests. We just want to make sure we're on the right path.
        self.assertEquals(channel.result["code"], b"401", channel.result)

        # Finish the UI auth for terms
        request_data = json.dumps(
            {
                "username": "kermit",
                "password": "monkey",
                "auth": {
                    "session": channel.json_body["session"],
                    "type": "m.login.terms",
                },
            }
        )
        request, channel = self.make_request(b"POST", self.url, request_data)
        self.render(request)

        # We're interested in getting a response that looks like a successful
        # registration, not so much that the details are exactly what we want.

        self.assertEquals(channel.result["code"], b"200", channel.result)

        self.assertTrue(channel.json_body is not None)
        self.assertIsInstance(channel.json_body["user_id"], six.text_type)
        self.assertIsInstance(channel.json_body["access_token"], six.text_type)
        self.assertIsInstance(channel.json_body["device_id"], six.text_type)
