# KyotoTycoon Integration

## Overview

The Agent's KyotoTycoon check tracks get, set, and delete operations, and lets you monitor replication lag.

## Setup

Follow the instructions below to install and configure this check for an Agent running on a host. For containerized environments, see the [Autodiscovery Integration Templates][1] for guidance on applying these instructions.

### Installation

The KyotoTycoon check is included in the [Datadog Agent][2] package, so you don't need to install anything else on your KyotoTycoon servers.

### Configuration

1. Edit the `kyototycoon.d/conf.yaml` file, in the `conf.d/` folder at the root of your [Agent's configuration directory][3].
    See the [sample kyototycoon.d/conf.yaml][4] for all available configuration options:

    ```yaml
    init_config:

    instances:
        #  Each instance needs a report URL.
        #  name, and optionally tags keys. The report URL should
        #  be a URL to the Kyoto Tycoon "report" RPC endpoint.
        #
        #  Complete example:
        #
        - report_url: http://localhost:1978/rpc/report
        #   name: my_kyoto_instance
        #   tags:
        #     foo: bar
        #     baz: bat
    ```

2. [Restart the Agent][5] to begin sending Kong metrics to Datadog.


### Validation

[Run the Agent's `status` subcommand][6] and look for `kyototycoon` under the Checks section.

## Data Collected
### Metrics

See [metadata.csv][7] for a list of metrics provided by this check.

### Events
The KyotoTycoon check does not include any events.

### Service Checks

`kyototycoon.can_connect`:

Returns CRITICAL if the Agent cannot connect to KyotoTycoon to collect metrics, otherwise OK.

## Troubleshooting
Need help? Contact [Datadog support][8].

[1]: https://docs.datadoghq.com/agent/autodiscovery/integrations
[2]: https://app.datadoghq.com/account/settings#agent
[3]: https://docs.datadoghq.com/agent/guide/agent-configuration-files/?tab=agentv6#agent-configuration-directory
[4]: https://github.com/DataDog/integrations-core/blob/master/kyototycoon/datadog_checks/kyototycoon/data/conf.yaml.example
[5]: https://docs.datadoghq.com/agent/guide/agent-commands/?tab=agentv6#start-stop-and-restart-the-agent
[6]: https://docs.datadoghq.com/agent/guide/agent-commands/?tab=agentv6#agent-status-and-information
[7]: https://github.com/DataDog/integrations-core/blob/master/kyototycoon/metadata.csv
[8]: https://docs.datadoghq.com/help
