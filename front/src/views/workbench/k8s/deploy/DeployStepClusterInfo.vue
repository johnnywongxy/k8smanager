<script setup lang="ts">
import { computed } from 'vue'

import K8sCidrInput from './K8sCidrInput.vue'
import K8sSelect from '../components/K8sSelect.vue'
import {
  CNI_PLUGIN_OPTIONS,
  DEFAULT_DATA_DIRS,
  DEFAULT_DATA_DIR_ROOT,
  NODE_NAMING_OPTIONS,
  RUNTIME_OPTIONS,
  customDataDirLayout,
} from './types/deploy'
import type { ClusterDeployForm, DeploySystemGroup } from './types/deploy'

const form = defineModel<ClusterDeployForm>({ required: true })

const props = defineProps<{
  errors: Record<string, string>
  showErrors: boolean
  versionOptions: Array<{ value: string; label: string }>
  systemGroups: DeploySystemGroup[]
  optionsLoading: boolean
}>()

/** 左侧「操作系统类型」下拉项，直接使用分组类型名。 */
const systemTypeOptions = computed(() =>
  props.systemGroups.map((group) => ({ value: group.type, label: group.type })),
)

/** 右侧「版本」下拉项，只展示当前选中类型下的版本。 */
const systemVersionOptions = computed(
  () => props.systemGroups.find((group) => group.type === form.value.osType)?.options || [],
)

/** 操作系统类型下拉的占位文案，加载中与无数据时给出不同提示。 */
const systemPlaceholder = computed(() => {
  if (props.optionsLoading) return '操作系统加载中...'
  if (!props.systemGroups.length) return '当前 Kubernetes 版本未配置支持系统'
  return '请选择操作系统类型'
})

/**
 * 「数据目录路径」下面的说明。
 *
 * 勾选自定义目录时把用户填的目录会派生出的三条路径写出来（后端就是在它下面开这三个
 * 子目录），不勾选时直接说明用的是默认目录 —— 用户不必为了知道数据落在哪而去看文档。
 */
const dataDirHint = computed(() => {
  if (!form.value.useCustomDataDir) {
    return `当前使用平台默认目录 ${DEFAULT_DATA_DIR_ROOT}，无需填写。`
  }
  const layout = customDataDirLayout(form.value.dataDir)
  if (!layout) return '需要是已挂载的数据盘绝对路径，例如 /data/k8s。'
  return `需要是已挂载的数据盘绝对路径，平台会在这下面建：${layout}。`
})

/**
 * 切换操作系统类型后自动选中该类型下的第一个版本，避免残留上一个类型的版本。
 * 参数：`value` 表示新选中的操作系统类型。
 * 返回：无显式返回值。
 * 副作用：修改部署表单的操作系统版本字段。
 */
function onSystemTypeChange(value: string) {
  const options = props.systemGroups.find((group) => group.type === value)?.options || []
  form.value.osImage = options[0]?.value || ''
}

/**
 * 读取指定字段的校验提示。
 * 参数：`field` 表示表单字段名。
 * 返回：返回该字段的中文错误提示，未提交校验或校验通过时返回空字符串。
 * 副作用：不直接修改持久化数据。
 */
function fieldError(field: string) {
  if (!props.showErrors) return ''
  return props.errors[field] || ''
}
</script>

<template>
  <div class="k8s-deploy-panel">
    <div class="k8s-deploy-panel-title">集群信息</div>
    <div class="k8s-deploy-grid">
      <div class="k8s-deploy-section">基础信息</div>

      <label class="k8s-deploy-field">
        <span>部署名称<b>*</b></span>
        <input v-model="form.name" type="text" maxlength="50" placeholder="例如 prod-k8s-01" />
        <em v-if="fieldError('name')" class="k8s-deploy-error">{{ fieldError('name') }}</em>
        <em v-else class="k8s-deploy-hint">部署名称必须唯一，与集群接入里已有的集群重名时无法提交。</em>
      </label>

      <label class="k8s-deploy-field">
        <span>Kubernetes 版本<b>*</b></span>
        <K8sSelect
          v-model="form.k8sVersion"
          :options="versionOptions"
          :placeholder="optionsLoading ? '版本加载中...' : '请选择 Kubernetes 版本'"
        />
        <em v-if="!optionsLoading && !versionOptions.length" class="k8s-deploy-error">
          配置中心尚未维护版本对应关系，请先在 K8s管理 &gt; 配置中心新增。
        </em>
        <em v-else class="k8s-deploy-hint">版本清单来自配置中心「版本对应关系」，括号内为配套的 kubeasz 版本。</em>
        <em v-if="fieldError('k8sVersion')" class="k8s-deploy-error">{{ fieldError('k8sVersion') }}</em>
      </label>

      <div class="k8s-deploy-field">
        <span>容器运行时<b>*</b></span>
        <div class="k8s-deploy-choices">
          <label v-for="option in RUNTIME_OPTIONS" :key="option.value">
            <input v-model="form.runtime" type="radio" name="deploy-runtime" :value="option.value" />
            {{ option.label }}
          </label>
        </div>
        <em class="k8s-deploy-hint">
          Kubernetes 1.24 起 kubelet 已移除 Dockershim，不再支持 Docker 作为集群运行时，平台统一使用 containerd。
        </em>
      </div>

      <div class="k8s-deploy-field">
        <span>操作系统<b>*</b></span>
        <div class="k8s-deploy-inline">
          <K8sSelect
            v-model="form.osType"
            :options="systemTypeOptions"
            :placeholder="systemPlaceholder"
            @change="onSystemTypeChange"
          />
          <K8sSelect
            v-model="form.osImage"
            :options="systemVersionOptions"
            :placeholder="form.osType ? '请选择版本' : '请先选择操作系统类型'"
          />
        </div>
        <em v-if="!optionsLoading && !systemGroups.length" class="k8s-deploy-error">
          当前 Kubernetes 版本尚未关联可用操作系统，请先在 K8s管理 &gt; 配置中心维护版本映射。
        </em>
        <em v-else class="k8s-deploy-hint">这里只显示当前 Kubernetes 版本已关联且启用的操作系统。</em>
        <em v-if="fieldError('osImage')" class="k8s-deploy-error">{{ fieldError('osImage') }}</em>
      </div>

      <div class="k8s-deploy-field">
        <span>数据目录</span>
        <div class="k8s-deploy-choices">
          <label>
            <input v-model="form.useCustomDataDir" type="checkbox" />
            自定义数据目录
          </label>
        </div>
        <em class="k8s-deploy-hint">
          不勾选时使用平台默认目录，容器与 etcd 数据统一落在系统盘，具体路径为：
        </em>
        <ul v-if="!form.useCustomDataDir" class="k8s-deploy-default-dirs">
          <li v-for="item in DEFAULT_DATA_DIRS" :key="item.label">
            <span>{{ item.label }}</span>
            <code>{{ item.path }}</code>
          </li>
        </ul>
        <em v-else class="k8s-deploy-hint">
          勾选后由下面的路径决定：容器与 etcd 数据落在数据盘上，证书与静态配置（/etc/kubernetes）仍在系统盘。
        </em>
      </div>

      <label class="k8s-deploy-field">
        <span>数据目录路径</span>
        <input
          v-model="form.dataDir"
          type="text"
          :disabled="!form.useCustomDataDir"
          :placeholder="form.useCustomDataDir ? '/data/k8s' : DEFAULT_DATA_DIR_ROOT"
        />
        <em v-if="fieldError('dataDir')" class="k8s-deploy-error">{{ fieldError('dataDir') }}</em>
        <em v-else class="k8s-deploy-hint">{{ dataDirHint }}</em>
      </label>

      <div class="k8s-deploy-section">网络配置</div>

      <label class="k8s-deploy-field">
        <span>容器网络插件<b>*</b></span>
        <K8sSelect v-model="form.cniPlugin" :options="CNI_PLUGIN_OPTIONS" />
        <em class="k8s-deploy-hint">集群创建后仍可更换，但会导致 Pod 网络中断。</em>
        <em v-if="fieldError('cniPlugin')" class="k8s-deploy-error">{{ fieldError('cniPlugin') }}</em>
      </label>

      <label class="k8s-deploy-field">
        <span>节点网口<b>*</b></span>
        <input v-model="form.networkInterface" type="text" placeholder="eth0" />
        <em class="k8s-deploy-hint">用于节点间通信和容器网络承载的物理网卡名称。</em>
        <em v-if="fieldError('networkInterface')" class="k8s-deploy-error">{{ fieldError('networkInterface') }}</em>
      </label>

      <div class="k8s-deploy-field">
        <span>容器网络 CIDR<b>*</b></span>
        <K8sCidrInput v-model="form.podCidr" />
        <em class="k8s-deploy-hint">分配给 Pod 的地址段，集群创建后无法更改。</em>
        <em v-if="fieldError('podCidr')" class="k8s-deploy-error">{{ fieldError('podCidr') }}</em>
      </div>

      <div class="k8s-deploy-field">
        <span>Service IP 段<b>*</b></span>
        <K8sCidrInput v-model="form.serviceCidr" />
        <em class="k8s-deploy-hint warn">集群创建后无法更改，且不能与容器网络 CIDR 重叠。</em>
        <em v-if="fieldError('serviceCidr')" class="k8s-deploy-error">{{ fieldError('serviceCidr') }}</em>
      </div>

      <div class="k8s-deploy-section">其他配置</div>

      <div class="k8s-deploy-field">
        <span>节点命名模式<b>*</b></span>
        <div class="k8s-deploy-choices">
          <label v-for="option in NODE_NAMING_OPTIONS" :key="option.value">
            <input v-model="form.nodeNaming" type="radio" name="deploy-node-naming" :value="option.value" />
            {{ option.label }}
          </label>
        </div>
        <em class="k8s-deploy-hint">
          自动命名由平台按角色与顺序生成：控制面 master-01、master-02…，工作节点 worker-NN；
          手动命名则到第 3 步「新增节点」逐台填写。两种模式都会把节点名写成目标机的 hostname
          （写进 /etc/hostname，重启后依然生效），因此机器上的 hostname 与集群里的节点名始终一致。
        </em>
      </div>

      <label class="k8s-deploy-field k8s-deploy-full">
        <span>描述</span>
        <textarea v-model="form.description" placeholder="记录集群用途、负责人或安全组说明，可留空。"></textarea>
      </label>
    </div>
  </div>
</template>
