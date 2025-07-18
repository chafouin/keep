import logging
import random
import time
import uuid
from datetime import datetime, timedelta

import pytest
import pytz
from sqlalchemy import text

from keep.api.core.db import get_last_alerts
from keep.api.core.dependencies import SINGLE_TENANT_UUID
from keep.api.models.alert import DeduplicationRuleDto, AlertStatus
from keep.api.models.db.alert import AlertDeduplicationRule, AlertDeduplicationEvent, Alert
from keep.api.utils.enrichment_helpers import convert_db_alerts_to_dto_alerts
from keep.providers.providers_factory import ProvidersFactory
from tests.fixtures.client import client, setup_api_key, test_app  # noqa

# Set the log level to DEBUG
logging.basicConfig(level=logging.DEBUG)


def wait_for_alerts(client, num_alerts):
    alerts = client.get("/alerts", headers={"x-api-key": "some-api-key"}).json()
    print(f"------------- Total alerts: {len(alerts)}")
    while len(alerts) != num_alerts:
        time.sleep(1)
        alerts = client.get("/alerts", headers={"x-api-key": "some-api-key"}).json()
        print(f"------------- Total alerts: {len(alerts)}")


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_default_deduplication_rule(db_session, client, test_app):
    # insert an alert with some provider_id and make sure that the default deduplication rule is working
    provider_classes = {
        provider: ProvidersFactory.get_provider_class(provider)
        for provider in ["datadog", "prometheus"]
    }
    for provider_type, provider in provider_classes.items():
        alert = provider.simulate_alert()
        client.post(
            f"/alerts/event/{provider_type}?",
            json=alert,
            headers={"x-api-key": "some-api-key"},
        )
        time.sleep(0.1)

    wait_for_alerts(client, 2)

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()
    assert len(deduplication_rules) == 3  # default + datadog + prometheus

    for dedup_rule in deduplication_rules:
        # check that the default deduplication rule is working
        if dedup_rule.get("provider_type") == "keep":
            assert dedup_rule.get("ingested") == 0
            assert dedup_rule.get("default")
            # check how many times the alert was deduplicated in the last 24 hours
            assert dedup_rule.get("distribution") == [
                {"hour": i, "number": 0} for i in range(24)
            ]
        # check that the datadog/prometheus deduplication rule is working
        else:
            assert dedup_rule.get("ingested") == 1
            # the deduplication ratio is zero since the alert was not deduplicated
            assert dedup_rule.get("dedup_ratio") == 0
            assert dedup_rule.get("default")


@pytest.mark.timeout(15)
@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_deduplication_sanity(db_session, client, test_app):
    # insert the same alert twice and make sure that the default deduplication rule is working
    # insert an alert with some provider_id and make sure that the default deduplication rule is working
    provider = ProvidersFactory.get_provider_class("datadog")
    alert = provider.simulate_alert()
    for i in range(2):
        client.post(
            "/alerts/event/datadog", json=alert, headers={"x-api-key": "some-api-key"}
        )
        time.sleep(0.1)

    wait_for_alerts(client, 1)

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()
    while not any(
        [rule for rule in deduplication_rules if rule.get("dedup_ratio") == 50.0]
    ):
        time.sleep(0.1)
        deduplication_rules = client.get(
            "/deduplications", headers={"x-api-key": "some-api-key"}
        ).json()

    assert len(deduplication_rules) == 2  # default + datadog

    for dedup_rule in deduplication_rules:
        # check that the default deduplication rule is working
        if dedup_rule.get("provider_type") == "keep":
            assert dedup_rule.get("ingested") == 0
            assert dedup_rule.get("default")
        # check that the datadog/prometheus deduplication rule is working
        else:
            assert dedup_rule.get("ingested") == 2
            # the deduplication ratio is zero since the alert was not deduplicated
            assert dedup_rule.get("dedup_ratio") == 50.0
            assert dedup_rule.get("default")


@pytest.mark.timeout(10)
@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_deduplication_sanity_2(db_session, client, test_app):
    # insert two different alerts, twice each, and make sure that the default deduplication rule is working
    provider = ProvidersFactory.get_provider_class("datadog")
    alert1 = provider.simulate_alert()
    alert2 = alert1
    # datadog deduplicated by monitor_id
    while alert2.get("monitor_id") == alert1.get("monitor_id"):
        alert2 = provider.simulate_alert()

    for alert in [alert1, alert2]:
        for _ in range(2):
            client.post(
                "/alerts/event/datadog",
                json=alert,
                headers={"x-api-key": "some-api-key"},
            )
            time.sleep(0.1)

    wait_for_alerts(client, 2)

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    while not any(
        [rule for rule in deduplication_rules if rule.get("dedup_ratio") == 50.0]
    ):
        time.sleep(0.1)
        deduplication_rules = client.get(
            "/deduplications", headers={"x-api-key": "some-api-key"}
        ).json()

    assert len(deduplication_rules) == 2  # default + datadog

    for dedup_rule in deduplication_rules:
        if dedup_rule.get("provider_type") == "datadog":
            assert dedup_rule.get("ingested") == 4
            assert dedup_rule.get("dedup_ratio") == 50.0
            assert dedup_rule.get("default")


@pytest.mark.timeout(20)
@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_deduplication_sanity_3(db_session, client, test_app):
    # insert many alerts and make sure that the default deduplication rule is working
    provider = ProvidersFactory.get_provider_class("datadog")
    alerts = [provider.simulate_alert() for _ in range(10)]

    monitor_ids = set()
    for alert in alerts:
        # lets make it not deduplicated by randomizing the monitor_id
        while alert["monitor_id"] in monitor_ids:
            alert["monitor_id"] = random.randint(0, 10**10)
        monitor_ids.add(alert["monitor_id"])
        client.post(
            "/alerts/event/datadog", json=alert, headers={"x-api-key": "some-api-key"}
        )
        time.sleep(0.1)

    wait_for_alerts(client, 10)

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    assert len(deduplication_rules) == 2  # default + datadog

    for dedup_rule in deduplication_rules:
        if dedup_rule.get("provider_type") == "datadog":
            assert dedup_rule.get("ingested") == 10
            assert dedup_rule.get("dedup_ratio") == 0
            assert dedup_rule.get("default")


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_custom_deduplication_rule(db_session, client, test_app):
    provider = ProvidersFactory.get_provider_class("datadog")
    alert1 = provider.simulate_alert()
    client.post(
        "/alerts/event/datadog", json=alert1, headers={"x-api-key": "some-api-key"}
    )

    # wait for the background tasks to finish
    wait_for_alerts(client, 1)

    # create a custom deduplication rule and insert alerts that should be deduplicated by this
    custom_rule = {
        "name": "Custom Rule",
        "description": "Custom Rule Description",
        "provider_type": "datadog",
        "fingerprint_fields": ["title", "message"],
        "full_deduplication": False,
        "ignore_fields": None,
    }

    resp = client.post(
        "/deduplications", json=custom_rule, headers={"x-api-key": "some-api-key"}
    )
    assert resp.status_code == 200

    provider = ProvidersFactory.get_provider_class("datadog")
    alert = provider.simulate_alert()

    for _ in range(2):
        # shoot two alerts with the same title and message, dedup should be 50%
        client.post(
            "/alerts/event/datadog", json=alert, headers={"x-api-key": "some-api-key"}
        )
        time.sleep(0.3)

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    while not any(
        [rule for rule in deduplication_rules if rule.get("dedup_ratio") == 50.0]
    ):
        time.sleep(0.1)
        deduplication_rules = client.get(
            "/deduplications", headers={"x-api-key": "some-api-key"}
        ).json()

    custom_rule_found = False
    for dedup_rule in deduplication_rules:
        if dedup_rule.get("name") == "Custom Rule":
            custom_rule_found = True
            assert dedup_rule.get("ingested") == 2
            assert dedup_rule.get("dedup_ratio") == 50.0
            assert not dedup_rule.get("default")

    assert custom_rule_found


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_custom_deduplication_rule_behaviour(db_session, client, test_app):
    # create a custom deduplication rule and insert alerts that should be deduplicated by this
    provider = ProvidersFactory.get_provider_class("datadog")
    alert1 = provider.simulate_alert()
    client.post(
        "/alerts/event/datadog", json=alert1, headers={"x-api-key": "some-api-key"}
    )

    # wait for the background tasks to finish
    wait_for_alerts(client, 1)

    custom_rule = {
        "name": "Custom Rule",
        "description": "Custom Rule Description",
        "provider_type": "datadog",
        "fingerprint_fields": ["title", "message"],
        "full_deduplication": False,
        "ignore_fields": None,
    }

    resp = client.post(
        "/deduplications", json=custom_rule, headers={"x-api-key": "some-api-key"}
    )
    assert resp.status_code == 200

    provider = ProvidersFactory.get_provider_class("datadog")
    alert = provider.simulate_alert()

    for _ in range(2):
        # the default rule should deduplicate the alert by monitor_id so let's randomize it -
        # if the custom rule is working, the alert should be deduplicated by title and message
        alert["monitor_id"] = random.randint(0, 10**10)
        client.post(
            "/alerts/event/datadog", json=alert, headers={"x-api-key": "some-api-key"}
        )
        time.sleep(0.3)

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    while not any(
        [rule for rule in deduplication_rules if rule.get("dedup_ratio") == 50.0]
    ):
        time.sleep(1)
        deduplication_rules = client.get(
            "/deduplications", headers={"x-api-key": "some-api-key"}
        ).json()

    custom_rule_found = False
    for dedup_rule in deduplication_rules:
        if dedup_rule.get("name") == "Custom Rule":
            custom_rule_found = True
            assert dedup_rule.get("ingested") == 2
            assert dedup_rule.get("dedup_ratio") == 50.0
            assert not dedup_rule.get("default")

    assert custom_rule_found


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
            "KEEP_PROVIDERS": '{"keepDatadog":{"type":"datadog","authentication":{"api_key":"1234","app_key": "1234"}}}',
        },
    ],
    indirect=True,
)
def test_custom_deduplication_rule_2(db_session, client, test_app):
    # create a custom full deduplication rule and insert alerts that should not be deduplicated by this
    providers = client.get("/providers", headers={"x-api-key": "some-api-key"}).json()
    datadog_provider_id = next(
        provider["id"]
        for provider in providers.get("installed_providers")
        if provider["type"] == "datadog"
    )

    custom_rule = {
        "name": "Custom Rule",
        "description": "Custom Rule Description",
        "provider_type": "datadog",
        "provider_id": datadog_provider_id,
        "fingerprint_fields": [
            "name",
            "message",
        ],  # title in datadog mapped to name in keep
        "full_deduplication": False,
        "ignore_fields": ["field_that_never_exists"],
    }

    response = client.post(
        "/deduplications", json=custom_rule, headers={"x-api-key": "some-api-key"}
    )
    assert response.status_code == 200

    provider = ProvidersFactory.get_provider_class("datadog")
    alert1 = provider.simulate_alert()

    client.post(
        f"/alerts/event/datadog?provider_id={datadog_provider_id}",
        json=alert1,
        headers={"x-api-key": "some-api-key"},
    )
    alert1["title"] = "Different title"
    client.post(
        f"/alerts/event/datadog?provider_id={datadog_provider_id}",
        json=alert1,
        headers={"x-api-key": "some-api-key"},
    )

    # wait for the background tasks to finish
    alerts = client.get("/alerts", headers={"x-api-key": "some-api-key"}).json()
    while len(alerts) < 2:
        time.sleep(1)
        alerts = client.get("/alerts", headers={"x-api-key": "some-api-key"}).json()

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    custom_rule_found = False
    for dedup_rule in deduplication_rules:
        if dedup_rule.get("name") == "Custom Rule":
            custom_rule_found = True
            assert dedup_rule.get("ingested") == 2
            assert dedup_rule.get("dedup_ratio") == 0
            assert not dedup_rule.get("default")

    assert custom_rule_found


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
            "KEEP_PROVIDERS": '{"keepDatadog":{"type":"datadog","authentication":{"api_key":"1234","app_key": "1234"}}}',
        },
    ],
    indirect=True,
)
def test_update_deduplication_rule(db_session, client, test_app):
    # create a custom deduplication rule and update it
    response = client.get("/providers", headers={"x-api-key": "some-api-key"})
    assert response.status_code == 200
    datadog_provider_id = next(
        provider["id"]
        for provider in response.json().get("installed_providers")
        if provider["type"] == "datadog"
    )

    custom_rule = {
        "name": "Custom Rule",
        "description": "Custom Rule Description",
        "provider_type": "datadog",
        "provider_id": datadog_provider_id,
        "fingerprint_fields": ["title", "message"],
        "full_deduplication": False,
        "ignore_fields": None,
    }

    response = client.post(
        "/deduplications", json=custom_rule, headers={"x-api-key": "some-api-key"}
    )
    assert response.status_code == 200

    rule_id = response.json().get("id")
    updated_rule = {
        "name": "Updated Custom Rule",
        "description": "Updated Custom Rule",
        "provider_type": "datadog",
        "provider_id": datadog_provider_id,
        "fingerprint_fields": ["title"],
        "full_deduplication": False,
        "ignore_fields": None,
    }

    response = client.put(
        f"/deduplications/{rule_id}",
        json=updated_rule,
        headers={"x-api-key": "some-api-key"},
    )
    assert response.status_code == 200

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    updated_rule_found = False
    for dedup_rule in deduplication_rules:
        if dedup_rule.get("id") == rule_id:
            updated_rule_found = True
            assert dedup_rule.get("description") == "Updated Custom Rule"
            assert dedup_rule.get("fingerprint_fields") == ["title"]

    assert updated_rule_found


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_update_deduplication_rule_non_exist_provider(db_session, client, test_app):
    # create a custom deduplication rule and update it
    custom_rule = {
        "name": "Custom Rule",
        "description": "Custom Rule Description",
        "provider_type": "datadog",
        "fingerprint_fields": ["title", "message"],
        "full_deduplication": False,
        "ignore_fields": None,
    }
    response = client.post(
        "/deduplications", json=custom_rule, headers={"x-api-key": "some-api-key"}
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "Provider datadog not found"}


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_update_deduplication_rule_linked_provider(db_session, client, test_app):
    provider = ProvidersFactory.get_provider_class("datadog")
    alert1 = provider.simulate_alert()
    response = client.post(
        "/alerts/event/datadog", json=alert1, headers={"x-api-key": "some-api-key"}
    )

    time.sleep(2)
    custom_rule = {
        "name": "Custom Rule",
        "description": "Custom Rule Description",
        "provider_type": "datadog",
        "fingerprint_fields": ["title", "message"],
        "full_deduplication": False,
        "ignore_fields": None,
    }
    response = client.post(
        "/deduplications", json=custom_rule, headers={"x-api-key": "some-api-key"}
    )
    # once a linked provider is created, a customization should be allowed
    assert response.status_code == 200


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
            "KEEP_PROVIDERS": '{"keepDatadog":{"type":"datadog","authentication":{"api_key":"1234","app_key": "1234"}}}',
        },
    ],
    indirect=True,
)
def test_delete_deduplication_rule_sanity(db_session, client, test_app):
    response = client.get("/providers", headers={"x-api-key": "some-api-key"})
    assert response.status_code == 200
    datadog_provider_id = next(
        provider["id"]
        for provider in response.json().get("installed_providers")
        if provider["type"] == "datadog"
    )
    # create a custom deduplication rule and delete it
    custom_rule = {
        "name": "Custom Rule",
        "description": "Custom Rule Description",
        "provider_type": "datadog",
        "provider_id": datadog_provider_id,
        "fingerprint_fields": ["title", "message"],
        "full_deduplication": False,
        "ignore_fields": None,
    }

    response = client.post(
        "/deduplications", json=custom_rule, headers={"x-api-key": "some-api-key"}
    )
    assert response.status_code == 200

    rule_id = response.json().get("id")
    client.delete(f"/deduplications/{rule_id}", headers={"x-api-key": "some-api-key"})

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    assert all(rule.get("id") != rule_id for rule in deduplication_rules)


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_delete_deduplication_rule_invalid(db_session, client, test_app):
    # try to delete a deduplication rule that does not exist
    response = client.delete(
        "/deduplications/non-existent-id", headers={"x-api-key": "some-api-key"}
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid rule id"}

    # now use UUID
    some_uuid = str(uuid.uuid4())
    response = client.delete(
        f"/deduplications/{some_uuid}", headers={"x-api-key": "some-api-key"}
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_delete_deduplication_rule_default(db_session, client, test_app):
    # shoot an alert to create a default deduplication rule
    provider = ProvidersFactory.get_provider_class("datadog")
    alert = provider.simulate_alert()
    client.post(
        "/alerts/event/datadog", json=alert, headers={"x-api-key": "some-api-key"}
    )

    alerts = client.get("/alerts", headers={"x-api-key": "some-api-key"}).json()
    while len(alerts) != 1:
        time.sleep(1)
        alerts = client.get("/alerts", headers={"x-api-key": "some-api-key"}).json()

    # try to delete a default deduplication rule
    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    default_rule_id = next(
        rule["id"] for rule in deduplication_rules if rule["default"]
    )

    response = client.delete(
        f"/deduplications/{default_rule_id}", headers={"x-api-key": "some-api-key"}
    )

    assert response.status_code == 404


"""
SHAHAR: should be resolved

@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_full_deduplication(db_session, client, test_app):
    # create a custom deduplication rule with full deduplication and insert alerts that should be deduplicated by this
    provider = ProvidersFactory.get_provider_class("datadog")
    alert = provider.simulate_alert()
    # send the alert so a linked provider is created
    response = client.post(
        "/alerts/event/datadog", json=alert, headers={"x-api-key": "some-api-key"}
    )
    custom_rule = {
        "name": "Full Deduplication Rule",
        "description": "Full Deduplication Rule",
        "provider_type": "datadog",
        "fingerprint_fields": ["title", "message", "source"],
        "full_deduplication": True,
        "ignore_fields": list(alert.keys()),  # ignore all fields
    }

    response = client.post(
        "/deduplications", json=custom_rule, headers={"x-api-key": "some-api-key"}
    )
    assert response.status_code == 200

    for _ in range(3):
        client.post(
            "/alerts/event/datadog", json=alert, headers={"x-api-key": "some-api-key"}
        )

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    full_dedup_rule_found = False
    for dedup_rule in deduplication_rules:
        if dedup_rule.get("description") == "Full Deduplication Rule":
            full_dedup_rule_found = True
            assert dedup_rule.get("ingested") == 3
            assert 66.667 - dedup_rule.get("dedup_ratio") < 0.1  # 0.66666666....7

    assert full_dedup_rule_found
"""


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_partial_deduplication(db_session, client, test_app):
    # insert a datadog alert with the same incident_id, group and title and make sure that the datadog default deduplication rule is working
    provider = ProvidersFactory.get_provider_class("datadog")
    base_alert = provider.simulate_alert()

    alerts = [
        base_alert,
        {**base_alert, "message": "Different message"},
        {**base_alert, "source": "Different source"},
    ]

    for alert in alerts:
        client.post(
            "/alerts/event/datadog", json=alert, headers={"x-api-key": "some-api-key"}
        )
        time.sleep(0.2)

    wait_for_alerts(client, 1)

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    while not any([rule for rule in deduplication_rules if rule.get("ingested") == 3]):
        time.sleep(1)
        deduplication_rules = client.get(
            "/deduplications", headers={"x-api-key": "some-api-key"}
        ).json()

    datadog_rule_found = False
    for dedup_rule in deduplication_rules:
        if dedup_rule.get("provider_type") == "datadog" and dedup_rule.get("default"):
            datadog_rule_found = True
            assert dedup_rule.get("ingested") == 3
            assert (
                dedup_rule.get("dedup_ratio") > 0
                and dedup_rule.get("dedup_ratio") < 100
            )

    assert datadog_rule_found


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_ingesting_alert_without_fingerprint_fields(db_session, client, test_app):
    # insert a datadog alert without the required fingerprint fields and make sure that it is not deduplicated
    provider = ProvidersFactory.get_provider_class("datadog")
    alert = provider.simulate_alert()
    alert.pop("incident_id", None)
    alert.pop("group", None)
    alert["title"] = str(random.randint(0, 10**10))

    client.post(
        "/alerts/event/datadog", json=alert, headers={"x-api-key": "some-api-key"}
    )

    wait_for_alerts(client, 1)

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    datadog_rule_found = False
    for dedup_rule in deduplication_rules:
        if dedup_rule.get("provider_type") == "datadog" and dedup_rule.get("default"):
            datadog_rule_found = True
            assert dedup_rule.get("ingested") == 1
            assert dedup_rule.get("dedup_ratio") == 0

    assert datadog_rule_found


@pytest.mark.timeout(15)
@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_deduplication_fields(db_session, client, test_app):
    # insert a datadog alert with the same incident_id and make sure that the datadog default deduplication rule is working
    provider = ProvidersFactory.get_provider_class("datadog")
    base_alert = provider.simulate_alert()

    alerts = [
        base_alert,
        {**base_alert, "group": "Different group"},
        {**base_alert, "title": "Different title"},
    ]

    for alert in alerts:
        client.post(
            "/alerts/event/datadog", json=alert, headers={"x-api-key": "some-api-key"}
        )

    wait_for_alerts(client, 1)

    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    while not any([rule for rule in deduplication_rules if rule.get("ingested") == 3]):
        print("Waiting for deduplication rules to be ingested")
        time.sleep(1)
        deduplication_rules = client.get(
            "/deduplications", headers={"x-api-key": "some-api-key"}
        ).json()

    datadog_rule_found = False
    for dedup_rule in deduplication_rules:
        if dedup_rule.get("provider_type") == "datadog" and dedup_rule.get("default"):
            datadog_rule_found = True
            assert dedup_rule.get("ingested") == 3
            # @tb: couldn't understand this:
            # assert 66.667 - dedup_rule.get("dedup_ratio") < 0.1  # 0.66666666....7
    assert datadog_rule_found


# @pytest.mark.parametrize("test_app", [{"AUTH_TYPE": "NOAUTH"}])
def test_full_deduplication_last_received(db_session, create_alert):

    db_session.exec(text("DELETE FROM alertdeduplicationrule"))
    dedup = AlertDeduplicationRule(
        name="Test Rule",
        fingerprint_fields=["service",],
        full_deduplication=True,
        ignore_fields=["fingerprint", "lastReceived", "id"],
        is_provisioned=True,
        tenant_id=SINGLE_TENANT_UUID,
        description="test",
        provider_id="test",
        provider_type="keep",
        last_updated_by="test",
        created_by="test",
    )
    db_session.add(dedup)
    db_session.commit()
    db_session.refresh(dedup)

    dt1 = datetime.utcnow()
    dt2 = dt1 + timedelta(hours=1)

    create_alert(
        None,
        AlertStatus.FIRING,
        dt1,
        {
            "source": ["keep"],
            "service": "service"
        },
    )

    assert db_session.query(Alert).count() == 1
    alerts = get_last_alerts(SINGLE_TENANT_UUID)
    alerts_dto = convert_db_alerts_to_dto_alerts(alerts)

    assert alerts_dto[0].lastReceived == dt1.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    create_alert(
        None,
        AlertStatus.FIRING,
        dt2,
        {
            "source": ["keep"],
            "service": "service"
        },
    )

    assert db_session.query(Alert).count() == 1
    alerts = get_last_alerts(SINGLE_TENANT_UUID)
    alerts_dto = convert_db_alerts_to_dto_alerts(alerts)

    assert alerts_dto[0].lastReceived == dt2.astimezone(pytz.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@pytest.mark.parametrize(
    "test_app",
    [
        {
            "AUTH_TYPE": "NOAUTH",
        },
    ],
    indirect=True,
)
def test_sort_keys_deduplication_fix(db_session, client, test_app):
    """
    Test that alerts with same content but different key ordering are properly deduplicated.
    This tests the sort_keys=True fix in the alert deduplicator hash calculation.
    """
    import hashlib
    import json
    from datetime import datetime, timezone

    # Create a base alert with specific structure using proper prometheus format
    base_labels = {
        "alertname": "TestAlert",
        "env": "production",
        "team": "backend",
        "priority": "high"
    }

    # Calculate fingerprint like prometheus does
    fingerprint_src = json.dumps(base_labels, sort_keys=True)
    fingerprint = hashlib.md5(fingerprint_src.encode()).hexdigest()

    base_alert = {
        "summary": "Test summary",
        "labels": base_labels,
        "annotations": {
            "runbook": "http://example.com",
            "description": "Test description"
        },
        "generatorURL": "http://prometheus:9090/graph",
        "startsAt": datetime.now(tz=timezone.utc).isoformat(),
        "endsAt": "0001-01-01T00:00:00Z",
        "status": "firing",
        "fingerprint": fingerprint
    }

    # Create the same alert but with different key ordering in nested objects
    # This should still be considered the same alert and deduplicated
    reordered_labels = {
        "priority": "high",  # different order
        "env": "production",
        "alertname": "TestAlert",
        "team": "backend"
    }

    # Same fingerprint since label content is identical
    reordered_alert = {
        "summary": "Test summary",
        "labels": reordered_labels,
        "generatorURL": "http://prometheus:9090/graph",  # different position
        "annotations": {
            "runbook": "http://example.com",
            "description": "Test description"
        },
        "startsAt": datetime.now(tz=timezone.utc).isoformat(),
        "endsAt": "0001-01-01T00:00:00Z",
        "status": "firing",
        "fingerprint": fingerprint  # Same fingerprint
    }

    # Send both alerts to prometheus provider
    client.post(
        "/alerts/event/prometheus",
        json=base_alert,
        headers={"x-api-key": "some-api-key"}
    )
    time.sleep(0.1)

    client.post(
        "/alerts/event/prometheus",
        json=reordered_alert,
        headers={"x-api-key": "some-api-key"}
    )
    time.sleep(0.1)

    # Should only have 1 alert because they should be deduplicated
    wait_for_alerts(client, 1)

    # Check deduplication rules to verify deduplication occurred
    deduplication_rules = client.get(
        "/deduplications", headers={"x-api-key": "some-api-key"}
    ).json()

    # Wait for deduplication ratio to be calculated
    while not any(
        [rule for rule in deduplication_rules if rule.get("dedup_ratio", 0) > 0]
    ):
        time.sleep(0.1)
        deduplication_rules = client.get(
            "/deduplications", headers={"x-api-key": "some-api-key"}
        ).json()

    # Find the prometheus deduplication rule
    prometheus_rule = None
    for rule in deduplication_rules:
        if rule.get("provider_type") == "prometheus" and rule.get("default"):
            prometheus_rule = rule
            break

    assert prometheus_rule is not None
    assert prometheus_rule.get("ingested") == 2
    assert prometheus_rule.get("dedup_ratio") == 50.0  # 1 out of 2 was deduplicated
