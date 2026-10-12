"""Tests for Android UI elements parsing using defusedxml."""

from __future__ import annotations

import xml.etree.ElementTree as stdlib_ET
from unittest.mock import MagicMock

import defusedxml.ElementTree as defused_ET
import pytest
from defusedxml.common import DefusedXmlException

from linc_nerve.core.android import ET, AndroidController
from linc_nerve.core.device import UiHierarchyUnavailableError

REPRESENTATIVE_HIERARCHY = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" text="" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">
    <node index="0" text="Network &amp; internet" class="android.widget.TextView" />
    <node index="1" text="Connected devices" class="android.widget.TextView" />
  </node>
</hierarchy>"""

ENTITY_EXPANSION_PAYLOAD = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ELEMENT lolz (#PCDATA)>
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
]>
<hierarchy rotation="0">
  <node text="&lol2;"></node>
</hierarchy>"""

EXTERNAL_ENTITY_PAYLOAD = """<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ELEMENT foo ANY >
  <!ENTITY xxe SYSTEM "file:///etc/passwd" >
]>
<hierarchy rotation="0">
  <node text="&xxe;"></node>
</hierarchy>"""

MALFORMED_XML_PAYLOAD = '<hierarchy rotation="0"><node text="bad"><unclosed></node></hierarchy>'


def test_defusedxml_module_alias() -> None:
    """Verify linc_nerve.core.android.ET uses defusedxml."""
    assert ET is defused_ET


def test_before_after_equivalence_fixture() -> None:
    """AC1: Given a valid uiautomator hierarchy XML string, defusedxml produces the
    identical node tree as stdlib ElementTree — same nodes, attributes, and ordering."""
    stdlib_root = stdlib_ET.fromstring(REPRESENTATIVE_HIERARCHY)
    defused_root = ET.fromstring(REPRESENTATIVE_HIERARCHY)

    ctrl1 = AndroidController()
    nodes_stdlib: list[dict] = []
    ctrl1._walk_hierarchy(stdlib_root, nodes_stdlib, depth=0, max_depth=30)

    ctrl2 = AndroidController()
    nodes_defused: list[dict] = []
    ctrl2._walk_hierarchy(defused_root, nodes_defused, depth=0, max_depth=30)

    assert nodes_stdlib == nodes_defused
    assert len(nodes_defused) == 2
    assert nodes_defused[0]["text"] == "Network & internet"


def test_elements_returns_identical_nodes_via_controller() -> None:
    """AndroidUI.elements() with defusedxml returns identical element list."""
    ctrl = AndroidController()
    mock_device = MagicMock()
    mock_device.shell.return_value = REPRESENTATIVE_HIERARCHY
    mock_client = MagicMock()
    mock_client.device.return_value = mock_device
    ctrl._client = mock_client

    elements = ctrl.elements("SERIAL123")
    assert len(elements) == 2
    assert elements[0]["text"] == "Network & internet"
    assert elements[1]["text"] == "Connected devices"


def test_malformed_hierarchy_triggers_parse_retry_and_logs_warning() -> None:
    """AC2: A malformed/unparseable hierarchy string fails parse, retries, logs
    warning, and raises UiHierarchyUnavailableError after max retries."""
    ctrl = AndroidController()
    mock_device = MagicMock()
    mock_device.shell.return_value = MALFORMED_XML_PAYLOAD
    mock_client = MagicMock()
    mock_client.device.return_value = mock_device
    ctrl._client = mock_client

    with pytest.raises(UiHierarchyUnavailableError) as exc_info:
        ctrl.elements("SERIAL123")
    assert "uiautomator dump on SERIAL123 returned unparseable XML" in str(exc_info.value)


def test_entity_expansion_payload_rejected_by_defusedxml() -> None:
    """AC3: XML entity-expansion payload (billion-laughs attack) is rejected by defusedxml,
    raising DefusedXmlException and handled as a parse failure."""
    with pytest.raises(DefusedXmlException):
        ET.fromstring(ENTITY_EXPANSION_PAYLOAD)

    ctrl = AndroidController()
    mock_device = MagicMock()
    mock_device.shell.return_value = ENTITY_EXPANSION_PAYLOAD
    mock_client = MagicMock()
    mock_client.device.return_value = mock_device
    ctrl._client = mock_client

    with pytest.raises(UiHierarchyUnavailableError) as exc_info:
        ctrl.elements("SERIAL123")
    assert "unparseable XML" in str(exc_info.value)


def test_external_entity_payload_rejected_by_defusedxml() -> None:
    """AC3: XML external-entity payload (XXE) is rejected by defusedxml."""
    with pytest.raises(DefusedXmlException):
        ET.fromstring(EXTERNAL_ENTITY_PAYLOAD)

    ctrl = AndroidController()
    mock_device = MagicMock()
    mock_device.shell.return_value = EXTERNAL_ENTITY_PAYLOAD
    mock_client = MagicMock()
    mock_client.device.return_value = mock_device
    ctrl._client = mock_client

    with pytest.raises(UiHierarchyUnavailableError):
        ctrl.elements("SERIAL123")
