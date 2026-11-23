"""Music lives in apps/music/routes.py, not api.py (Stage 1 of the split: one app per module)."""

import ast
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def served_paths(routes, prefix=""):
    """Every path the app answers. Newer FastAPI keeps routers added with include_router nested, so look inside."""
    paths = set()
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            context = getattr(route, "include_context", None)
            paths |= served_paths(inner.routes, prefix + (getattr(context, "prefix", "") or ""))
        elif getattr(route, "path", None) is not None:
            paths.add(prefix + route.path)
    return paths


class MusicModuleTests(unittest.TestCase):
    def test_music_routes_are_served_from_the_music_module(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import api
            from apps.music import routes
        paths = {getattr(r, "path", "") for r in routes.router.routes}
        self.assertIn("/apps/music/playlists", paths)
        self.assertIn("/apps/music/playlists/spotify/callback", paths)
        served = served_paths(api.app.routes)
        self.assertEqual(paths - served, set())

    def test_api_py_no_longer_defines_music_code(self):
        tree = ast.parse((ROOT / "api.py").read_text(encoding="utf-8"))
        names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self.assertEqual([n for n in names if "playlist" in n or "spotify" in n], [])


if __name__ == "__main__":
    unittest.main()
