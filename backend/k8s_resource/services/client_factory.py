import os
import tempfile

from k8s_cluster.services import decrypted_kubeconfig


def build_api_client(cluster):
    """Build a Kubernetes API client from the encrypted kubeconfig of a cluster."""
    try:
        from kubernetes import client, config
    except ImportError as exc:
        raise RuntimeError('未安装 kubernetes Python Client，请先安装 kubernetes 依赖') from exc

    raw_text = decrypted_kubeconfig(cluster)
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', delete=False, suffix='.kubeconfig') as handle:
        handle.write(raw_text)
        temp_path = handle.name
    try:
        api_client = config.new_client_from_config(config_file=temp_path, context=cluster.context_name or None)
        return client, api_client
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def get_core_v1_api(cluster):
    """Return CoreV1Api for namespace, node, pod, service, configmap and secret queries."""
    client, api_client = build_api_client(cluster)
    return client.CoreV1Api(api_client)


def get_apps_v1_api(cluster):
    """Return AppsV1Api for Deployment, StatefulSet and DaemonSet queries."""
    client, api_client = build_api_client(cluster)
    return client.AppsV1Api(api_client)


def get_batch_v1_api(cluster):
    """Return BatchV1Api for Job and CronJob queries."""
    client, api_client = build_api_client(cluster)
    return client.BatchV1Api(api_client)


def get_networking_v1_api(cluster):
    """Return NetworkingV1Api for Ingress queries."""
    client, api_client = build_api_client(cluster)
    return client.NetworkingV1Api(api_client)


def get_storage_v1_api(cluster):
    """Return StorageV1Api for StorageClass queries."""
    client, api_client = build_api_client(cluster)
    return client.StorageV1Api(api_client)


def get_autoscaling_v2_api(cluster):
    """Return AutoscalingV2Api for HPA queries."""
    client, api_client = build_api_client(cluster)
    return client.AutoscalingV2Api(api_client)


def get_policy_v1_api(cluster):
    """Return PolicyV1Api for PodDisruptionBudget queries."""
    client, api_client = build_api_client(cluster)
    return client.PolicyV1Api(api_client)


def get_rbac_v1_api(cluster):
    """Return RbacAuthorizationV1Api for RBAC queries."""
    client, api_client = build_api_client(cluster)
    return client.RbacAuthorizationV1Api(api_client)


def get_api_extensions_v1_api(cluster):
    """Return ApiextensionsV1Api for CRD discovery."""
    client, api_client = build_api_client(cluster)
    return client.ApiextensionsV1Api(api_client)


def get_custom_objects_api(cluster):
    """Return CustomObjectsApi for metrics-server and custom metrics queries."""
    client, api_client = build_api_client(cluster)
    return client.CustomObjectsApi(api_client)


def get_dynamic_client(cluster):
    """Return a DynamicClient for mutating standard Kubernetes resources by kind."""
    try:
        from kubernetes.dynamic import DynamicClient
    except ImportError as exc:
        raise RuntimeError('当前 kubernetes Python Client 不支持 DynamicClient') from exc

    _client, api_client = build_api_client(cluster)
    return DynamicClient(api_client)
