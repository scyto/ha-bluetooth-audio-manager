"""Discovery filter: CoD decoding and rejection reasons.

Device properties here are taken from real scan logs attached to issues,
so a regression shows up as "the speaker from #286 stopped appearing"
rather than as an abstract assertion failure.
"""

import unittest

from bt_audio_manager.bluez.adapter import _classify_rejection
from bt_audio_manager.bluez.constants import (
    A2DP_SINK_UUID,
    A2DP_SOURCE_UUID,
    ASCS_UUID,
    AVRCP_CONTROLLER_UUID,
    AVRCP_TARGET_UUID,
    HFP_UUID,
    PACS_UUID,
    cod_major_class,
    cod_major_label,
    cod_minor_class,
    is_cod_audio_sink,
)

# Vendor UUIDs seen in the wild, none of which imply an audio profile.
IAP2_ACCESSORY_UUID = "00000000-deca-fade-deca-deafdecacaff"  # Apple MFi
HID_UUID = "00001812-0000-1000-8000-00805f9b34fb"
BATTERY_UUID = "0000180f-0000-1000-8000-00805f9b34fb"


class TestCodDecoding(unittest.TestCase):
    """Class of Device bit unpacking (Bluetooth Assigned Numbers §2.8)."""

    def test_soundcore_3_decodes_as_loudspeaker(self):
        # Anker Soundcore 3, issue #286. BlueZ reported Class=2360340.
        cod = 0x240414
        self.assertEqual(cod, 2360340)
        self.assertEqual(cod_major_class(cod), 0x04)
        self.assertEqual(cod_major_label(cod), "Audio/Video")
        self.assertEqual(cod_minor_class(cod), 5)  # Loudspeaker
        self.assertTrue(is_cod_audio_sink(cod))

    def test_rendering_and_audio_service_bits_are_set(self):
        # A2DP v1.4.1 §5.5.1: a Sink sets the Rendering bit (18); bit 21
        # is Audio. Both are set on the Soundcore's CoD.
        cod = 0x240414
        self.assertTrue(cod & (1 << 18), "Rendering service bit")
        self.assertTrue(cod & (1 << 21), "Audio service bit")

    def test_headset_minor_class_is_a_sink(self):
        # DS1190, accepted via CoD fallback in the #286 log.
        self.assertTrue(is_cod_audio_sink(0x240404))
        self.assertEqual(cod_minor_class(0x240404), 1)  # Wearable Headset

    def test_computer_major_class_is_not_a_sink(self):
        # QUIETPC in the #286 log — has a CoD, but not an audio one.
        self.assertEqual(cod_major_label(0x2E4104), "Computer")
        self.assertFalse(is_cod_audio_sink(0x2E4104))

    def test_microphone_and_camcorder_minors_are_not_sinks(self):
        # Audio/Video major, but they capture rather than render.
        for minor, what in ((4, "Microphone"), (13, "Camcorder")):
            with self.subTest(what=what):
                self.assertFalse(is_cod_audio_sink(0x200000 | (minor << 2) | 0x04))

    def test_absent_cod_is_not_a_sink(self):
        # BlueZ reports no Class property for most LE-only devices; the
        # adapter substitutes 0.
        self.assertFalse(is_cod_audio_sink(0))
        self.assertEqual(cod_major_label(0), "Misc")


class TestRejectionReasons(unittest.TestCase):
    """The reason logged when a device is not surfaced."""

    def test_source_only_device_is_named_as_such(self):
        # Nebula projector (7C:E9:13:5E:B2:BB) from the #286 log: it
        # advertises A2DP Source plus AVRCP and no sink.
        uuids = {A2DP_SOURCE_UUID, AVRCP_TARGET_UUID, AVRCP_CONTROLLER_UUID}
        self.assertIn("audio source only", _classify_rejection(uuids))

    def test_a_source_that_is_also_a_sink_is_not_rejected_as_source(self):
        uuids = {A2DP_SOURCE_UUID, A2DP_SINK_UUID}
        self.assertNotIn("audio source only", _classify_rejection(uuids))

    def test_le_audio_takes_precedence_over_other_reasons(self):
        for uuid in (PACS_UUID, ASCS_UUID):
            with self.subTest(uuid=uuid):
                self.assertIn("LE Audio", _classify_rejection({uuid}))

    def test_avrcp_only_remote(self):
        reason = _classify_rejection({AVRCP_CONTROLLER_UUID, AVRCP_TARGET_UUID})
        self.assertIn("AVRCP remote control only", reason)

    def test_empty_uuids_reads_as_incomplete_sdp(self):
        self.assertIn("no UUIDs advertised", _classify_rejection(set()))

    def test_vendor_only_uuids_are_not_reported_as_empty(self):
        # The Soundcore advertises exactly one UUID, so "no UUIDs
        # advertised (incomplete SDP)" would be actively misleading.
        reason = _classify_rejection({IAP2_ACCESSORY_UUID})
        self.assertNotIn("no UUIDs advertised", reason)
        self.assertIn("no audio sink profile", reason)

    def test_unrelated_le_device(self):
        # Litra Beam in the #286 log — HID plus battery service.
        self.assertIn("no audio sink profile", _classify_rejection({HID_UUID, BATTERY_UUID}))


class TestSinkUuidMembership(unittest.TestCase):
    """SINK_UUIDS drives the primary match; AUDIO_UUIDS gates the fallback."""

    def test_sink_uuids_hold_playback_profiles_only(self):
        from bt_audio_manager.bluez.constants import SINK_UUIDS

        self.assertIn(A2DP_SINK_UUID, SINK_UUIDS)
        self.assertIn(HFP_UUID, SINK_UUIDS)
        self.assertNotIn(A2DP_SOURCE_UUID, SINK_UUIDS)
        self.assertNotIn(AVRCP_CONTROLLER_UUID, SINK_UUIDS)
        self.assertNotIn(PACS_UUID, SINK_UUIDS)

    def test_audio_uuids_is_a_superset_of_sink_uuids(self):
        from bt_audio_manager.bluez.constants import AUDIO_UUIDS, SINK_UUIDS

        self.assertTrue(SINK_UUIDS.issubset(AUDIO_UUIDS))
        # It must also recognise the profiles we deliberately reject, or
        # the CoD fallback cannot tell "no profile info" from "a profile
        # we don't want".
        for uuid in (A2DP_SOURCE_UUID, AVRCP_CONTROLLER_UUID, PACS_UUID):
            with self.subTest(uuid=uuid):
                self.assertIn(uuid, AUDIO_UUIDS)

    def test_vendor_uuid_is_not_recognised_as_audio(self):
        from bt_audio_manager.bluez.constants import AUDIO_UUIDS

        self.assertNotIn(IAP2_ACCESSORY_UUID, AUDIO_UUIDS)


if __name__ == "__main__":
    unittest.main()
