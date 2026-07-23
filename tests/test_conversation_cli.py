import io
import os
from unittest.mock import patch
import unittest

from conversation import cli


class TestConversationCli(unittest.TestCase):
    def test_voice_option_is_case_insensitive(self) -> None:
        args = cli.build_parser().parse_args(["--voice", "CEDAR"])

        self.assertEqual(args.voice, "cedar")

    def test_list_voices_does_not_require_an_api_key(self) -> None:
        output = io.StringIO()
        with patch.object(cli, "load_configuration"), patch.dict(
            os.environ,
            {"OPENAI_REALTIME_VOICE": "marin"},
            clear=True,
        ), patch("sys.stdout", output):
            result = cli.main(["--list-voices"])

        self.assertEqual(result, 0)
        self.assertIn("marin (recommended, configured)", output.getvalue())
        self.assertIn("cedar (recommended)", output.getvalue())

    def test_voice_option_overrides_environment_configuration(self) -> None:
        with patch.object(cli, "load_configuration"), patch.object(
            cli,
            "require_api_key",
            return_value="key",
        ), patch.dict(
            os.environ,
            {"OPENAI_REALTIME_VOICE": "marin"},
            clear=True,
        ):
            conversation = cli.create_realtime_voice("cedar")

        audio = conversation.session_configuration()["session"]["audio"]
        self.assertEqual(audio["output"]["voice"], "cedar")


if __name__ == "__main__":
    unittest.main()
