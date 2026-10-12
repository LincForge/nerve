"""Tests for device data models."""

import pytest

from linc_nerve.core.device import DeviceType, ElementNode, NerveDevice, Platform


class TestNerveDevice:
    def test_to_dict(self) -> None:
        d = NerveDevice(
            serial="ABC123",
            platform=Platform.ANDROID,
            device_type=DeviceType.PHONE,
            model="Pixel 8 Pro",
            os_version="14",
        )
        result = d.to_dict()
        assert result["serial"] == "ABC123"
        assert result["platform"] == "android"
        assert result["device_type"] == "phone"
        assert result["model"] == "Pixel 8 Pro"
        assert result["os_version"] == "14"
        assert result["state"] == "online"

    def test_emulator_device(self) -> None:
        d = NerveDevice(
            serial="emulator-5554",
            platform=Platform.ANDROID,
            device_type=DeviceType.EMULATOR,
        )
        assert d.device_type == DeviceType.EMULATOR
        assert d.state == "online"

    def test_ios_simulator(self) -> None:
        d = NerveDevice(
            serial="ABCD-1234-EF56",
            platform=Platform.IOS,
            device_type=DeviceType.SIMULATOR,
            model="iPhone 15 Pro",
        )
        assert d.platform == Platform.IOS


class TestElementNode:
    def test_compact_dict_omits_falsy(self) -> None:
        node = ElementNode(text="Hello", clickable=True, enabled=True)
        d = node.to_dict()
        assert d == {"text": "Hello", "clickable": True}
        assert "id" not in d
        assert "enabled" not in d  # True is default, omitted

    def test_disabled_element(self) -> None:
        node = ElementNode(resource_id="btn_submit", enabled=False)
        d = node.to_dict()
        assert d["id"] == "btn_submit"
        assert d["enabled"] is False

    def test_full_element(self) -> None:
        node = ElementNode(
            resource_id="com.app:id/button",
            text="Submit",
            content_desc="Submit form",
            class_name="android.widget.Button",
            bounds="[0,100][200,150]",
            clickable=True,
            checked=True,
            focused=True,
        )
        d = node.to_dict()
        assert d["id"] == "com.app:id/button"
        assert d["text"] == "Submit"
        assert d["desc"] == "Submit form"
        assert d["class"] == "android.widget.Button"
        assert d["bounds"] == "[0,100][200,150]"
        assert d["clickable"] is True
        assert d["checked"] is True
        assert d["focused"] is True

    def test_children(self) -> None:
        child = ElementNode(text="Child")
        parent = ElementNode(text="Parent", children=[child])
        d = parent.to_dict()
        assert len(d["children"]) == 1
        assert d["children"][0]["text"] == "Child"

    def test_empty_element_omits_everything(self) -> None:
        node = ElementNode()
        d = node.to_dict()
        assert d == {}


class TestDeviceKitReExport:
    def test_platform_is_device_kit_enum(self) -> None:
        """Verify Platform is actually from device-kit."""
        from linc_device_kit.models import Platform as DKPlatform

        assert Platform is DKPlatform

    def test_device_type_is_device_kit_enum(self) -> None:
        """Verify DeviceType is actually from device-kit."""
        from linc_device_kit.models import DeviceType as DKDeviceType

        assert DeviceType is DKDeviceType

    def test_device_error_is_device_kit_base(self) -> None:
        """nerve's DeviceError is an alias for device-kit's DeviceKitError.

        Aliasing (not subclassing) gives cross-repo catch symmetry: a
        DeviceKitError raised by another consumer (e.g. crucible) is caught
        by `except DeviceError` here, and nerve's `raise DeviceError(...)`
        is caught by `except DeviceKitError` there. A subclass would split
        type identity across the stack.
        """
        from linc_device_kit.exceptions import DeviceKitError

        from linc_nerve.core.device import DeviceError

        # Same type object — not merely a subclass.
        assert DeviceError is DeviceKitError

        # Cross-repo catch symmetry, both directions.
        with pytest.raises(DeviceKitError):
            raise DeviceError("device degraded (circuit open)")
        with pytest.raises(DeviceError):
            raise DeviceKitError("raised by another consumer")
