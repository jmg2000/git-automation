import toml
from pydantic_settings import BaseSettings, SettingsConfigDict


# class Settings(BaseSettings):
#     gitlab_token: str
#     repo_path: str
#     gitlab_project_id: int
#     gitlab_api_url: str = "https://gitlab.com"
#     max_retries: int = 30  # Максимальное количество попыток
#     wait_interval: int = 30  # Интервал ожидания между попытками в секундах
#     kubernetes_namespace: str = "default"  # Пространство имён для проверки Pod'ов
#     kubernetes_label_selector: str = "app=myapp"  # Метки для выбора Pod'ов

#     model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

# settings = Settings()

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
    print(f"general_config: {general_config}")
    print(f"project_config: {project_config}")

    # Объединяем настройки general с настройками выбранного проекта
    combined_config = {**general_config, **project_config}
    print(f"combined_config: {combined_config}")

    return ProjectSettings(**combined_config)
