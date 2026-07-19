import os
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.run_codex_feedback_worker import repo_for_run, worker_defaults_for_project


class CodexWorkerLaneTests(unittest.TestCase):
    def test_worker_defaults_are_project_scoped(self):
        dieter = worker_defaults_for_project("dieter")
        zombie = worker_defaults_for_project("zombie_game")

        self.assertEqual("dieter", dieter["project"])
        self.assertIn("dieter-codex", dieter["worker"])
        self.assertEqual("tmp/codex_worker_status_dieter.json", dieter["status_file"])
        self.assertEqual("zombie_game", zombie["project"])
        self.assertIn("zombie-game-codex", zombie["worker"])
        self.assertEqual("tmp/codex_worker_status_zombie_game.json", zombie["status_file"])

    def test_missing_zombie_repo_path_fails_clearly(self):
        with patch.dict(os.environ, {"ZOMBIE_GAME_REPO_PATH": ""}, clear=False):
            _, error, pipeline = repo_for_run(
                Path.cwd(),
                {"area": "Zombie Game / Gameplay"},
            )

        self.assertIsNotNone(pipeline)
        self.assertIn("Set ZOMBIE_GAME_REPO_PATH", error)
        self.assertIn("Zombie Game", error)


if __name__ == "__main__":
    unittest.main()
