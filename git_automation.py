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
        'target_branch': settings.develop_branch,
        'title': commit_message,
        'remove_source_branch': settings.remove_source_branch,
        'squash' : settings.squash_commits
    })
    print(f"Merge Request created: !{mr.iid}")
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
        print(f"Successfully merged {source_branch} into the current branch.")
    except GitCommandError as e:
        print(f"Error merging branch {source_branch}: {e}")
        # Дополнительная обработка ошибок, например, откат слияния
        try:
            repo.git.merge('--abort')
            print("The merger has been cancelled.")
        except GitCommandError as abort_error:
            print(f"Error while undoing merge: {abort_error}")
        sys.exit(1)


def wait_for_pipeline(project: Project, pipeline: ProjectPipeline, wait_interval: int = 10):
    while True:
        pipeline = project.pipelines.get(pipeline.get_id())
        status = pipeline.status
        print(f"Pipeline status {pipeline.get_id() }: {status}")
        if status in ['success', 'failed', 'canceled', 'skipped']:
            if status == 'success':
                print("The pipeline was completed successfully.")
            else:
                print(f"The pipeline ended with the status: {status}")
                sys.exit(1)
            break
        time.sleep(wait_interval)
        

def merge_merge_request(project: Project, mr: ProjectMergeRequest, max_retries: int = 30, wait_interval: int = 30):
    """
    Attempts to merge a Merge Request. If the MR cannot be merged automatically, the script will wait and retry until the maximum number of attempts is reached.

    :param mr: Merge Request Object
    :param max_retries: Maximum number of attempts
    :param wait_interval: Wait interval between attempts in seconds
    """
    attempts = 0
    while attempts < max_retries:
        mr = project.mergerequests.get(mr.get_id())
        print(f"Attempting to merge Merge Request !{mr.iid}, attempting {attempts + 1} of {max_retries}...")
        
        if mr.state != 'opened':
            print(f"Merge Request !{mr.iid} is in state '{mr.state}'. Merge is not possible.")
            sys.exit(1)
        
        if mr.merge_status == 'can_be_merged':
            try:
                mr.merge()
                print("Merge Request successfully merged.")
                return
            except gitlab.exceptions.GitlabMRClosedError as e:
                print(f"Error merging Merge Request: {e}")
                sys.exit(1)
            except gitlab.exceptions.GitlabMRAlreadyMergedError as e:
                print(f"Merge Request has already been merged: {e}")
                return
            except gitlab.exceptions.GitlabError as e:
                print(f"Unknown error while merging Merge Request: {e}")
                sys.exit(1)
        elif mr.merge_status in ['cannot_be_merged', 'unchecked']:
            print(f"Merge Request !{mr.iid} could not be merged automatically. Current status: {mr.merge_status}")
            sys.exit(1)
        else:
            print(f"Merge Request !{mr.iid} is in intermediate state: {mr.merge_status}. Waiting...")
        
        attempts += 1
        print(f"Waiting {wait_interval} seconds before trying again...")
        time.sleep(wait_interval)
    
    print(f"Merge Request !{mr.iid} could not be auto-merged after {max_retries} attempts.")
    sys.exit(1)
    
    
def wait_for_pods_ready(namespace, label_selector, timeout=300, interval=10):
    """
    Waits until all Pods with the given labels in the given namespace become Ready.

    :param namespace: Kubernetes namespace.
    :param label_selector: Labels for selecting Pods (e.g. "app=myapp").
    :param timeout: Maximum wait time in seconds.
    :param interval: The interval between checks in seconds.
    :return: True if all Pods are ready, False otherwise.
    """
    try:
        kube_config.load_kube_config()  # Или config.load_incluster_config() при необходимости
    except Exception as e:
        print(f"Error loading Kubernetes configuration: {e}")
        sys.exit(1)

    v1 = kube_client.CoreV1Api()
    start_time = time.time()
    
    while True:
        try:
            pods = v1.list_namespaced_pod(namespace, label_selector=label_selector)
        except kube_client.exceptions.ApiException as e:
            print(f"Error getting list of Pods: {e}")
            sys.exit(1)
        
        if not pods.items:
            print("No Pods found with the specified tags.")
            return False
        
        all_ready = True
        for pod in pods.items:
            print(f"Checking the Pod {pod.metadata.name}...")
            # Проверяем статус фазы Pod'а
            if pod.status.phase != "Running":
                all_ready = False
                print(f"Pod {pod.metadata.name} is in phase {pod.status.phase}. Waiting...")
                break
            # Проверяем готовность контейнеров внутри Pod'а
            for condition in pod.status.conditions:
                if condition.type == "Ready" and condition.status != "True":
                    all_ready = False
                    print(f"Pod {pod.metadata.name} is not ready. Waiting...")
                    break
            if not all_ready:
                break
        
        if all_ready:
            print("All Pods are ready and working.")
            return True
        
        if time.time() - start_time > timeout:
            print("Timeout waiting for Pods to be ready.")
            return False
        
        print(f"Waiting {interval} seconds before next check...")
        time.sleep(interval)
        

def main():
    if len(sys.argv) < 3:
        print("Using: python git_automation.py <project_name> <\"commit message\">")
        sys.exit(1)
        
    project_name = sys.argv[1]
    commit_message = sys.argv[2]
    
    settings = load_settings(project_name)

    repo = Repo(settings.repo_path)
    
    # Инициализация клиента GitLab
    gl = gitlab.Gitlab(settings.gitlab_api_url, private_token=settings.gitlab_token)
    project = gl.projects.get(settings.gitlab_project_id)

    # Стадирование всех изменений
    print("Staging all changes...")
    try:
        repo.git.add(all=True)
    except GitCommandError as e:
        print(f"Error adding files: {e}")
        sys.exit(1)

    # Коммит изменений
    print(f"Create a commit with the message: {commit_message}")
    try:
        repo.index.commit(commit_message)
    except GitCommandError as e:
        print(f"Error creating commit: {e}")
        sys.exit(1)

    # Получение текущей ветки
    current_branch = repo.active_branch.name
    print(f"Current branch: {current_branch}")

    # Пуш текущей ветки
    print(f"Pushing branch {current_branch} to remote repository...")
    try:
        origin = repo.remote(name='origin')
        origin.push(current_branch)
    except GitCommandError as e:
        print(f"Error when pushing a branch: {e}")
        sys.exit(1)

    # Создание Merge Request
    print("Creating Merge Request...")
    existing_mr = find_existing_mr(project, current_branch)
    if existing_mr:
        print(f"There is already an open Merge Request: !{existing_mr.iid}")
        mr = existing_mr
    else:
        mr = create_merge_request(project, current_branch, commit_message, settings)

    # Получение пайплайна MR
    print("Receiving Merge Request pipeline...")
    time.sleep(5)
    pipelines = mr.pipelines.list(order_by='id', sort='desc')
    if not pipelines:
        print("Warning: No pipelines associated with Merge Request. Proceeding without waiting for pipeline.")
    else:
        pipeline = pipelines[0]
        print(f"Waiting for MR pipeline (ID: {pipeline.id}) to complete...")
        wait_for_pipeline(project, pipeline)

    # Слияние Merge Request
    print("Merge Request...")
    merge_merge_request(project, mr, max_retries=settings.max_retries, wait_interval=settings.wait_interval)
    
    # Ожидание пайплайна деплоя develop
    print(f"Waiting for deployment pipeline {settings.develop_branch} to complete...")
    time.sleep(5)  # Небольшая задержка перед проверкой пайплайна develop
    try:
        develop_pipelines = project.pipelines.list(ref=settings.develop_branch, order_by='id', sort='desc', get_all=False)
        if not develop_pipelines:
            print(f"Warning: No pipelines for branch {settings.develop_branch}. Continuing execution without waiting for pipeline.")
        else:
            develop_pipeline = develop_pipelines[0]
            print(f"Waiting for pipeline {settings.develop_branch} to complete (ID: {develop_pipeline.get_id()})...")
            wait_for_pipeline(project, develop_pipeline)
    except gitlab.exceptions.GitlabGetError as e:
        print(f"Error getting pipeline {settings.develop_branch}: {e}")
        sys.exit(1)

    # Локальное обновление develop
    print(f"Switching to the {settings.develop_branch} and updating...")
    try:
        repo.git.checkout(settings.develop_branch)
        repo.git.pull('origin', settings.develop_branch)
    except GitCommandError as e:
        print(f"Error updating develop: {e}")
        sys.exit(1)

    # Переключение на master и слияние develop
    print(f"Switching to branch {settings.master_branch} and merging {settings.develop_branch}...")
    try:
        repo.git.checkout(settings.master_branch)
        repo.git.merge(settings.develop_branch, '--no-edit')
    except GitCommandError as e:
        print(f"Error merging develop into master: {e}")
        sys.exit(1)

    # Пуш master
    print(f"Pushing branch {settings.master_branch} to remote repository...")
    try:
        origin.push(settings.master_branch)
    except GitCommandError as e:
        print(f"Error when pushing master: {e}")
        sys.exit(1)

    # Ожидание пайплайна деплоя master
    print(f"Waiting for deployment pipeline to complete {settings.master_branch}...")
    time.sleep(5)  # Небольшая задержка перед проверкой пайплайна master
    try:
        master_pipelines = project.pipelines.list(ref=settings.master_branch, order_by='id', sort='desc', get_all=False)
        if not master_pipelines:
            print(f"Warning: No pipelines for branch {settings.master_branch}. Continuing execution without waiting for pipeline.")
        else:
            master_pipeline = master_pipelines[0]
            print(f"Waiting for pipeline {settings.master_branch} to complete (ID: {master_pipeline.get_id()})...")
            wait_for_pipeline(project, master_pipeline)
    except gitlab.exceptions.GitlabGetError as e:
        print(f"Error getting pipeline {settings.master_branch}: {e}")
        sys.exit(1)
        
    # Проверка готовности Pod'ов
    print("Checking Pods readiness...")
    time.sleep(10)
    if not wait_for_pods_ready(settings.kubernetes_namespace, settings.kubernetes_label_selector):
        print("Error: Not all Pods are ready.")
        sys.exit(1)

    print("All operations completed successfully.")

if __name__ == "__main__":
    main()
