# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
# Copyright 2017, 2018 New Vector Ltd.
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
from collections import namedtuple

from signedjson.key import (
    decode_verify_key_bytes,
    encode_verify_key_base64,
    is_signing_algorithm_supported,
)
from signedjson.sign import (
    SignatureVerifyException,
    encode_canonical_json,
    sign_json,
    signature_ids,
    verify_signed_json,
)
from unpaddedbase64 import decode_base64, encode_base64

from OpenSSL import crypto
from twisted.internet import defer

from synapse.api.errors import Codes, SynapseError
from synapse.crypto.keyclient import fetch_server_key
from synapse.util import logcontext, unwrapFirstError
from synapse.util.logcontext import (
    LoggingContext,
    PreserveLoggingContext,
    preserve_fn,
    run_in_background,
)
from synapse.util.metrics import Measure

logger = logging.getLogger(__name__)


VerifyKeyRequest = namedtuple("VerifyRequest", (
    "server_name", "key_ids", "json_object", "deferred"
))
"""
A request for a verify key to verify a JSON object.

Attributes:
    server_name(str): The name of the server to verify against.
    key_ids(set(str)): The set of key_ids to that could be used to verify the
        JSON object
    json_object(dict): The JSON object to verify.
    deferred(Deferred[str, str, nacl.signing.VerifyKey]):
        A deferred (server_name, key_id, verify_key) tuple that resolves when
        a verify key has been fetched. The deferreds' callbacks are run with no
        logcontext.
"""


class KeyLookupError(ValueError):
    pass


class Keyring(object):
    def __init__(self, hs):
        self.store = hs.get_datastore()
        self.clock = hs.get_clock()
        self.client = hs.get_http_client()
        self.config = hs.get_config()
        self.perspective_servers = self.config.perspectives
        self.hs = hs

        # map from server name to Deferred. Has an entry for each server with
        # an ongoing key download; the Deferred completes once the download
        # completes.
        #
        # These are regular, logcontext-agnostic Deferreds.
        self.key_downloads = {}

    def verify_json_for_server(self, server_name, json_object):
        return logcontext.make_deferred_yieldable(
            self.verify_json_objects_for_server(
                [(server_name, json_object)]
            )[0]
        )

    def verify_json_objects_for_server(self, server_and_json):
        """Bulk verifies signatures of json objects, bulk fetching keys as
        necessary.

        Args:
            server_and_json (list): List of pairs of (server_name, json_object)

        Returns:
            List<Deferred>: for each input pair, a deferred indicating success
                or failure to verify each json object's signature for the given
                server_name. The deferreds run their callbacks in the sentinel
                logcontext.
        """
        verify_requests = []

        for server_name, json_object in server_and_json:

            key_ids = signature_ids(json_object, server_name)
            if not key_ids:
                logger.warn("Request from %s: no supported signature keys",
                            server_name)
                deferred = defer.fail(SynapseError(
                    400,
                    "Not signed with a supported algorithm",
                    Codes.UNAUTHORIZED,
                ))
            else:
                deferred = defer.Deferred()

            logger.debug("Verifying for %s with key_ids %s",
                         server_name, key_ids)

            verify_request = VerifyKeyRequest(
                server_name, key_ids, json_object, deferred
            )

            verify_requests.append(verify_request)

        run_in_background(self._start_key_lookups, verify_requests)

        # Pass those keys to handle_key_deferred so that the json object
        # signatures can be verified
        handle = preserve_fn(_handle_key_deferred)
        return [
            handle(rq) for rq in verify_requests
        ]

    @defer.inlineCallbacks
    def _start_key_lookups(self, verify_requests):
        """Sets off the key fetches for each verify request

        Once each fetch completes, verify_request.deferred will be resolved.

        Args:
            verify_requests (List[VerifyKeyRequest]):
        """

        try:
            # create a deferred for each server we're going to look up the keys
            # for; we'll resolve them once we have completed our lookups.
            # These will be passed into wait_for_previous_lookups to block
            # any other lookups until we have finished.
            # The deferreds are called with no logcontext.
            server_to_deferred = {
                rq.server_name: defer.Deferred()
                for rq in verify_requests
            }

            # We want to wait for any previous lookups to complete before
            # proceeding.
            yield self.wait_for_previous_lookups(
                [rq.server_name for rq in verify_requests],
                server_to_deferred,
            )

            # Actually start fetching keys.
            self._get_server_verify_keys(verify_requests)

            # When we've finished fetching all the keys for a given server_name,
            # resolve the deferred passed to `wait_for_previous_lookups` so that
            # any lookups waiting will proceed.
            #
            # map from server name to a set of request ids
            server_to_request_ids = {}

            for verify_request in verify_requests:
                server_name = verify_request.server_name
                request_id = id(verify_request)
                server_to_request_ids.setdefault(server_name, set()).add(request_id)

            def remove_deferreds(res, verify_request):
                server_name = verify_request.server_name
                request_id = id(verify_request)
                server_to_request_ids[server_name].discard(request_id)
                if not server_to_request_ids[server_name]:
                    d = server_to_deferred.pop(server_name, None)
                    if d:
                        d.callback(None)
                return res

            for verify_request in verify_requests:
                verify_request.deferred.addBoth(
                    remove_deferreds, verify_request,
                )
        except Exception:
            logger.exception("Error starting key lookups")

    @defer.inlineCallbacks
    def wait_for_previous_lookups(self, server_names, server_to_deferred):
        """Waits for any previous key lookups for the given servers to finish.

        Args:
            server_names (list): list of server_names we want to lookup
            server_to_deferred (dict): server_name to deferred which gets
                resolved once we've finished looking up keys for that server.
                The Deferreds should be regular twisted ones which call their
                callbacks with no logcontext.

        Returns: a Deferred which resolves once all key lookups for the given
            servers have completed. Follows the synapse rules of logcontext
            preservation.
        """
        loop_count = 1
        while True:
            wait_on = [
                (server_name, self.key_downloads[server_name])
                for server_name in server_names
                if server_name in self.key_downloads
            ]
            if not wait_on:
                break
            logger.info(
                "Waiting for existing lookups for %s to complete [loop %i]",
                [w[0] for w in wait_on], loop_count,
            )
            with PreserveLoggingContext():
                yield defer.DeferredList((w[1] for w in wait_on))

            loop_count += 1

        ctx = LoggingContext.current_context()

        def rm(r, server_name_):
            with PreserveLoggingContext(ctx):
                logger.debug("Releasing key lookup lock on %s", server_name_)
                self.key_downloads.pop(server_name_, None)
            return r

        for server_name, deferred in server_to_deferred.items():
            logger.debug("Got key lookup lock on %s", server_name)
            self.key_downloads[server_name] = deferred
            deferred.addBoth(rm, server_name)

    def _get_server_verify_keys(self, verify_requests):
        """Tries to find at least one key for each verify request

        For each verify_request, verify_request.deferred is called back with
        params (server_name, key_id, VerifyKey) if a key is found, or errbacked
        with a SynapseError if none of the keys are found.

        Args:
            verify_requests (list[VerifyKeyRequest]): list of verify requests
        """

        # These are functions that produce keys given a list of key ids
        key_fetch_fns = (
            self.get_keys_from_store,  # First try the local store
            self.get_keys_from_perspectives,  # Then try via perspectives
            self.get_keys_from_server,  # Then try directly
        )

        @defer.inlineCallbacks
        def do_iterations():
            with Measure(self.clock, "get_server_verify_keys"):
                # dict[str, dict[str, VerifyKey]]: results so far.
                # map server_name -> key_id -> VerifyKey
                merged_results = {}

                # dict[str, set(str)]: keys to fetch for each server
                missing_keys = {}
                for verify_request in verify_requests:
                    missing_keys.setdefault(verify_request.server_name, set()).update(
                        verify_request.key_ids
                    )

                for fn in key_fetch_fns:
                    results = yield fn(missing_keys.items())
                    merged_results.update(results)

                    # We now need to figure out which verify requests we have keys
                    # for and which we don't
                    missing_keys = {}
                    requests_missing_keys = []
                    for verify_request in verify_requests:
                        server_name = verify_request.server_name
                        result_keys = merged_results[server_name]

                        if verify_request.deferred.called:
                            # We've already called this deferred, which probably
                            # means that we've already found a key for it.
                            continue

                        for key_id in verify_request.key_ids:
                            if key_id in result_keys:
                                with PreserveLoggingContext():
                                    verify_request.deferred.callback((
                                        server_name,
                                        key_id,
                                        result_keys[key_id],
                                    ))
                                break
                        else:
                            # The else block is only reached if the loop above
                            # doesn't break.
                            missing_keys.setdefault(server_name, set()).update(
                                verify_request.key_ids
                            )
                            requests_missing_keys.append(verify_request)

                    if not missing_keys:
                        break

                with PreserveLoggingContext():
                    for verify_request in requests_missing_keys:
                        verify_request.deferred.errback(SynapseError(
                            401,
                            "No key for %s with id %s" % (
                                verify_request.server_name, verify_request.key_ids,
                            ),
                            Codes.UNAUTHORIZED,
                        ))

        def on_err(err):
            with PreserveLoggingContext():
                for verify_request in verify_requests:
                    if not verify_request.deferred.called:
                        verify_request.deferred.errback(err)

        run_in_background(do_iterations).addErrback(on_err)

    @defer.inlineCallbacks
    def get_keys_from_store(self, server_name_and_key_ids):
        """

        Args:
            server_name_and_key_ids (list[(str, iterable[str])]):
                list of (server_name, iterable[key_id]) tuples to fetch keys for

        Returns:
            Deferred: resolves to dict[str, dict[str, VerifyKey]]: map from
                server_name -> key_id -> VerifyKey
        """
        res = yield logcontext.make_deferred_yieldable(defer.gatherResults(
            [
                run_in_background(
                    self.store.get_server_verify_keys,
                    server_name, key_ids,
                ).addCallback(lambda ks, server: (server, ks), server_name)
                for server_name, key_ids in server_name_and_key_ids
            ],
            consumeErrors=True,
        ).addErrback(unwrapFirstError))

        defer.returnValue(dict(res))

    @defer.inlineCallbacks
    def get_keys_from_perspectives(self, server_name_and_key_ids):
        @defer.inlineCallbacks
        def get_key(perspective_name, perspective_keys):
            try:
                result = yield self.get_server_verify_key_v2_indirect(
                    server_name_and_key_ids, perspective_name, perspective_keys
                )
                defer.returnValue(result)
            except Exception as e:
                logger.exception(
                    "Unable to get key from %r: %s %s",
                    perspective_name,
                    type(e).__name__, str(e),
                )
                defer.returnValue({})

        results = yield logcontext.make_deferred_yieldable(defer.gatherResults(
            [
                run_in_background(get_key, p_name, p_keys)
                for p_name, p_keys in self.perspective_servers.items()
            ],
            consumeErrors=True,
        ).addErrback(unwrapFirstError))

        union_of_keys = {}
        for result in results:
            for server_name, keys in result.items():
                union_of_keys.setdefault(server_name, {}).update(keys)

        defer.returnValue(union_of_keys)

    @defer.inlineCallbacks
    def get_keys_from_server(self, server_name_and_key_ids):
        results = yield logcontext.make_deferred_yieldable(defer.gatherResults(
            [
                run_in_background(
                    self.get_server_verify_key_v2_direct,
                    server_name,
                    key_ids,
                )
                for server_name, key_ids in server_name_and_key_ids
            ],
            consumeErrors=True,
        ).addErrback(unwrapFirstError))

        merged = {}
        for result in results:
            merged.update(result)

        defer.returnValue({
            server_name: keys
            for server_name, keys in merged.items()
            if keys
        })

    @defer.inlineCallbacks
    def get_server_verify_key_v2_indirect(self, server_names_and_key_ids,
                                          perspective_name,
                                          perspective_keys):
        # TODO(mark): Set the minimum_valid_until_ts to that needed by
        # the events being validated or the current time if validating
        # an incoming request.
        query_response = yield self.client.post_json(
            destination=perspective_name,
            path="/_matrix/key/v2/query",
            data={
                u"server_keys": {
                    server_name: {
                        key_id: {
                            u"minimum_valid_until_ts": 0
                        } for key_id in key_ids
                    }
                    for server_name, key_ids in server_names_and_key_ids
                }
            },
            long_retries=True,
        )

        keys = {}

        responses = query_response["server_keys"]

        for response in responses:
            if (u"signatures" not in response
                    or perspective_name not in response[u"signatures"]):
                raise KeyLookupError(
                    "Key response not signed by perspective server"
                    " %r" % (perspective_name,)
                )

            verified = False
            for key_id in response[u"signatures"][perspective_name]:
                if key_id in perspective_keys:
                    verify_signed_json(
                        response,
                        perspective_name,
                        perspective_keys[key_id]
                    )
                    verified = True

            if not verified:
                logging.info(
                    "Response from perspective server %r not signed with a"
                    " known key, signed with: %r, known keys: %r",
                    perspective_name,
                    list(response[u"signatures"][perspective_name]),
                    list(perspective_keys)
                )
                raise KeyLookupError(
                    "Response not signed with a known key for perspective"
                    " server %r" % (perspective_name,)
                )

            processed_response = yield self.process_v2_response(
                perspective_name, response, only_from_server=False
            )

            for server_name, response_keys in processed_response.items():
                keys.setdefault(server_name, {}).update(response_keys)

        yield logcontext.make_deferred_yieldable(defer.gatherResults(
            [
                run_in_background(
                    self.store_keys,
                    server_name=server_name,
                    from_server=perspective_name,
                    verify_keys=response_keys,
                )
                for server_name, response_keys in keys.items()
            ],
            consumeErrors=True
        ).addErrback(unwrapFirstError))

        defer.returnValue(keys)

    @defer.inlineCallbacks
    def get_server_verify_key_v2_direct(self, server_name, key_ids):
        keys = {}

        for requested_key_id in key_ids:
            if requested_key_id in keys:
                continue

            (response, tls_certificate) = yield fetch_server_key(
                server_name, self.hs.tls_client_options_factory, requested_key_id
            )

            if (u"signatures" not in response
                    or server_name not in response[u"signatures"]):
                raise KeyLookupError("Key response not signed by remote server")

            if "tls_fingerprints" not in response:
                raise KeyLookupError("Key response missing TLS fingerprints")

            certificate_bytes = crypto.dump_certificate(
                crypto.FILETYPE_ASN1, tls_certificate
            )
            sha256_fingerprint = hashlib.sha256(certificate_bytes).digest()
            sha256_fingerprint_b64 = encode_base64(sha256_fingerprint)

            response_sha256_fingerprints = set()
            for fingerprint in response[u"tls_fingerprints"]:
                if u"sha256" in fingerprint:
                    response_sha256_fingerprints.add(fingerprint[u"sha256"])

            if sha256_fingerprint_b64 not in response_sha256_fingerprints:
                raise KeyLookupError("TLS certificate not allowed by fingerprints")

            response_keys = yield self.process_v2_response(
                from_server=server_name,
                requested_ids=[requested_key_id],
                response_json=response,
            )

            keys.update(response_keys)

        yield logcontext.make_deferred_yieldable(defer.gatherResults(
            [
                run_in_background(
                    self.store_keys,
                    server_name=key_server_name,
                    from_server=server_name,
                    verify_keys=verify_keys,
                )
                for key_server_name, verify_keys in keys.items()
            ],
            consumeErrors=True
        ).addErrback(unwrapFirstError))

        defer.returnValue(keys)

    @defer.inlineCallbacks
    def process_v2_response(self, from_server, response_json,
                            requested_ids=[], only_from_server=True):
        time_now_ms = self.clock.time_msec()
        response_keys = {}
        verify_keys = {}
        for key_id, key_data in response_json["verify_keys"].items():
            if is_signing_algorithm_supported(key_id):
                key_base64 = key_data["key"]
                key_bytes = decode_base64(key_base64)
                verify_key = decode_verify_key_bytes(key_id, key_bytes)
                verify_key.time_added = time_now_ms
                verify_keys[key_id] = verify_key

        old_verify_keys = {}
        for key_id, key_data in response_json["old_verify_keys"].items():
            if is_signing_algorithm_supported(key_id):
                key_base64 = key_data["key"]
                key_bytes = decode_base64(key_base64)
                verify_key = decode_verify_key_bytes(key_id, key_bytes)
                verify_key.expired = key_data["expired_ts"]
                verify_key.time_added = time_now_ms
                old_verify_keys[key_id] = verify_key

        results = {}
        server_name = response_json["server_name"]
        if only_from_server:
            if server_name != from_server:
                raise KeyLookupError(
                    "Expected a response for server %r not %r" % (
                        from_server, server_name
                    )
                )
        for key_id in response_json["signatures"].get(server_name, {}):
            if key_id not in response_json["verify_keys"]:
                raise KeyLookupError(
                    "Key response must include verification keys for all"
                    " signatures"
                )
            if key_id in verify_keys:
                verify_signed_json(
                    response_json,
                    server_name,
                    verify_keys[key_id]
                )

        signed_key_json = sign_json(
            response_json,
            self.config.server_name,
            self.config.signing_key[0],
        )

        signed_key_json_bytes = encode_canonical_json(signed_key_json)
        ts_valid_until_ms = signed_key_json[u"valid_until_ts"]

        updated_key_ids = set(requested_ids)
        updated_key_ids.update(verify_keys)
        updated_key_ids.update(old_verify_keys)

        response_keys.update(verify_keys)
        response_keys.update(old_verify_keys)

        yield logcontext.make_deferred_yieldable(defer.gatherResults(
            [
                run_in_background(
                    self.store.store_server_keys_json,
                    server_name=server_name,
                    key_id=key_id,
                    from_server=server_name,
                    ts_now_ms=time_now_ms,
                    ts_expires_ms=ts_valid_until_ms,
                    key_json_bytes=signed_key_json_bytes,
                )
                for key_id in updated_key_ids
            ],
            consumeErrors=True,
        ).addErrback(unwrapFirstError))

        results[server_name] = response_keys

        defer.returnValue(results)

    def store_keys(self, server_name, from_server, verify_keys):
        """Store a collection of verify keys for a given server
        Args:
            server_name(str): The name of the server the keys are for.
            from_server(str): The server the keys were downloaded from.
            verify_keys(dict): A mapping of key_id to VerifyKey.
        Returns:
            A deferred that completes when the keys are stored.
        """
        # TODO(markjh): Store whether the keys have expired.
        return logcontext.make_deferred_yieldable(defer.gatherResults(
            [
                run_in_background(
                    self.store.store_server_verify_key,
                    server_name, server_name, key.time_added, key
                )
                for key_id, key in verify_keys.items()
            ],
            consumeErrors=True,
        ).addErrback(unwrapFirstError))


@defer.inlineCallbacks
def _handle_key_deferred(verify_request):
    """Waits for the key to become available, and then performs a verification

    Args:
        verify_request (VerifyKeyRequest):

    Returns:
        Deferred[None]

    Raises:
        SynapseError if there was a problem performing the verification
    """
    server_name = verify_request.server_name
    try:
        with PreserveLoggingContext():
            _, key_id, verify_key = yield verify_request.deferred
    except IOError as e:
        logger.warn(
            "Got IOError when downloading keys for %s: %s %s",
            server_name, type(e).__name__, str(e),
        )
        raise SynapseError(
            502,
            "Error downloading keys for %s" % (server_name,),
            Codes.UNAUTHORIZED,
        )
    except Exception as e:
        logger.exception(
            "Got Exception when downloading keys for %s: %s %s",
            server_name, type(e).__name__, str(e),
        )
        raise SynapseError(
            401,
            "No key for %s with id %s" % (server_name, verify_request.key_ids),
            Codes.UNAUTHORIZED,
        )

    json_object = verify_request.json_object

    logger.debug("Got key %s %s:%s for server %s, verifying" % (
        key_id, verify_key.alg, verify_key.version, server_name,
    ))
    try:
        verify_signed_json(json_object, server_name, verify_key)
    except SignatureVerifyException as e:
        logger.debug(
            "Error verifying signature for %s:%s:%s with key %s: %s",
            server_name, verify_key.alg, verify_key.version,
            encode_verify_key_base64(verify_key),
            str(e),
        )
        raise SynapseError(
            401,
            "Invalid signature for server %s with key %s:%s: %s" % (
                server_name, verify_key.alg, verify_key.version, str(e),
            ),
            Codes.UNAUTHORIZED,
        )
