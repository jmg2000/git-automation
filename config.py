import toml
from pydantic_settings import BaseSettings

class GeneralSettings(BaseSettings):
    gitlab_token: str
    gitlab_api_url: str = "https://gitlab.com"
    kubernetes_namespace: str
    max_retries: int = 30
    wait_interval: int = 30
    master_branch: str = "master"
    develop_branch: str = "develop"
    remove_source_branch: bool = False
    squash_commits: bool = False

class ProjectSettings(GeneralSettings):
    gitlab_project_id: int
    repo_path: str
    kubernetes_label_selector: str

def load_settings(project_name: str) -> ProjectSettings:
    config = toml.load("config.toml")

    general_config = config.get("general", {})
    project_config = config.get(project_name, {})

    # Объединяем настройки general с настройками выбранного проекта
    combined_config = {**general_config, **project_config}

    return ProjectSettings(**combined_config)
