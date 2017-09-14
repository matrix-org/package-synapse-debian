#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright 2016 OpenMarket Ltd
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

import synapse

from synapse.config._base import ConfigError
from synapse.config.homeserver import HomeServerConfig
from synapse.config.logger import setup_logging
from synapse.http.site import SynapseSite
from synapse.http.server import JsonResource
from synapse.metrics.resource import MetricsResource, METRICS_PREFIX
from synapse.replication.slave.storage._base import BaseSlavedStore
from synapse.replication.slave.storage.appservice import SlavedApplicationServiceStore
from synapse.replication.slave.storage.client_ips import SlavedClientIpStore
from synapse.replication.slave.storage.events import SlavedEventStore
from synapse.replication.slave.storage.keys import SlavedKeyStore
from synapse.replication.slave.storage.room import RoomStore
from synapse.replication.slave.storage.directory import DirectoryStore
from synapse.replication.slave.storage.registration import SlavedRegistrationStore
from synapse.replication.slave.storage.transactions import TransactionStore
from synapse.replication.tcp.client import ReplicationClientHandler
from synapse.rest.client.v1.room import PublicRoomListRestServlet
from synapse.server import HomeServer
from synapse.storage.engines import create_engine
from synapse.util.httpresourcetree import create_resource_tree
from synapse.util.logcontext import LoggingContext, PreserveLoggingContext
from synapse.util.manhole import manhole
from synapse.util.rlimit import change_resource_limit
from synapse.util.versionstring import get_version_string
from synapse.crypto import context_factory

from synapse import events


from twisted.internet import reactor
from twisted.web.resource import Resource

from daemonize import Daemonize

import sys
import logging
import gc

logger = logging.getLogger("synapse.app.client_reader")


class ClientReaderSlavedStore(
    SlavedEventStore,
    SlavedKeyStore,
    RoomStore,
    DirectoryStore,
    SlavedApplicationServiceStore,
    SlavedRegistrationStore,
    TransactionStore,
    SlavedClientIpStore,
    BaseSlavedStore,
):
    pass


class ClientReaderServer(HomeServer):
    def get_db_conn(self, run_new_connection=True):
        # Any param beginning with cp_ is a parameter for adbapi, and should
        # not be passed to the database engine.
        db_params = {
            k: v for k, v in self.db_config.get("args", {}).items()
            if not k.startswith("cp_")
        }
        db_conn = self.database_engine.module.connect(**db_params)

        if run_new_connection:
            self.database_engine.on_new_connection(db_conn)
        return db_conn

    def setup(self):
        logger.info("Setting up.")
        self.datastore = ClientReaderSlavedStore(self.get_db_conn(), self)
        logger.info("Finished setting up.")

    def _listen_http(self, listener_config):
        port = listener_config["port"]
        bind_addresses = listener_config["bind_addresses"]
        site_tag = listener_config.get("tag", port)
        resources = {}
        for res in listener_config["resources"]:
            for name in res["names"]:
                if name == "metrics":
                    resources[METRICS_PREFIX] = MetricsResource(self)
                elif name == "client":
                    resource = JsonResource(self, canonical_json=False)
                    PublicRoomListRestServlet(self).register(resource)
                    resources.update({
                        "/_matrix/client/r0": resource,
                        "/_matrix/client/unstable": resource,
                        "/_matrix/client/v2_alpha": resource,
                        "/_matrix/client/api/v1": resource,
                    })

        root_resource = create_resource_tree(resources, Resource())

        for address in bind_addresses:
            reactor.listenTCP(
                port,
                SynapseSite(
                    "synapse.access.http.%s" % (site_tag,),
                    site_tag,
                    listener_config,
                    root_resource,
                ),
                interface=address
            )

        logger.info("Synapse client reader now listening on port %d", port)

    def start_listening(self, listeners):
        for listener in listeners:
            if listener["type"] == "http":
                self._listen_http(listener)
            elif listener["type"] == "manhole":
                bind_addresses = listener["bind_addresses"]

                for address in bind_addresses:
                    reactor.listenTCP(
                        listener["port"],
                        manhole(
                            username="matrix",
                            password="rabbithole",
                            globals={"hs": self},
                        ),
                        interface=address
                    )
            else:
                logger.warn("Unrecognized listener type: %s", listener["type"])

        self.get_tcp_replication().start_replication(self)

    def build_tcp_replication(self):
        return ReplicationClientHandler(self.get_datastore())


def start(config_options):
    try:
        config = HomeServerConfig.load_config(
            "Synapse client reader", config_options
        )
    except ConfigError as e:
        sys.stderr.write("\n" + e.message + "\n")
        sys.exit(1)

    assert config.worker_app == "synapse.app.client_reader"

    setup_logging(config, use_worker_options=True)

    events.USE_FROZEN_DICTS = config.use_frozen_dicts

    database_engine = create_engine(config.database_config)

    tls_server_context_factory = context_factory.ServerContextFactory(config)

    ss = ClientReaderServer(
        config.server_name,
        db_config=config.database_config,
        tls_server_context_factory=tls_server_context_factory,
        config=config,
        version_string="Synapse/" + get_version_string(synapse),
        database_engine=database_engine,
    )

    ss.setup()
    ss.get_handlers()
    ss.start_listening(config.worker_listeners)

    def run():
        # make sure that we run the reactor with the sentinel log context,
        # otherwise other PreserveLoggingContext instances will get confused
        # and complain when they see the logcontext arbitrarily swapping
        # between the sentinel and `run` logcontexts.
        with PreserveLoggingContext():
            logger.info("Running")
            change_resource_limit(config.soft_file_limit)
            if config.gc_thresholds:
                gc.set_threshold(*config.gc_thresholds)
            reactor.run()

    def start():
        ss.get_state_handler().start_caching()
        ss.get_datastore().start_profiling()

    reactor.callWhenRunning(start)

    if config.worker_daemonize:
        daemon = Daemonize(
            app="synapse-client-reader",
            pid=config.worker_pid_file,
            action=run,
            auto_close_fds=False,
            verbose=True,
            logger=logger,
        )
        daemon.start()
    else:
        run()


if __name__ == '__main__':
    with LoggingContext("main"):
        start(sys.argv[1:])
