"""Music lives in apps/music/routes.py, not api.py (Stage 1 of the split: one app per module)."""

import ast
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class MusicModuleTests(unittest.TestCase):
    def test_music_routes_are_served_from_the_music_module(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
            from apps.music import routes
        paths = {getattr(r, "path", "") for r in routes.router.routes}
        self.assertIn("/apps/music/playlists", paths)
        self.assertIn("/apps/music/playlists/spotify/callback", paths)
        served = {getattr(r, "path", "") for r in api.app.routes}
        self.assertEqual(paths - served, set(), f"not served: {sorted(paths - served)}; served types: "
                         f"{[(type(r).__mro__, {k: repr(v)[:200] for k, v in vars(r).items()}) for r in api.app.routes if type(r).__name__ == '_IncludedRouter'][:1]}")

    def test_api_py_no_longer_defines_music_code(self):
        tree = ast.parse((ROOT / "api.py").read_text(encoding="utf-8"))
        names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertEqual([n for n in names if "playlist" in n or "spotify" in n], [])


if __name__ == "__main__":
    unittest.main()
