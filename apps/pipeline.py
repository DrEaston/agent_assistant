"""Studio project and area ownership hints for the Codex issue worker."""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_STUDIO_PROJECT_ID = "dieter"

PLATFORM_FILES = (
    "api.py",
    "database.py",
    "templates/base.html",
    "requirements.txt",
    "Dockerfile",
    ".gcloudignore",
    ".gitignore",
)

PLATFORM_DIRS = (
    "scripts/",
    "docs/",
    "tests/",
)


@dataclass(frozen=True)
class AppPipeline:
    area: str
    label: str
    owned_paths: tuple[str, ...]
    check_command: str
    project_id: str = "dieter"
    project_label: str = "Dieter"
    repo_env: str = ""
    deploy_after: bool = False


@dataclass(frozen=True)
class StudioProject:
    id: str
    label: str
    repo_env: str
    description: str
    areas: tuple[str, ...]


DIETER_PIPELINES = {
    "Kitchen / Recipes": AppPipeline(
        area="Kitchen / Recipes",
        label="Kitchen / Recipes",
        owned_paths=(
            "apps/kitchen/",
            "templates/recipe_",
            "recipe_",
        ),
        check_command="python -m unittest tests.test_app_issue_menu tests.test_recipe_scheduler_due",
        deploy_after=True,
    ),
    "Trainer": AppPipeline(
        area="Trainer",
        label="Trainer",
        owned_paths=(
            "apps/trainer/",
            "templates/trainer.html",
        ),
        check_command="python -m unittest tests.test_app_issue_menu",
        deploy_after=True,
    ),
    "Assistant / Planner": AppPipeline(
        area="Assistant / Planner",
        label="Assistant / Planner",
        owned_paths=(
            "apps/assistant/",
            "templates/projects.html",
            "templates/project_detail.html",
            "templates/chat.html",
        ),
        check_command="python -m unittest tests.test_app_issue_menu tests.test_recipe_scheduler_due",
        deploy_after=True,
    ),
    "Scheduler": AppPipeline(
        area="Scheduler",
        label="Scheduler",
        owned_paths=(
            "apps/assistant/",
            "templates/scheduler.html",
        ),
        check_command="python -m unittest tests.test_app_issue_menu tests.test_recipe_scheduler_due",
        deploy_after=True,
    ),
    "Music": AppPipeline(
        area="Music",
        label="Music",
        owned_paths=(
            "apps/music/",
            "templates/playlists.html",
        ),
        check_command="python -m unittest tests.test_app_issue_menu",
        deploy_after=True,
    ),
    "Studio": AppPipeline(
        area="Studio",
        label="Studio",
        owned_paths=(
            "apps/issues/",
            "templates/app_feedback.html",
            "scripts/run_codex_feedback_worker.py",
            "scripts/codex_worker_dashboard.py",
            "scripts/start_codex_worker_dashboard.ps1",
        ),
        check_command="python -m unittest tests.test_app_issue_menu",
        deploy_after=True,
    ),
    "Issues": AppPipeline(
        area="Issues",
        label="Studio",
        owned_paths=(
            "apps/issues/",
            "templates/app_feedback.html",
            "scripts/run_codex_feedback_worker.py",
            "scripts/codex_worker_dashboard.py",
            "scripts/start_codex_worker_dashboard.ps1",
        ),
        check_command="python -m unittest tests.test_app_issue_menu",
        deploy_after=True,
    ),
    "Auth": AppPipeline(
        area="Auth",
        label="Auth",
        owned_paths=(
            "templates/auth.html",
        ),
        check_command="python -m unittest tests.test_app_issue_menu",
        deploy_after=True,
    ),
    "Dieter": AppPipeline(
        area="Dieter",
        label="Shared Platform",
        owned_paths=(
            "api.py",
            "database.py",
            "apps/",
            "templates/",
            "scripts/",
            "docs/",
            "tests/",
        ),
        check_command="python -m unittest discover -q",
        deploy_after=True,
    ),
}


EXTERNAL_PIPELINES = {
    "Zombie Game / Gameplay": AppPipeline(
        area="Zombie Game / Gameplay",
        label="Gameplay",
        project_id="zombie_game",
        project_label="Zombie Game",
        repo_env="ZOMBIE_GAME_REPO_PATH",
        owned_paths=("src/", "game/", "assets/", "levels/", "tests/"),
        check_command="npm test",
    ),
    "Zombie Game / UI": AppPipeline(
        area="Zombie Game / UI",
        label="UI",
        project_id="zombie_game",
        project_label="Zombie Game",
        repo_env="ZOMBIE_GAME_REPO_PATH",
        owned_paths=("src/", "ui/", "components/", "assets/", "tests/"),
        check_command="npm test",
    ),
    "Zombie Game / Build": AppPipeline(
        area="Zombie Game / Build",
        label="Build",
        project_id="zombie_game",
        project_label="Zombie Game",
        repo_env="ZOMBIE_GAME_REPO_PATH",
        owned_paths=("package.json", "vite.config.", "src/", "public/", "tests/"),
        check_command="npm test",
    ),
    "EEG / Firmware": AppPipeline(
        area="EEG / Firmware",
        label="Firmware",
        project_id="eeg",
        project_label="EEG Headband",
        repo_env="EEG_REPO_PATH",
        owned_paths=("firmware/", "src/", "include/", "platformio.ini"),
        check_command="pio test",
    ),
    "EEG / Signal Processing": AppPipeline(
        area="EEG / Signal Processing",
        label="Signal Processing",
        project_id="eeg",
        project_label="EEG Headband",
        repo_env="EEG_REPO_PATH",
        owned_paths=("analysis/", "notebooks/", "src/", "tests/"),
        check_command="pytest",
    ),
    "EEG / Hardware": AppPipeline(
        area="EEG / Hardware",
        label="Hardware",
        project_id="eeg",
        project_label="EEG Headband",
        repo_env="EEG_REPO_PATH",
        owned_paths=("hardware/", "docs/", "bom/", "cad/"),
        check_command="",
    ),
    "Calcium Imaging / Analysis": AppPipeline(
        area="Calcium Imaging / Analysis",
        label="Analysis",
        project_id="calcium_imaging",
        project_label="Calcium Imaging",
        repo_env="CALCIUM_IMAGING_REPO_PATH",
        owned_paths=("analysis/", "src/", "notebooks/", "tests/"),
        check_command="pytest",
    ),
    "Calcium Imaging / Pipeline": AppPipeline(
        area="Calcium Imaging / Pipeline",
        label="Pipeline",
        project_id="calcium_imaging",
        project_label="Calcium Imaging",
        repo_env="CALCIUM_IMAGING_REPO_PATH",
        owned_paths=("pipeline/", "scripts/", "src/", "tests/"),
        check_command="pytest",
    ),
    "Calcium Imaging / Visualization": AppPipeline(
        area="Calcium Imaging / Visualization",
        label="Visualization",
        project_id="calcium_imaging",
        project_label="Calcium Imaging",
        repo_env="CALCIUM_IMAGING_REPO_PATH",
        owned_paths=("visualization/", "notebooks/", "src/", "tests/"),
        check_command="pytest",
    ),
}


APP_PIPELINES = {**DIETER_PIPELINES, **EXTERNAL_PIPELINES}


STUDIO_PROJECTS = (
    StudioProject(
        id="dieter",
        label="Dieter",
        repo_env="",
        description="Personal assistant platform and Studio itself.",
        areas=tuple(DIETER_PIPELINES.keys()),
    ),
    StudioProject(
        id="zombie_game",
        label="Zombie Game",
        repo_env="ZOMBIE_GAME_REPO_PATH",
        description="External game repository for gameplay, UI, and build work.",
        areas=("Zombie Game / Gameplay", "Zombie Game / UI", "Zombie Game / Build"),
    ),
    StudioProject(
        id="eeg",
        label="EEG Headband",
        repo_env="EEG_REPO_PATH",
        description="External EEG hardware, firmware, and signal-processing repository.",
        areas=("EEG / Firmware", "EEG / Signal Processing", "EEG / Hardware"),
    ),
    StudioProject(
        id="calcium_imaging",
        label="Calcium Imaging",
        repo_env="CALCIUM_IMAGING_REPO_PATH",
        description="External calcium-imaging analysis, pipeline, and visualization repository.",
        areas=(
            "Calcium Imaging / Analysis",
            "Calcium Imaging / Pipeline",
            "Calcium Imaging / Visualization",
        ),
    ),
)


PROJECT_ALIASES = {
    "": DEFAULT_STUDIO_PROJECT_ID,
    "dieter": "dieter",
    "studio": "dieter",
    "zombie": "zombie_game",
    "zombie-game": "zombie_game",
    "zombie_game": "zombie_game",
    "game": "zombie_game",
    "eeg": "eeg",
    "eeg_headband": "eeg",
    "eeg-headband": "eeg",
    "calcium": "calcium_imaging",
    "calcium-imaging": "calcium_imaging",
    "calcium_imaging": "calcium_imaging",
}


def normalize_project_id(project_id: str) -> str:
    """Normalize worker project/lane identifiers from CLI or query params."""
    key = (project_id or "").strip().lower().replace(" ", "_")
    return PROJECT_ALIASES.get(key, key if key in {project.id for project in STUDIO_PROJECTS} else DEFAULT_STUDIO_PROJECT_ID)


def studio_project_by_id(project_id: str) -> StudioProject:
    """Return a configured Studio project, defaulting to Dieter."""
    normalized = normalize_project_id(project_id)
    return next((project for project in STUDIO_PROJECTS if project.id == normalized), STUDIO_PROJECTS[0])


def studio_project_options() -> list[StudioProject]:
    """Return configured Studio project lane definitions."""
    return list(STUDIO_PROJECTS)


def project_id_for_area(area: str) -> str:
    """Resolve the Studio project lane that owns an issue area."""
    pipeline = pipeline_for_area(area)
    return normalize_project_id(pipeline.project_id if pipeline else DEFAULT_STUDIO_PROJECT_ID)


def project_label_for_id(project_id: str) -> str:
    return studio_project_by_id(project_id).label


def studio_area_options() -> list[str]:
    """Return area labels for Studio issue creation/filtering."""
    options: list[str] = []
    for project in STUDIO_PROJECTS:
        options.extend(project.areas)
    return options


def normalize_area(area: str) -> str:
    return (area or "").strip() or "Dieter"


def pipeline_for_area(area: str) -> AppPipeline | None:
    return APP_PIPELINES.get(normalize_area(area))


def path_matches_any(path: str, prefixes: tuple[str, ...]) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    return any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in prefixes)


def is_platform_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("/")
    return normalized in PLATFORM_FILES or path_matches_any(normalized, PLATFORM_DIRS)


def classify_changed_paths(paths: list[str] | tuple[str, ...], area: str) -> dict[str, list[str]]:
    pipeline = pipeline_for_area(area)
    owned_paths = pipeline.owned_paths if pipeline else ()
    classified = {"owned": [], "shared": [], "other": []}
    for path in paths:
        normalized = path.replace("\\", "/").lstrip("/")
        if owned_paths and path_matches_any(normalized, owned_paths):
            classified["owned"].append(normalized)
        elif is_platform_path(normalized):
            classified["shared"].append(normalized)
        else:
            classified["other"].append(normalized)
    return classified


def format_pipeline_prompt(area: str) -> str:
    pipeline = pipeline_for_area(area)
    if not pipeline:
        return "\n".join(
            [
                "Studio project: Dieter.",
                "App area: shared platform.",
                "This issue is not scoped to one configured area, so keep changes small and explain them.",
            ]
        )
    owned = "\n".join(f"- {path}" for path in pipeline.owned_paths)
    lines = [
        f"Studio project: {pipeline.project_label}.",
        f"App area: {pipeline.label}.",
        "Prefer app-owned files for this issue:",
        owned,
        "",
        "Shared/platform files are allowed only when necessary:",
        *[f"- {path}" for path in PLATFORM_FILES],
        *[f"- {path}" for path in PLATFORM_DIRS],
        "",
        "If you edit shared/platform files, keep the change narrow and mention it in the final note.",
        f"Suggested focused check: {pipeline.check_command or 'Project-specific manual verification.'}",
    ]
    if pipeline.repo_env:
        lines.insert(2, f"Expected repo env var: {pipeline.repo_env}.")
    return "\n".join(lines)
