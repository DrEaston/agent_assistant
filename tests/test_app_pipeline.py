import unittest

from apps.pipeline import (
    classify_changed_paths,
    format_pipeline_prompt,
    normalize_project_id,
    pipeline_for_area,
    project_id_for_area,
    studio_area_options,
    studio_project_options,
)


class AppPipelineTests(unittest.TestCase):
    def test_known_areas_have_owned_path_guidance(self):
        kitchen = pipeline_for_area("Kitchen / Recipes")
        trainer = pipeline_for_area("Trainer")
        eeg = pipeline_for_area("EEG / Signal Processing")

        self.assertIsNotNone(kitchen)
        self.assertIn("apps/kitchen/", kitchen.owned_paths)
        self.assertIsNotNone(trainer)
        self.assertIn("apps/trainer/", trainer.owned_paths)
        self.assertIsNotNone(eeg)
        self.assertEqual("EEG_REPO_PATH", eeg.repo_env)
        self.assertIn("analysis/", eeg.owned_paths)

    def test_prompt_guidance_prefers_app_owned_files(self):
        prompt = format_pipeline_prompt("Trainer")

        self.assertIn("App area: Trainer.", prompt)
        self.assertIn("apps/trainer/", prompt)
        self.assertIn("Shared/platform files are allowed only when necessary", prompt)

    def test_external_prompt_guidance_names_project_and_repo_env(self):
        prompt = format_pipeline_prompt("Calcium Imaging / Analysis")

        self.assertIn("Studio project: Calcium Imaging.", prompt)
        self.assertIn("Expected repo env var: CALCIUM_IMAGING_REPO_PATH.", prompt)
        self.assertIn("analysis/", prompt)

    def test_studio_area_options_include_external_projects(self):
        options = studio_area_options()

        self.assertIn("Kitchen / Recipes", options)
        self.assertIn("Zombie Game / Gameplay", options)
        self.assertIn("EEG / Firmware", options)
        self.assertIn("Calcium Imaging / Visualization", options)

    def test_project_routing_separates_lanes(self):
        self.assertEqual("dieter", project_id_for_area("Kitchen / Recipes"))
        self.assertEqual("zombie_game", project_id_for_area("Zombie Game / UI"))
        self.assertEqual("eeg", project_id_for_area("EEG / Firmware"))
        self.assertEqual("calcium_imaging", project_id_for_area("Calcium Imaging / Pipeline"))
        self.assertEqual("zombie_game", normalize_project_id("zombie-game"))

    def test_studio_projects_include_zombie_game(self):
        projects = {project.id: project for project in studio_project_options()}

        self.assertIn("zombie_game", projects)
        self.assertEqual("ZOMBIE_GAME_REPO_PATH", projects["zombie_game"].repo_env)
        self.assertIn("Zombie Game / Build", projects["zombie_game"].areas)

    def test_changed_paths_are_classified_by_area(self):
        classified = classify_changed_paths(
            [
                "apps/trainer/templates/trainer.html",
                "api.py",
                "apps/kitchen/templates/recipe_home.html",
            ],
            "Trainer",
        )

        self.assertEqual(classified["owned"], ["apps/trainer/templates/trainer.html"])
        self.assertEqual(classified["shared"], ["api.py"])
        self.assertEqual(classified["other"], ["apps/kitchen/templates/recipe_home.html"])


if __name__ == "__main__":
    unittest.main()
