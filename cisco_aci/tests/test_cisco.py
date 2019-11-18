# (C) Datadog, Inc. 2010-2018
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)

from datadog_checks.cisco_aci import CiscoACICheck
from datadog_checks.cisco_aci.api import Api
from datadog_checks.utils.containers import hash_mutable

from . import common


def test_cisco(aggregator):
    cisco_aci_check = CiscoACICheck(common.CHECK_NAME, {}, {})
    api = Api(common.ACI_URLS, cisco_aci_check.http, common.USERNAME, password=common.PASSWORD, log=cisco_aci_check.log)
    api.wrapper_factory = common.FakeSessionWrapper
    cisco_aci_check._api_cache[hash_mutable(common.CONFIG)] = api

    cisco_aci_check.check(common.CONFIG)
