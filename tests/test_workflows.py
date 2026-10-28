"""Static export contracts, not a substitute for importing/executing n8n 2.45.4.

Schemas checked against official n8n-io/n8n tag n8n@2.45.4:
packages/nodes-base/nodes/{Schedule/ScheduleTrigger.node.ts,
HttpRequest/V3/Description.ts,If/V2/IfV2.node.ts,
EmailSend/v2/send.operation.ts,StopAndError/StopAndError.node.ts}.
Schedule Trigger deliberately uses singular `rule`, as read by that source.
"""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest


WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "n8n"
CONTRACTS = {
    "01_discovery.json": ("0 0 */6 * * *", "discovery"),
    "02_generation.json": ("0 15 6,18 * * *", "generation"),
    "03_schedule.json": ("0 */30 * * * *", "schedule"),
    "04_publish_due.json": ("0 */5 * * * *", "publish-due"),
    "05_analytics.json": ("0 0 1 * * *", "analytics"),
    "06_failure_monitoring.json": ("0 */15 * * * *", None),
}
NODE_VERSIONS = {
    "n8n-nodes-base.scheduleTrigger": 1.2,
    "n8n-nodes-base.httpRequest": 4.2,
    "n8n-nodes-base.if": 2.2,
    "n8n-nodes-base.emailSend": 2.1,
    "n8n-nodes-base.stopAndError": 1,
}
API_CREDENTIAL = {
    "httpHeaderAuth": {
        "id": "CONFIGURE_MEME_INTERNAL_API",
        "name": "Meme Internal API",
    }
}


def load_workflow(filename):
    return json.loads((WORKFLOW_DIR / filename).read_text(encoding="utf-8"))


def node_of_type(workflow, node_type):
    nodes = [n for n in workflow["nodes"] if n["type"] == "n8n-nodes-base." + node_type]
    assert len(nodes) == 1
    return nodes[0]


def edge_to(node):
    return {"node": node["name"], "type": "main", "index": 0}


def assert_boolean_if(node, expression):
    assert node["parameters"] == {
        "conditions": {
            "options": {
                "caseSensitive": True,
                "leftValue": "",
                "typeValidation": "strict",
                "version": 2,
            },
            "conditions": [{
                "id": node["parameters"]["conditions"]["conditions"][0]["id"],
                "leftValue": expression,
                "rightValue": "",
                "operator": {"type": "boolean", "operation": "true", "singleValue": True},
            }],
            "combinator": "and",
        },
        "options": {},
    }


def test_exactly_six_exports():
    assert {path.name for path in WORKFLOW_DIR.glob("*.json")} == set(CONTRACTS)
    assert len({load_workflow(name)["name"] for name in CONTRACTS}) == 6


@pytest.mark.parametrize("filename", CONTRACTS)
def test_inactive_import_shape_and_connected_graph(filename):
    workflow = load_workflow(filename)
    assert workflow["active"] is False
    assert workflow["pinData"] == {}
    assert workflow["settings"] == {
        "executionOrder": "v1", "timezone": "Asia/Jakarta", "executionTimeout": 180,
    }
    nodes = workflow["nodes"]
    names = {node["name"] for node in nodes}
    assert len(names) == len(nodes)
    assert len({node["id"] for node in nodes}) == len(nodes)
    for node in nodes:
        assert node["type"] in NODE_VERSIONS  # No Code/Function, Meta or shell nodes.
        assert node["typeVersion"] == NODE_VERSIONS[node["type"]]
        assert len(node["position"]) == 2
        assert "disabled" not in node
        assert not node.get("executeOnce", False)
        assert not node.get("continueOnFail", False)
    incoming = set()
    for source, connections in workflow["connections"].items():
        assert source in names
        assert set(connections) == {"main"}
        for output in connections["main"]:
            for edge in output:
                assert edge["node"] in names
                assert edge["type"] == "main"
                assert edge["index"] == 0
                incoming.add(edge["node"])
    trigger = node_of_type(workflow, "scheduleTrigger")
    assert incoming == names - {trigger["name"]}
    assert trigger["notesInFlow"] is True
    assert "credential" in trigger["notes"]


@pytest.mark.parametrize("filename", CONTRACTS)
def test_official_schedule_schema_and_jakarta_cron(filename):
    trigger = node_of_type(load_workflow(filename), "scheduleTrigger")
    cron, _ = CONTRACTS[filename]
    assert trigger["parameters"] == {
        "rule": {"interval": [{"field": "cronExpression", "expression": cron}]},
    }
    assert len(cron.split()) == 6


@pytest.mark.parametrize("filename", CONTRACTS)
def test_http_auth_internal_urls_json_and_bounded_retries(filename):
    workflow = load_workflow(filename)
    _, endpoint = CONTRACTS[filename]
    allowed_paths = (
        {"/api/v1/accounts", f"/api/v1/jobs/{endpoint}"}
        if endpoint else {"/api/v1/monitoring"}
    )
    for node in workflow["nodes"]:
        if node["type"] != "n8n-nodes-base.httpRequest":
            continue
        params = node["parameters"]
        url = urlsplit(params["url"])
        assert url.scheme == "http"
        assert url.netloc == "api:8000"
        assert url.path in allowed_paths
        assert not url.query and not url.fragment
        assert params["authentication"] == "genericCredentialType"
        assert params["genericAuthType"] == "httpHeaderAuth"
        assert node["credentials"] == API_CREDENTIAL
        assert params["options"] == {
            "timeout": 30000,
            "redirect": {"redirect": {"followRedirects": False}},
            "response": {"response": {
                "responseFormat": "json", "fullResponse": False, "neverError": False,
            }},
        }
        assert "sendHeaders" not in params
        assert "sendQuery" not in params
        assert node["retryOnFail"] is True
        assert node["maxTries"] == 3
        assert node["waitBetweenTries"] == 5000
        if endpoint:
            assert "onError" not in node  # Request failures must fail the execution.
            assert not node.get("alwaysOutputData", False)  # Empty accounts stay empty.


@pytest.mark.parametrize("filename", [name for name, (_, job) in CONTRACTS.items() if job])
def test_per_account_dispatch_requires_active_and_stable_scoped_idempotency(filename):
    workflow = load_workflow(filename)
    _, endpoint = CONTRACTS[filename]
    assert len(workflow["nodes"]) == 4
    trigger = node_of_type(workflow, "scheduleTrigger")
    active = node_of_type(workflow, "if")
    accounts, job = [node for node in workflow["nodes"]
                     if node["type"] == "n8n-nodes-base.httpRequest"]
    assert accounts["parameters"]["method"] == "GET"
    assert accounts["parameters"]["url"] == "http://api:8000/api/v1/accounts"
    assert "sendBody" not in accounts["parameters"]
    assert_boolean_if(active, "={{ $json.active }}")
    assert job["parameters"]["method"] == "POST"
    assert job["parameters"]["url"] == f"http://api:8000/api/v1/jobs/{endpoint}"
    assert job["parameters"]["sendBody"] is True
    assert job["parameters"]["contentType"] == "json"
    assert job["parameters"]["specifyBody"] == "keypair"
    body = job["parameters"]["bodyParameters"]["parameters"]
    assert body == [
        {"name": "account_id", "value": "={{ $json.id }}"},
        {"name": "idempotency_key", "value": (
            "={{ 'n8n:" + endpoint + ":' + $execution.id + ':' + $json.id }}"
        )},
    ]
    assert workflow["connections"] == {
        trigger["name"]: {"main": [[edge_to(accounts)]]},
        accounts["name"]: {"main": [[edge_to(active)]]},
        active["name"]: {"main": [[edge_to(job)], []]},
    }  # Inactive items cannot reach the job; each active item retains its own id.


def test_monitoring_alerts_only_unhealthy_then_marks_execution_failed():
    workflow = load_workflow("06_failure_monitoring.json")
    assert len(workflow["nodes"]) == 5
    trigger = node_of_type(workflow, "scheduleTrigger")
    check = node_of_type(workflow, "httpRequest")
    unhealthy = node_of_type(workflow, "if")
    email = node_of_type(workflow, "emailSend")
    stop = node_of_type(workflow, "stopAndError")
    assert check["parameters"]["method"] == "GET"
    assert check["parameters"]["url"] == "http://api:8000/api/v1/monitoring"
    assert check["onError"] == "continueRegularOutput"
    assert check["alwaysOutputData"] is True
    assert_boolean_if(unhealthy, "={{ $json.healthy !== true }}")
    assert email["credentials"] == {
        "smtp": {"id": "CONFIGURE_MEME_ALERTS_SMTP", "name": "Meme Alerts SMTP"},
    }
    params = email["parameters"]
    assert params["resource"] == "email"
    assert params["operation"] == "send"
    assert params["fromEmail"] == "={{ $vars.ALERT_FROM_EMAIL }}"
    assert params["toEmail"] == "={{ $vars.ALERT_EMAIL }}"
    assert params["emailFormat"] == "text"
    assert params["options"] == {"appendAttribution": False, "allowUnauthorizedCerts": False}
    assert all(key in params["text"] for key in (
        "$json.failed_jobs", "$json.stale_jobs", "$json.uncertain_publications", "$execution.id",
    ))
    assert "JSON.stringify" not in params["text"]  # Never email raw response/error data.
    assert "$json.error" not in params["text"]
    assert "onError" not in email  # SMTP failure also fails visibly.
    assert not email.get("retryOnFail", False)  # Avoid ambiguous duplicate email retries.
    assert stop["parameters"]["errorType"] == "errorMessage"
    assert stop["parameters"]["errorMessage"]
    assert workflow["connections"] == {
        trigger["name"]: {"main": [[edge_to(check)]]},
        check["name"]: {"main": [[edge_to(unhealthy)]]},
        unhealthy["name"]: {"main": [[edge_to(email)], []]},
        email["name"]: {"main": [[edge_to(stop)]]},
    }


@pytest.mark.parametrize("filename", CONTRACTS)
def test_no_embedded_secrets_or_business_logic(filename):
    workflow = load_workflow(filename)
    for node in workflow["nodes"]:
        for credential in node.get("credentials", {}).values():
            assert set(credential) == {"id", "name"}
            assert credential["id"].startswith("CONFIGURE_")
        params = json.dumps(node["parameters"])
        assert not re.search(
            r"access_token|refresh_token|client_secret|password|Bearer |sk-[A-Za-z0-9]|"
            r"graph\.instagram\.com|graph\.facebook\.com|api\.openai\.com|"
            r"\$env|process\.env|Math\.random|Date\.now|\$now|function\s*\(|=>",
            params,
            re.IGNORECASE,
        )
        assert "apiKey" not in node["parameters"]
        assert "headerParameters" not in node["parameters"]
        assert "jsCode" not in node["parameters"]
        assert "pythonCode" not in node["parameters"]
