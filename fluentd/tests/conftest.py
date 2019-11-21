# (C) Datadog, Inc. 2018
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)

import os

import pytest

from datadog_checks.dev import docker_run

from .common import DEFAULT_INSTANCE, FLUENTD_VERSION, HERE, URL


@pytest.fixture(scope="session")
def dd_environment():
    """
    Start a cluster with one master, one replica and one unhealthy replica and
    stop it after the tests are done.
    If there's any problem executing docker-compose, let the exception bubble
    up.
    """
    if not FLUENTD_VERSION:
        pytest.skip(reason='FLUENTD_VERSION is required')

    env = {
        'TD_AGENT_CONF_PATH': os.path.join(HERE, 'compose', 'td-agent.conf'),
        'FLUENTD_VERSION': FLUENTD_VERSION,
    }

    with docker_run(
        compose_file=os.path.join(HERE, 'compose', 'docker-compose.yaml'),
        log_patterns="type monitor_agent",
        endpoints=[URL],
        env_vars=env,
    ):
        yield DEFAULT_INSTANCE


@pytest.fixture
def instance():
    return DEFAULT_INSTANCE
