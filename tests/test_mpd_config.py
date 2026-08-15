"""MPD config generation.

mpd.conf is text we hand to another program, so the interesting failures
are quoting ones: a speaker whose name contains a double quote must not be
able to terminate the string and inject a directive.
"""

import tempfile
import unittest
from pathlib import Path

try:
    from bt_audio_manager.audio.mpd import MPDManager
except OSError as exc:  # libpulse.so.0 is absent on non-Linux dev machines
    raise unittest.SkipTest(f"libpulse not available: {exc}") from exc

ADDR = "AA:BB:CC:DD:EE:FF"
SINK = "bluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink"


def build(speaker_name="Kitchen Speaker", *, port=6600, password=None, log_level="info"):
    """Return (manager, generated_config_text) with paths in a temp dir."""
    tmp = tempfile.TemporaryDirectory()
    mpd = MPDManager(
        address=ADDR,
        port=port,
        speaker_name=speaker_name,
        password=password,
        log_level=log_level,
    )
    mpd._tmp_dir = tmp.name
    mpd._conf_path = str(Path(tmp.name) / "mpd.conf")
    mpd._pid_file = str(Path(tmp.name) / "pid")
    mpd._sink_name = SINK
    mpd._generate_config()
    return mpd, Path(mpd._conf_path).read_text(), tmp


class TestConfigGeneration(unittest.TestCase):
    def test_targets_the_requested_port_and_sink(self):
        _, config, tmp = build(port=6603)
        self.addCleanup(tmp.cleanup)
        self.assertIn('port                "6603"', config)
        self.assertIn(f'sink    "{SINK}"', config)

    def test_uses_the_software_mixer(self):
        # Issue #274: the pulse mixer only reports a volume while a
        # sink-input exists, so HA hides the slider when MPD is idle.
        _, config, tmp = build()
        self.addCleanup(tmp.cleanup)
        self.assertIn('mixer_type    "software"', config)

    def test_password_line_present_only_when_set(self):
        _, without, tmp1 = build(password=None)
        self.addCleanup(tmp1.cleanup)
        self.assertNotIn("password", without)

        _, with_pw, tmp2 = build(password="s3cret")
        self.addCleanup(tmp2.cleanup)
        self.assertIn('password "s3cret@read,add,control,admin"', with_pw)

    def test_log_level_follows_app_log_level(self):
        _, verbose, tmp1 = build(log_level="debug")
        self.addCleanup(tmp1.cleanup)
        self.assertIn('log_level           "verbose"', verbose)

        _, default, tmp2 = build(log_level="info")
        self.addCleanup(tmp2.cleanup)
        self.assertIn('log_level           "default"', default)

    def test_no_state_or_db_file_is_configured(self):
        # MPD 0.24 writes those with O_TMPFILE + linkat(AT_EMPTY_PATH),
        # which needs CAP_DAC_READ_SEARCH — not available in HA add-on
        # containers. Omitting them makes MPD skip saving entirely.
        _, config, tmp = build()
        self.addCleanup(tmp.cleanup)
        self.assertNotIn("state_file", config)
        self.assertNotIn("db_file", config)


class TestSpeakerNameQuoting(unittest.TestCase):
    def test_double_quote_in_name_is_escaped(self):
        _, config, tmp = build('Bose "SoundLink"')
        self.addCleanup(tmp.cleanup)
        self.assertIn(r'name    "Bose \"SoundLink\""', config)

    def test_backslash_in_name_is_escaped(self):
        _, config, tmp = build(r"Back\slash")
        self.addCleanup(tmp.cleanup)
        self.assertIn(r'name    "Back\\slash"', config)

    def test_quote_cannot_terminate_the_string_early(self):
        # A name crafted to close the quoted value and append a directive
        # must leave every quote escaped, so MPD reads the whole thing as
        # one string rather than as a new setting.
        _, config, tmp = build('Evil" state_file "/tmp/pwned')
        self.addCleanup(tmp.cleanup)
        self.assertIn(r'name    "Evil\" state_file \"/tmp/pwned"', config)
        # No bare (unescaped) quote survives inside the value.
        value = config.split('name    "', 1)[1].split('"\n', 1)[0]
        self.assertNotIn('" ', value.replace(r"\"", ""))

    def test_ordinary_unicode_name_is_left_alone(self):
        _, config, tmp = build("Café Küche 🔊")
        self.addCleanup(tmp.cleanup)
        self.assertIn('name    "Café Küche 🔊"', config)


if __name__ == "__main__":
    unittest.main()
