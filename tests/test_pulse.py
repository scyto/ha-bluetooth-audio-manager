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



A = "41:42:E8:96:EE:34"
B = "41:42:86:95:15:12"
C = "41:42:03:37:0A:D0"


def _sink(index, mac):
    return SimpleNamespace(index=index, name=f"bluez_sink.{mac.replace(':', '_')}.a2dp_sink")


def _mpd_stream(index, sink, mac=None, pid=None):
    props = {"application.name": "Music Player Daemon", "application.process.binary": "mpd"}
    if mac:
        props["module-stream-restore.id"] = f"bt-audio-manager:{mac}"
    if pid:
        props["application.process.id"] = str(pid)
    return SimpleNamespace(index=index, sink=sink, proplist=props)


class FakePinPulse:
    def __init__(self, sinks, streams):
        self.sinks = sinks
        self.streams = {s.index: s for s in streams}
        self.moves = []

    async def sink_input_info(self, index):
        return self.streams[index]

    async def sink_input_list(self):
        return list(self.streams.values())

    async def sink_info(self, index):
        return next(s for s in self.sinks if s.index == index)

    async def sink_list(self):
        return list(self.sinks)

    async def sink_input_move(self, stream_index, sink_index):
        self.moves.append((stream_index, sink_index))
        self.streams[stream_index].sink = sink_index


class TestMpdStreamPinning(unittest.IsolatedAsyncioTestCase):
    """#318: a running MPD stream moved onto another speaker goes back home.

    Seen when a speaker reconnects while others play: PulseAudio drags the
    running streams onto the new sink and two stations mix on one speaker,
    while Home Assistant still shows both as playing correctly.
    """

    def build(self, sinks, streams):
        manager = PulseAudioManager()
        manager._pulse = FakePinPulse(sinks, streams)
        return manager

    async def test_stray_stream_is_moved_back(self):
        manager = self.build([_sink(1, A), _sink(3, C)], [_mpd_stream(10, sink=3, mac=A)])
        self.assertTrue(await manager._pin_mpd_stream(10))
        self.assertEqual(manager._pulse.moves, [(10, 1)])

    async def test_stream_on_its_own_sink_is_left_alone(self):
        manager = self.build([_sink(1, A), _sink(3, C)], [_mpd_stream(10, sink=1, mac=A)])
        self.assertFalse(await manager._pin_mpd_stream(10))
        self.assertEqual(manager._pulse.moves, [])

    async def test_foreign_streams_are_left_alone(self):
        foreign = SimpleNamespace(index=20, sink=3, proplist={"application.name": "Music Assistant"})
        manager = self.build([_sink(1, A), _sink(3, C)], [foreign])
        self.assertFalse(await manager._pin_mpd_stream(20))
        self.assertEqual(manager._pulse.moves, [])

    async def test_nothing_happens_while_the_own_sink_is_missing(self):
        manager = self.build([_sink(3, C)], [_mpd_stream(10, sink=3, mac=A)])
        self.assertFalse(await manager._pin_mpd_stream(10))
        self.assertEqual(manager._pulse.moves, [])

    async def test_new_sink_brings_every_stray_home(self):
        # C reconnected and both running streams were dragged onto it.
        manager = self.build(
            [_sink(1, A), _sink(2, B), _sink(3, C)],
            [_mpd_stream(10, sink=3, mac=A), _mpd_stream(11, sink=3, mac=B),
             _mpd_stream(12, sink=3, mac=C)],
        )
        self.assertEqual(await manager._pin_all_mpd_streams(), 2)
        self.assertEqual(sorted(manager._pulse.moves), [(10, 1), (11, 2)])

    async def test_mac_case_does_not_matter(self):
        manager = self.build([_sink(1, A), _sink(3, C)], [_mpd_stream(10, sink=3, mac=A.lower())])
        self.assertTrue(await manager._pin_mpd_stream(10))

    async def test_pid_resolver_identifies_streams_without_restore_id(self):
        manager = self.build([_sink(1, A), _sink(3, C)], [_mpd_stream(10, sink=3, pid=4242)])
        manager.set_stream_owner_resolver(
            lambda props: A if props.get("application.process.id") == "4242" else None
        )
        self.assertTrue(await manager._pin_mpd_stream(10))
        self.assertEqual(manager._pulse.moves, [(10, 1)])

    async def test_gives_up_when_something_keeps_moving_the_stream(self):
        manager = self.build([_sink(1, A), _sink(3, C)], [_mpd_stream(10, sink=3, mac=A)])
        for _ in range(manager.PIN_MAX_MOVES):
            self.assertTrue(await manager._pin_mpd_stream(10))
            manager._pulse.streams[10].sink = 3  # moved away again
        self.assertFalse(await manager._pin_mpd_stream(10))
        self.assertEqual(len(manager._pulse.moves), manager.PIN_MAX_MOVES)


if __name__ == "__main__":
    unittest.main()
