"""quality_scale.yaml is metadata only, but it should be true (upstream b687704).

hassfest skips this file for custom integrations, so nothing else checks it.
These tests pin the rule list Home Assistant's schema requires and the
statuses this fork claims, so a later edit cannot drop a rule or quietly
flip one.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

QUALITY_SCALE = Path("custom_components/qolsys_panel/quality_scale.yaml")

# The 54 rules Home Assistant's quality-scale schema lists (as of upstream
# b687704, checked there against script/hassfest/quality_scale.py).
ALL_RULES = {
    # Bronze
    "action-setup", "appropriate-polling", "brands", "common-modules",
    "config-flow-test-coverage", "config-flow", "dependency-transparency",
    "docs-actions", "docs-conditions", "docs-high-level-description",
    "docs-installation-instructions", "docs-removal-instructions",
    "docs-triggers", "entity-event-setup", "entity-unique-id",
    "has-entity-name", "runtime-data", "test-before-configure",
    "test-before-setup", "unique-config-entry",
    # Silver
    "action-exceptions", "config-entry-unloading",
    "docs-configuration-parameters", "docs-installation-parameters",
    "entity-unavailable", "integration-owner", "log-when-unavailable",
    "parallel-updates", "reauthentication-flow", "test-coverage",
    # Gold
    "devices", "diagnostics", "discovery", "discovery-update-info",
    "docs-data-update", "docs-examples", "docs-known-limitations",
    "docs-supported-devices", "docs-supported-functions",
    "docs-troubleshooting", "docs-use-cases", "dynamic-devices",
    "entity-category", "entity-device-class", "entity-disabled-by-default",
    "entity-translations", "exception-translations", "icon-translations",
    "reconfiguration-flow", "repair-issues", "stale-devices",
    # Platinum
    "async-dependency", "inject-websession", "strict-typing",
}  # fmt: skip


def _rules() -> dict[str, Any]:
    data = yaml.safe_load(QUALITY_SCALE.read_text(encoding="utf-8"))
    rules: dict[str, Any] = data["rules"]
    return rules


def _status(value: Any) -> str:
    return value if isinstance(value, str) else value["status"]


def test_every_rule_is_listed_once() -> None:
    """All 54 rules, nothing extra."""
    assert len(ALL_RULES) == 54
    assert set(_rules()) == ALL_RULES


def test_statuses_are_valid_and_exemptions_say_why() -> None:
    """done/todo/exempt only; an exempt rule says why."""
    for name, value in _rules().items():
        status = _status(value)
        assert status in {"done", "todo", "exempt"}, name
        if status == "exempt":
            assert isinstance(value, dict) and value.get("comment"), name


@pytest.mark.parametrize(
    ("rule", "status"),
    [
        # Corrected from upstream b687704
        ("action-setup", "done"),  # two regular services, set up in async_setup
        ("docs-conditions", "exempt"),
        ("docs-triggers", "exempt"),
        ("dynamic-devices", "done"),  # the automatic reload, upstream 79e61727e
        ("entity-translations", "todo"),
        ("strict-typing", "done"),
        # Left as they are: has-entity-name waits on Lukas's call on 820cf52
        ("has-entity-name", "done"),
        ("exception-translations", "done"),
        ("config-flow-test-coverage", "done"),
        # This fork's own entries, kept over upstream's
        ("repair-issues", "done"),
        ("stale-devices", "done"),
    ],
)
def test_rule_status(rule: str, status: str) -> None:
    """The statuses this sync sets or deliberately keeps."""
    assert _status(_rules()[rule]) == status


@pytest.mark.parametrize(
    ("rule", "phrase"),
    [
        # Our own explanations, kept over upstream's
        ("repair-issues", "stale_devices.py"),
        ("stale-devices", "Never automatic"),
        # has-entity-name stays done while 820cf52 waits on Lukas, and says so
        ("has-entity-name", "820cf52"),
        # The vendored library is excluded from mypy; only call sites are checked
        ("strict-typing", "vendored"),
    ],
)
def test_rule_comment_says(rule: str, phrase: str) -> None:
    """Each of these rules carries the comment that explains its status."""
    value = _rules()[rule]
    assert isinstance(value, dict)
    assert phrase in value["comment"]
