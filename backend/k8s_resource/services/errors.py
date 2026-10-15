def translate_k8s_error(exc):
    """Return a user-facing Chinese message for common Kubernetes client errors."""
    status = getattr(exc, 'status', None)
    reason = getattr(exc, 'reason', '') or ''
    body = getattr(exc, 'body', '') or ''
    raw = ' '.join([str(status or ''), str(reason), str(body), str(exc)]).strip()
    lowered = raw.lower()

    if status == 401 or '401' in raw or 'unauthorized' in lowered:
        return 'Kubernetes 凭据已过期或无效，请重新导入集群凭据'
    if status == 403 or '403' in raw or 'forbidden' in lowered:
        return '当前凭据没有访问该 Kubernetes 资源的权限'
    if status == 404 or '404' in raw or 'not found' in lowered:
        return 'Kubernetes 资源不存在或已被删除'
    if status == 409 or '409' in raw or 'conflict' in lowered or 'already exists' in lowered:
        return 'Kubernetes 资源版本冲突或同名资源已存在，请刷新后重试'
    if status == 422 or '422' in raw or 'unprocessable entity' in lowered or 'invalid' in lowered:
        return 'Kubernetes 资源字段校验失败，请检查 YAML 或表单参数'
    if status == 429 or '429' in raw or 'too many requests' in lowered or 'rate limit' in lowered:
        return 'Kubernetes API Server 请求过多已被限流，请稍后重试'
    if status and 500 <= int(status) <= 599:
        return 'Kubernetes API Server 内部异常，请稍后重试或检查集群控制面'
    if any(code in raw for code in ['500', '502', '503', '504']) or 'internal server error' in lowered or 'service unavailable' in lowered:
        return 'Kubernetes API Server 内部异常，请稍后重试或检查集群控制面'
    if 'connection refused' in lowered or 'actively refused' in lowered:
        return '无法连接 Kubernetes API Server，请检查地址、网络和防火墙'
    if 'httpsconnection' in lowered or 'max retries' in lowered or 'connection aborted' in lowered or 'connection reset' in lowered:
        return 'Kubernetes API Server 网络连接不稳定，请稍后刷新重试'
    if 'timed out' in lowered or 'timeout' in lowered:
        return 'Kubernetes API Server 响应超时，请稍后重试'
    if 'certificate' in lowered or 'ssl' in lowered:
        return 'Kubernetes API Server 证书校验失败，请检查 kubeconfig 证书配置'
    if 'no route to host' in lowered or 'network is unreachable' in lowered:
        return '无法访问 Kubernetes API Server 网络，请检查路由和网络连通性'
    return raw or 'Kubernetes 操作失败'
