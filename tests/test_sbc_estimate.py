"""Reading how far PulseAudio lowered the SBC bitrate (14.11.2026).

module-bluez5-device sets a Bluetooth sink's fixed latency to 25 ms plus the
audio in one packet; a lower bitpool fits more SBC frames into a packet.
"""

import unittest

from bt_audio_manager.audio.pulse import PulseAudioManager

PACTL = """Sink #12
\tState: RUNNING
\tName: bluez_sink.41_42_00_00_00_01.a2dp_sink
\tSample Specification: s16le 2ch 44100Hz
\tLatency: 61234 usec, configured 45317 usec
Sink #13
\tState: RUNNING
\tName: bluez_sink.41_42_00_00_00_02.a2dp_sink
\tLatency: 70000 usec, configured 68537 usec
Sink #1
\tName: auto_null
\tLatency: 0 usec, configured 0 usec
"""


class SbcEstimateTest(unittest.TestCase):

    def test_parses_latencies_per_sink(self):
        lat = PulseAudioManager.parse_sink_latencies(PACTL)
        self.assertEqual(lat["bluez_sink.41_42_00_00_00_01.a2dp_sink"],
                         {"latency_usec": 61234, "configured_usec": 45317})
        self.assertEqual(lat["auto_null"]["configured_usec"], 0)

    def test_full_bitrate_is_seven_frames(self):
        # 25 ms + 7 x 128 samples at 44.1 kHz (20.3 ms)
        self.assertEqual(PulseAudioManager.sbc_frames_per_packet(45317, 44100), 7)

    def test_lowest_step_is_fifteen_frames(self):
        self.assertEqual(PulseAudioManager.sbc_frames_per_packet(68537, 44100), 15)

    def test_suspended_or_unknown(self):
        self.assertIsNone(PulseAudioManager.sbc_frames_per_packet(0, 44100))
        self.assertIsNone(PulseAudioManager.sbc_frames_per_packet(45317, None))
        self.assertIsNone(PulseAudioManager.sbc_frames_per_packet(20000, 44100))


if __name__ == "__main__":
    unittest.main()
