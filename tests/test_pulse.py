import unittest
from types import SimpleNamespace

from bt_audio_manager.audio.pulse import PulseAudioManager


class FakePulse:
    def __init__(self, sinks, sink_inputs, event_stream):
        self._sinks = sinks
        self._sink_inputs = sink_inputs
        self._event_stream = event_stream
        self.volume_calls = []

    async def sink_input_info(self, index):
        return self._event_stream

    async def sink_info(self, index):
        return next(sink for sink in self._sinks if sink.index == index)

    async def volume_set_all_chans(self, sink_input, volume):
        self.volume_calls.append((sink_input.index, volume))


class TestMpdStreamNormalization(unittest.IsolatedAsyncioTestCase):
    async def test_normalizes_exact_new_mpd_stream_when_another_mpd_stream_exists(self):
        sink = SimpleNamespace(index=7, name="bluez_sink.6C_0D_C4_30_50_F6.a2dp_sink")
        old_mpd_stream = SimpleNamespace(
            index=101,
            sink=7,
            proplist={
                "application.name": "Music Player Daemon",
                "application.process.binary": "mpd",
            },
        )
        new_mpd_stream = SimpleNamespace(
            index=102,
            sink=7,
            proplist={
                "application.name": "Music Player Daemon",
                "application.process.binary": "mpd",
            },
        )
        pulse = PulseAudioManager()
        pulse._pulse = FakePulse([sink], [old_mpd_stream, new_mpd_stream], new_mpd_stream)

        updated = await pulse._normalize_new_mpd_stream(new_mpd_stream.index)

        self.assertTrue(updated)
        self.assertEqual(pulse._pulse.volume_calls, [(new_mpd_stream.index, 1.0)])

    async def test_skips_mpd_stream_on_non_bluetooth_sink(self):
        sink = SimpleNamespace(index=7, name="alsa_output.pci-0000_00_1f.3.analog-stereo")
        mpd_stream = SimpleNamespace(
            index=101,
            sink=7,
            proplist={
                "application.name": "Music Player Daemon",
                "application.process.binary": "mpd",
            },
        )
        pulse = PulseAudioManager()
        pulse._pulse = FakePulse([sink], [mpd_stream], mpd_stream)

        updated = await pulse._normalize_new_mpd_stream(mpd_stream.index)

        self.assertFalse(updated)
        self.assertEqual(pulse._pulse.volume_calls, [])

    async def test_skips_sink_with_bluez_only_in_its_name(self):
        sink = SimpleNamespace(index=7, name="combined_bluez_sink")
        mpd_stream = SimpleNamespace(
            index=101,
            sink=7,
            proplist={
                "application.name": "Music Player Daemon",
                "application.process.binary": "mpd",
            },
        )
        pulse = PulseAudioManager()
        pulse._pulse = FakePulse([sink], [mpd_stream], mpd_stream)

        updated = await pulse._normalize_new_mpd_stream(mpd_stream.index)

        self.assertFalse(updated)
        self.assertEqual(pulse._pulse.volume_calls, [])

    async def test_skips_non_mpd_stream_on_bluetooth_sink(self):
        sink = SimpleNamespace(index=7, name="bluez_sink.6C_0D_C4_30_50_F6.a2dp_sink")
        music_stream = SimpleNamespace(
            index=101,
            sink=7,
            proplist={
                "application.name": "Another music player",
                "application.process.binary": "other-player",
            },
        )
        pulse = PulseAudioManager()
        pulse._pulse = FakePulse([sink], [music_stream], music_stream)

        updated = await pulse._normalize_new_mpd_stream(music_stream.index)

        self.assertFalse(updated)
        self.assertEqual(pulse._pulse.volume_calls, [])


if __name__ == "__main__":
    unittest.main()
