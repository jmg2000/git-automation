import sys
import time
import gitlab
from git import Repo, GitCommandError
from gitlab.v4.objects import ProjectMergeRequest, ProjectPipeline, Project
from kubernetes import client as kube_client, config as kube_config
from kubernetes.client.exceptions import ApiException
from config import ProjectSettings, load_settings


def find_existing_mr(project: Project, source_branch: str, target_branch: str = 'develop') -> ProjectMergeRequest | None:
    mrs = project.mergerequests.list(source_branch=source_branch, target_branch=target_branch, state='opened')
    if mrs:
        return mrs[0]
    return None


def create_merge_request(project: Project, branch_name: str, commit_message: str, settings: ProjectSettings) -> ProjectMergeRequest:
    mr = project.mergerequests.create({
        'source_branch': branch_name,
        'target_branch': settings.target_branch,
        'title': commit_message,
        'remove_source_branch': settings.remove_source_branch,
        'squash' : settings.squash_commits
    })
    print(f"Создан Merge Request: !{mr.iid}")
    return mr


def merge_branch(repo, source_branch, commit_message=None, strategy_option=None):
    try:
        if commit_message:
            merge_command = ['--no-ff', '--no-edit', '-m', commit_message]
        else:
            merge_command = ['--no-ff', '--no-edit']
        
        if strategy_option:
            merge_command.extend(['-X', strategy_option])
        
        repo.git.merge(source_branch, *merge_command)
        print(f"Успешно слито {source_branch} в текущую ветку.")
    except GitCommandError as e:
        print(f"Ошибка при слиянии ветки {source_branch}: {e}")
        # Дополнительная обработка ошибок, например, откат слияния
        try:
            repo.git.merge('--abort')
            print("Слияние отменено.")
        except GitCommandError as abort_error:
            print(f"Ошибка при отмене слияния: {abort_error}")
        sys.exit(1)


def wait_for_pipeline(project: Project, pipeline: ProjectPipeline, wait_interval: int = 10):
    while True:
        pipeline = project.pipelines.get(pipeline.get_id())
        status = pipeline.status
        print(f"Статус пайплайна {pipeline.get_id() }: {status}")
        if status in ['success', 'failed', 'canceled', 'skipped']:
            if status == 'success':
                print("Пайплайн завершился успешно.")
            else:
                print(f"Пайплайн завершился со статусом: {status}")
                sys.exit(1)
            break
        time.sleep(wait_interval)
        

def merge_merge_request(project: Project, mr: ProjectMergeRequest, max_retries: int = 30, wait_interval: int = 30):
    """
    Пытается слить Merge Request. Если MR не может быть слит автоматически,
    скрипт будет ждать и повторять попытку до достижения максимального количества попыток.

    :param mr: Объект Merge Request
    :param max_retries: Максимальное количество попыток
    :param wait_interval: Интервал ожидания между попытками в секундах
    """
    attempts = 0
    while attempts < max_retries:
        mr = project.mergerequests.get(mr.get_id())
        print(f"Попытка слияния Merge Request !{mr.iid}, попытка {attempts + 1} из {max_retries}...")
        
        if mr.state != 'opened':
            print(f"Merge Request !{mr.iid} находится в состоянии '{mr.state}'. Слияние невозможно.")
            sys.exit(1)
        
        if mr.merge_status == 'can_be_merged':
            try:
                mr.merge()
                print("Merge Request успешно слит.")
                return
            except gitlab.exceptions.GitlabMRClosedError as e:
                print(f"Ошибка при слиянии Merge Request: {e}")
                sys.exit(1)
            except gitlab.exceptions.GitlabMRAlreadyMergedError as e:
                print(f"Merge Request уже слит: {e}")
                return
            except gitlab.exceptions.GitlabError as e:
                print(f"Неизвестная ошибка при слиянии Merge Request: {e}")
                sys.exit(1)
        elif mr.merge_status in ['cannot_be_merged', 'unchecked']:
            print(f"Merge Request !{mr.iid} не может быть слит автоматически. Текущий статус: {mr.merge_status}")
            sys.exit(1)
        else:
            print(f"Merge Request !{mr.iid} находится в промежуточном состоянии: {mr.merge_status}. Жду...")
        
        attempts += 1
        print(f"Ожидание {wait_interval} секунд перед следующей попыткой...")
        time.sleep(wait_interval)
    
    print(f"Merge Request !{mr.iid} не может быть слит автоматически после {max_retries} попыток.")
    sys.exit(1)
    
    
def wait_for_pods_ready(namespace, label_selector, timeout=300, interval=10):
    """
    Ожидает, пока все Pod'ы с указанными метками в заданном namespace не станут Ready.

    :param namespace: Пространство имён Kubernetes.
    :param label_selector: Метки для выбора Pod'ов (например, "app=myapp").
    :param timeout: Максимальное время ожидания в секундах.
    :param interval: Интервал между проверками в секундах.
    :return: True, если все Pod'ы готовы, иначе False.
    """
    try:
        kube_config.load_kube_config()  # Или config.load_incluster_config() при необходимости
    except Exception as e:
        print(f"Ошибка при загрузке конфигурации Kubernetes: {e}")
        sys.exit(1)

    v1 = kube_client.CoreV1Api()
    start_time = time.time()
    
    while True:
        try:
            pods = v1.list_namespaced_pod(namespace, label_selector=label_selector)
        except kube_client.exceptions.ApiException as e:
            print(f"Ошибка при получении списка Pod'ов: {e}")
            sys.exit(1)
        
        if not pods.items:
            print("Не найдено Pod'ов с указанными метками.")
            return False
        
        all_ready = True
        for pod in pods.items:
            print(f"Проверка Pod'а {pod.metadata.name}...")
            # Проверяем статус фазы Pod'а
            if pod.status.phase != "Running":
                all_ready = False
                print(f"Pod {pod.metadata.name} находится в фазе {pod.status.phase}. Ожидание...")
                break
            # Проверяем готовность контейнеров внутри Pod'а
            for condition in pod.status.conditions:
                if condition.type == "Ready" and condition.status != "True":
                    all_ready = False
                    print(f"Pod {pod.metadata.name} не готов. Ожидание...")
                    break
            if not all_ready:
                break
        
        if all_ready:
            print("Все Pod'ы готовы и работают.")
            return True
        
        if time.time() - start_time > timeout:
            print("Таймаут ожидания готовности Pod'ов.")
            return False
        
        print(f"Ожидание {interval} секунд перед следующей проверкой...")
        time.sleep(interval)
        

def main():
    if len(sys.argv) < 3:
        print("Использование: python git_automation.py <project_name> <\"commit message\">")
        sys.exit(1)
        
    project_name = sys.argv[1]
    commit_message = sys.argv[2]
    
    settings = load_settings(project_name)

    repo = Repo(settings.repo_path)
    
    # Инициализация клиента GitLab
    gl = gitlab.Gitlab(settings.gitlab_api_url, private_token=settings.gitlab_token)
    project = gl.projects.get(settings.gitlab_project_id)

    # Стадирование всех изменений
    print("Стадирование всех изменений...")
    try:
        repo.git.add(all=True)
    except GitCommandError as e:
        print(f"Ошибка при добавлении файлов: {e}")
        sys.exit(1)

    # Коммит изменений
    print(f"Создание коммита с сообщением: {commit_message}")
    try:
        repo.index.commit(commit_message)
    except GitCommandError as e:
        print(f"Ошибка при создании коммита: {e}")
        sys.exit(1)

    # Получение текущей ветки
    current_branch = repo.active_branch.name
    print(f"Текущая ветка: {current_branch}")

    # Пуш текущей ветки
    print(f"Пуш ветки {current_branch} в удаленный репозиторий...")
    try:
        origin = repo.remote(name='origin')
        origin.push(current_branch)
    except GitCommandError as e:
        print(f"Ошибка при пуше ветки: {e}")
        sys.exit(1)

    # Создание Merge Request
    print("Создание Merge Request...")
    existing_mr = find_existing_mr(project, current_branch)
    if existing_mr:
        print(f"Существует уже открытый Merge Request: !{existing_mr.iid}")
        mr = existing_mr
    else:
        mr = create_merge_request(project, current_branch, commit_message, settings)

    # Получение пайплайна MR
    print("Получение пайплайна Merge Request...")
    time.sleep(5)
    pipelines = mr.pipelines.list(order_by='id', sort='desc')
    if not pipelines:
        print("Предупреждение: Нет пайплайнов, связанных с Merge Request. Продолжаю выполнение без ожидания пайплайна.")
    else:
        pipeline = pipelines[0]
        print(f"Ожидание завершения пайплайна MR (ID: {pipeline.id})...")
        wait_for_pipeline(project, pipeline)

    # Слияние Merge Request
    print("Слияние Merge Request...")
    merge_merge_request(project, mr, max_retries=settings.max_retries, wait_interval=settings.wait_interval)
    
    # Ожидание пайплайна деплоя develop
    print(f"Ожидание завершения пайплайна деплоя {settings.develop_branch}...")
    time.sleep(5)  # Небольшая задержка перед проверкой пайплайна develop
    try:
        develop_pipelines = project.pipelines.list(ref=settings.develop_branch, order_by='id', sort='desc')
        if not develop_pipelines:
            print(f"Предупреждение: Нет пайплайнов для ветки {settings.develop_branch}. Продолжаю выполнение без ожидания пайплайна.")
        else:
            develop_pipeline = develop_pipelines[0]
            print(f"Ожидание завершения пайплайна {settings.develop_branch} (ID: {develop_pipeline.get_id()})...")
            wait_for_pipeline(project, develop_pipeline)
    except gitlab.exceptions.GitlabGetError as e:
        print(f"Ошибка при получении пайплайна {settings.develop_branch}: {e}")
        sys.exit(1)


    # Локальное обновление develop
    print(f"Переключение на ветку {settings.develop_branch} и обновление...")
    try:
        repo.git.checkout(settings.develop_branch)
        repo.git.pull('origin', settings.develop_branch)
    except GitCommandError as e:
        print(f"Ошибка при обновлении develop: {e}")
        sys.exit(1)

    # Переключение на master и слияние develop
    print(f"Переключение на ветку {settings.master_branch} и слияние {settings.develop_branch}...")
    try:
        repo.git.checkout(settings.master_branch)
        repo.git.merge(settings.develop_branch, '--no-edit')
    except GitCommandError as e:
        print(f"Ошибка при слиянии develop в master: {e}")
        sys.exit(1)

    # Пуш master
    print(f"Пуш ветки {settings.master_branch} в удаленный репозиторий...")
    try:
        origin.push(settings.master_branch)
    except GitCommandError as e:
        print(f"Ошибка при пуше master: {e}")
        sys.exit(1)

    # Ожидание пайплайна деплоя master
    print(f"Ожидание завершения пайплайна деплоя {settings.master_branch}...")
    time.sleep(5)  # Небольшая задержка перед проверкой пайплайна master
    try:
        master_pipelines = project.pipelines.list(ref=settings.master_branch, order_by='id', sort='desc')
        if not master_pipelines:
            print(f"Предупреждение: Нет пайплайнов для ветки {settings.master_branch}. Продолжаю выполнение без ожидания пайплайна.")
        else:
            master_pipeline = master_pipelines[0]
            print(f"Ожидание завершения пайплайна {settings.master_branch} (ID: {master_pipeline.get_id()})...")
            wait_for_pipeline(project, master_pipeline)
    except gitlab.exceptions.GitlabGetError as e:
        print(f"Ошибка при получении пайплайна {settings.master_branch}: {e}")
        sys.exit(1)
        
    # Проверка готовности Pod'ов
    print("Проверка готовности Pod'ов...")
    time.sleep(10)
    if not wait_for_pods_ready(settings.kubernetes_namespace, settings.kubernetes_label_selector):
        print("Ошибка: Не все Pod'ы готовы.")
        sys.exit(1)

    print("Все операции успешно завершены.")

if __name__ == "__main__":
    main()
