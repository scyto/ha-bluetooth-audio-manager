"""Pure helpers in manager.py: RSSI banding, sink-name and modalias parsing."""

import unittest

try:
    from bt_audio_manager.manager import (
        BluetoothAudioManager,
        _dbus_val,
        classify_signal,
    )
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc


class FakeVariant:
    """Stand-in for dbus_next.Variant, which only exposes .value here."""

    def __init__(self, value):
        self.value = value


class TestClassifySignal(unittest.TestCase):
    def test_bands(self):
        cases = [
            (-30, "excellent"),
            (-49, "excellent"),
            (-50, "good"),      # boundary: > -50 is excellent, -50 is not
            (-64, "good"),
            (-65, "fair"),
            (-74, "fair"),
            (-75, "weak"),
            (-84, "weak"),
            (-85, "very_weak"),
            (-100, "very_weak"),
        ]
        for rssi, expected in cases:
            with self.subTest(rssi=rssi):
                self.assertEqual(classify_signal(rssi), expected)

    def test_missing_rssi_is_unknown_not_a_band(self):
        # BlueZ clears RSSI when discovery stops; None must not be
        # rendered as "very_weak" or the UI shows a false warning.
        self.assertIsNone(classify_signal(None))

    def test_zero_is_treated_as_a_reading(self):
        self.assertEqual(classify_signal(0), "excellent")


class TestDbusVal(unittest.TestCase):
    def test_unwraps_a_variant(self):
        self.assertEqual(_dbus_val(FakeVariant("Speaker")), "Speaker")

    def test_none_yields_the_default(self):
        self.assertIsNone(_dbus_val(None))
        self.assertEqual(_dbus_val(None, "fallback"), "fallback")

    def test_plain_value_passes_through(self):
        self.assertEqual(_dbus_val("already unwrapped"), "already unwrapped")

    def test_falsy_variant_value_is_preserved(self):
        # A device with RSSI 0 or Connected=False must not collapse to
        # the default.
        self.assertEqual(_dbus_val(FakeVariant(0), 99), 0)
        self.assertIs(_dbus_val(FakeVariant(False), True), False)


class TestAddrFromSinkName(unittest.TestCase):
    def test_extracts_mac_from_a_bluez_sink(self):
        self.assertEqual(
            BluetoothAudioManager._addr_from_sink_name(
                "bluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink"
            ),
            "AA:BB:CC:DD:EE:FF",
        )

    def test_handles_the_hfp_profile_suffix(self):
        self.assertEqual(
            BluetoothAudioManager._addr_from_sink_name(
                "bluez_sink.AA_BB_CC_DD_EE_FF.handsfree_head_unit"
            ),
            "AA:BB:CC:DD:EE:FF",
        )

    def test_non_bluez_sink_yields_empty(self):
        self.assertEqual(BluetoothAudioManager._addr_from_sink_name("nosuchsink"), "")

    def test_non_bluez_sink_yields_nonsense_and_relies_on_caller_gating(self):
        # This is a positional split, not a parser: an alsa sink name
        # produces a plausible-looking but meaningless "address". Callers
        # gate on "bluez" appearing in the sink name (see the event
        # monitor in audio/pulse.py) before calling, so the behaviour is
        # characterised here rather than defended against.
        self.assertEqual(
            BluetoothAudioManager._addr_from_sink_name(
                "alsa_output.pci-0000_00_1f.3.analog-stereo"
            ),
            "pci-0000:00:1f",
        )


class TestModaliasToUsbId(unittest.TestCase):
    def test_parses_a_usb_modalias(self):
        self.assertEqual(
            BluetoothAudioManager._modalias_to_usb_id("usb:v2357p0604d0001"),
            "2357:0604",
        )

    def test_result_is_lowercased(self):
        self.assertEqual(
            BluetoothAudioManager._modalias_to_usb_id("usb:vABCDpEF01d0001"),
            "abcd:ef01",
        )

    def test_non_usb_modalias_returns_none(self):
        self.assertIsNone(BluetoothAudioManager._modalias_to_usb_id("pci:v00008086d00001234"))

    def test_malformed_input_returns_none(self):
        for value in ("", "usb:", "usb:vZZZZpYYYY", "not a modalias"):
            with self.subTest(value=value):
                self.assertIsNone(BluetoothAudioManager._modalias_to_usb_id(value))


if __name__ == "__main__":
    unittest.main()
