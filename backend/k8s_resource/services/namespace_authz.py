from django.db.models import Q
from django.utils import timezone

from ops.permission_service import is_permission_admin
from ops.permissions import user_organization
from ops.tenancy import scoped_queryset

from ..models import K8sNamespacePolicy


SCOPE_RANK = {'read': 1, 'write': 2, 'admin': 3}
WRITE_ACTIONS = {'create', 'update', 'delete', 'cordon', 'uncordon', 'drain', 'scale', 'restart', 'reveal', 'apply', 'validate', 'exec'}


def _user_department_id(user):
    return getattr(getattr(user, 'profile', None), 'organization_id', None)


def _user_role(user):
    return getattr(getattr(user, 'profile', None), 'role', '') or ''


def _active_policies(user, cluster):
    now = timezone.now()
    queryset = scoped_queryset(K8sNamespacePolicy.objects.filter(cluster=cluster), user)
    queryset = queryset.filter(status='available', valid_from__lte=now)
    queryset = queryset.filter(Q(valid_until__isnull=True) | Q(valid_until__gte=now))
    return queryset.distinct()


def _policy_matches_subject(policy, user):
    if policy.users.filter(pk=user.pk).exists():
        return True
    department_id = _user_department_id(user)
    if policy.department_id and department_id == policy.department_id:
        return True
    if policy.role and policy.role == _user_role(user):
        return True
    return False


def _policy_matches_namespace(policy, namespace):
    policy_namespace = (policy.namespace or '').strip()
    if not policy_namespace or policy_namespace == '*':
        return True
    return (namespace or '').strip() == policy_namespace


def _policy_matches_resource(policy, resource_type):
    values = [str(item).lower() for item in (policy.resource_types or []) if str(item).strip()]
    if not values or '*' in values:
        return True
    return (resource_type or '').lower() in values


def _required_scope(action):
    return 'read' if action == 'read' else 'write'


def has_namespace_permission(user, cluster, namespace='', resource_type='', action='read'):
    """Return whether user may access the namespace/resource/action scope."""
    if is_permission_admin(user):
        return True
    policies = list(_active_policies(user, cluster))
    if not policies:
        # Keep existing installations usable while policies are introduced:
        # reads still rely on platform permissions, writes require an explicit policy.
        return action == 'read'
    required_rank = SCOPE_RANK[_required_scope(action)]
    for policy in policies:
        if not _policy_matches_subject(policy, user):
            continue
        if not _policy_matches_namespace(policy, namespace):
            continue
        if not _policy_matches_resource(policy, resource_type):
            continue
        if SCOPE_RANK.get(policy.action_scope, 0) >= required_rank:
            return True
    return False


def allowed_namespaces(user, cluster, action='read', resource_type=''):
    """Return None for all namespaces, otherwise a sorted list of namespace names."""
    if is_permission_admin(user):
        return None
    policies = list(_active_policies(user, cluster))
    if not policies:
        return None if action == 'read' else []
    required_rank = SCOPE_RANK[_required_scope(action)]
    result = set()
    for policy in policies:
        if not _policy_matches_subject(policy, user):
            continue
        if not _policy_matches_resource(policy, resource_type):
            continue
        if SCOPE_RANK.get(policy.action_scope, 0) < required_rank:
            continue
        namespace = (policy.namespace or '').strip()
        if not namespace or namespace == '*':
            return None
        result.add(namespace)
    return sorted(result)


def filter_items_for_user(items, user, cluster, action='read', resource_type=''):
    namespaces = allowed_namespaces(user, cluster, action=action, resource_type=resource_type)
    if namespaces is None:
        return items
    allowed = set(namespaces)
    return [item for item in items if not item.get('namespace') or item.get('namespace') in allowed]


def assert_namespace_permission(user, cluster, namespace='', resource_type='', action='read'):
    if not has_namespace_permission(user, cluster, namespace=namespace, resource_type=resource_type, action=action):
        raise PermissionError('当前用户没有该 namespace 的 K8s 业务授权')


def policy_actor(user):
    return getattr(user, 'username', 'system') or 'system'


def policy_organization(user):
    return user_organization(user)
