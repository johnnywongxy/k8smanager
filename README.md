# K8s 统一管理平台

一套面向企业内网 Kubernetes 运维场景的集群全生命周期管理平台。后端基于 Django REST Framework，
前端基于 Vue 3 + Vite + TypeScript，把「装集群、看集群、管集群、审集群」收在一处：
从平台化部署一套新集群，到多集群纳管、节点与工作负载运维、机器直达 SSH，
再到按部门细分的权限策略与全量操作审计。

- 默认访问地址：http://127.0.0.1:5173
- 默认账号：`admin` / `admin123`（仅限本地开发，生产环境请立即修改）
- 非常感谢https://github.com/easzlab/kubeasz.git 本系统采用kubeasz实现，已经通知开源作者，后续系统挂到kubeasz的链接。

部署的k8s组件都放在我自己的oss上面了，so有想二次开发的小伙伴给我说下，我给同步下kubeasz对应的组件，这个目录是确定的，目前oss的信息是阿里云的oss，私有的。

![公众号](images/gzhh.jpg)![k8s部署微信沟通群](images/二维码.png)


## 界面预览

平台内置 **星云夜 / 深海蓝 / 星云白 / 天青白** 四套主题（默认「深海蓝」深色）。下列截图统一使用
**天青白** 浅色主题，界面清爽、数据卡片与表格层次分明；截图资源都在 `images/` 目录下，可直接用于
README 与项目文档。

登录页：

![登录页](images/01-login.png)

总览（跨集群的集群 / 节点 / 命名空间 / Pod / 工作负载总览，含资源规模、Pod 状态分布与证书到期）：

![总览](images/02-dashboard.png)

集群接入与部署集群：

![集群接入](images/03-clusters.png)

![部署集群](images/04-deploy-step1.png)

集群资源（节点管理、命名空间、部署基线）：

![节点管理](images/08-nodes.png)

![命名空间](images/09-namespaces.png)

![部署基线](images/10-config.png)

工作负载（工作负载、Pod 管理、镜像管理）：

![工作负载](images/11-workloads.png)

![Pod 管理](images/12-pods.png)

![镜像管理](images/13-images.png)

网络与存储（服务与入口、配置与密钥）：

![服务与入口](images/14-network.png)

![配置与密钥](images/15-configs.png)

应用交付与可观测性（Helm 管理、事件中心、资源统计、操作审计）：

![Helm 管理](images/16-helm.png)

![事件中心](images/17-events.png)

![资源统计](images/18-statistics.png)

![操作审计](images/19-audit.png)

机器管理与用户管理：

![机器列表](images/20-machines.png)

![部门管理](images/21-departments.png)

![权限策略](images/22-permission-tree.png)

平台管理与日志管理：

![平台设置](images/23-settings.png)

![系统状态](images/25-system-status.png)

![菜单排序](images/24-menu-order.png)

![平台工具](images/26-system-tools.png)

![用户日志](images/27-user-logs.png)

![系统日志](images/28-system-logs.png)

---

## 一、开发环境与版本

### 1.1 基础环境

| 项目 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.12 | 后端运行时 |
| Node.js | ≥ 22（实测 v24.13.0） | 前端开发服务器与构建 |
| npm | ≥ 10 | 前端包管理 |
| Django | 4.2 LTS | Web 框架 |
| 数据库 | SQLite 3 | 默认内置，开箱即用 |
| kubeasz | 3.6.7 | 被纳管的集群编排工具 |
| Kubernetes | 1.33 ~ 1.36 | 平台支持部署的集群版本 |
| 浏览器 | Chrome / Edge 最新版 | 前端为 SPA 应用，终端页依赖 WebSocket |

### 1.2 后端依赖（`backend/requirements.txt`）

```
Django>=4.2,<5.0
django-cors-headers>=4.9,<5.0
djangorestframework>=3.17,<4.0
gmssl>=3.2.2,<4.0
openpyxl>=3.1,<4.0
psutil>=7.0,<8.0
PyJWT>=2.12,<3.0
requests>=2.33,<3.0
channels>=4.2,<5.0
daphne>=4.2,<5.0
kubernetes>=32.0,<33.0
PyYAML>=6.0,<7.0
oss2>=2.19,<3.0
cryptography>=44.0,<46.0
icmplib>=3.0,<4.0
paramiko>=3.5,<5.0
```

### 1.3 前端依赖（`front/package.json`）

运行依赖：

- `vue` ^3.5.34、`vue-router` ^4.6.4、`pinia` ^3.0.4
- `echarts` ^6.1.0 — 总览与资源统计的图表渲染
- `@xterm/xterm` ^6.0.0、`@xterm/addon-fit`、`@xterm/addon-webgl` — Pod 终端与机器 SSH 终端
- `html2canvas` ^1.4.1、`jspdf` ^4.2.1 — 报表导出
- `@vuepic/vue-datepicker` ^14.0.0、`date-fns` ^4.4.0 — 日期选择
- `lucide-vue-next` ^0.577.0 — 图标库
- `qrcode` ^1.5.4 — 二维码（OTP 绑定）

开发依赖：

- `vite` ^7.3.1、`@vitejs/plugin-vue` ^6.0.7
- `typescript` ^5.9.2、`vue-tsc` ^3.3.2

---

## 二、技术架构

### 2.1 技术栈

| 层次 | 技术选型 |
| --- | --- |
| 后端框架 | Django 4.2 + Django REST Framework 3.17 |
| 数据库 | SQLite 3（通过 Django ORM 访问，开启 WAL） |
| 实时通道 | Channels 4 + Daphne（Pod 日志、进入容器、机器 SSH 终端走 WebSocket） |
| 集群对接 | kubernetes（官方 Python 客户端），按集群加密托管 kubeconfig |
| 集群编排 | kubeasz + Ansible，由独立的部署 Worker 串行执行 |
| 远程执行 | paramiko（SSH），密码优先、部署密钥回落 |
| 身份认证 | JWT（PyJWT）+ 图形验证码 + 滑块验证，可选 OTP 二次验证 |
| 前端框架 | Vue 3 + TypeScript + Vite 7 |
| 状态管理 | Pinia |
| UI 样式 | 手写 CSS + CSS 变量主题（星云夜 / 深海蓝 / 星云白 / 天青白四套） |
| 图表 / 导出 | ECharts 6、html2canvas、jsPDF、openpyxl |
| 加解密 | gmssl（国密 SM4，用于 kubeconfig、机器密码等敏感配置落库加密） |
| 运维探测 | icmplib / psutil / requests（ping、telnet、curl、traceroute、mtr） |

### 2.2 目录结构

```
k8smanager/
├── backend/                    # Django 后端
│   ├── backend/                # 项目配置：settings / urls / asgi / wsgi
│   ├── ops/                    # 平台核心：用户、组织、角色、权限策略、系统设置与系统工具
│   ├── identity/               # 身份认证相关实现（验证码、OTP、密码策略等）
│   ├── k8s_cluster/            # 集群接入：kubeconfig 托管、连通检测、证书到期
│   ├── k8s_deployment/         # 平台化部署：预检、七阶段编排、部署 Worker、备份与销毁
│   ├── k8s_machine/            # 机器管理：资产清单、水位采集、SSH 终端
│   ├── k8s_resource/           # 集群资源：节点、命名空间、工作负载、Pod、Service、存储等
│   ├── k8s_config/             # 配置中心：版本对应关系（部署基线）、集群配置
│   ├── k8s_platform/           # 高级能力：Pod 日志、进入容器等基于 WebSocket 的能力
│   ├── k8s_observability/      # 可观测性：事件中心、资源统计
│   ├── k8s_audit/              # 操作审计
│   ├── data_security/          # 数据安全与国密加解密能力
│   ├── platform_logs/          # 用户操作日志与系统日志
│   ├── scripts/                # 运维脚本
│   ├── requirements.txt        # 后端依赖清单
│   └── manage.py               # Django 管理入口
├── front/                      # Vue 3 前端
│   ├── src/                    # 源码：views / components / stores / styles / utils
│   ├── package.json            # 前端依赖与脚本
│   └── vite.config.ts          # Vite 配置（含 /api 与 /ws 开发代理）
└── images/                     # README 截图资源
```

---

## 三、功能模块

平台左侧菜单分为 **总览、K8s 管理、机器管理、用户管理、平台管理、日志管理** 六大板块，权限均通过「权限策略」细粒度控制到页面与按钮。

### 3.1 总览

跨全部已接入集群的运营看板：集群 / 节点 / 命名空间 / Pod / 工作负载计数与异常数，运维操作趋势，资源规模，集群运行状态，Pod 状态分布，以及各集群客户端证书的到期倒计时。

![总览](images/02-dashboard.png)

### 3.2 K8s 管理（核心）

- **集群接入**：纳管多个集群，kubeconfig 经国密 SM4 加密落库，维护 API Server、凭据与备注，支持连通测试与同步、证书到期提醒。列表按集群展示版本、节点数、状态与最近检测结果。
- **任务中心**：平台化部署集群的任务清单，七阶段时间轴 + 中文日志，失败点、执行命令与输出都在任务详情里。
- **部署集群**：四步向导式部署一套新集群（集群信息 → 高级配置 → 新增节点 → 信息确认），
  详见 [四、部署一套新集群](#四部署一套新集群四步向导)。

  ![集群接入](images/03-clusters.png)

- **集群资源**：
  - **节点管理**：节点状态、内网 IP、Kubelet 版本，支持编辑标签、禁止 / 恢复调度、驱逐、删除。
  - **命名空间**：命名空间清单与标签查看，支持新增、编辑、删除。
  - **Namespace 授权**：面向多租户的命名空间级授权规则维护。
  - **部署基线**：维护「Kubernetes 版本 ↔ kubeasz 版本 ↔ 支持的操作系统」对应关系，部署向导的版本下拉即取自这里。

  ![节点管理](images/08-nodes.png)

  ![命名空间](images/09-namespaces.png)

  ![部署基线](images/10-config.png)

- **工作负载**：
  - **工作负载**：Deployment / StatefulSet / DaemonSet / Job / CronJob 统一视图，支持扩缩容、重启、手动触发、暂停与恢复、批量操作。
  - **Pod 管理**：Pod 状态、IP、重启次数、所在节点，支持查看日志、进入容器（WebSocket 终端）、批量删除。
  - **镜像管理**：各节点已缓存的镜像清单，含镜像大小与别名数，支持导出。

  ![工作负载](images/11-workloads.png)

  ![Pod 管理](images/12-pods.png)

  ![镜像管理](images/13-images.png)

- **网络与存储**：
  - **服务与入口**：Service 与 Ingress 清单，含类型、ClusterIP、端口与外部地址。
  - **配置与密钥**：ConfigMap 与 Secret 清单，Secret 内容按权限单独放行查看。
  - **存储管理**：PV / PVC / StorageClass 等存储资源。

  ![服务与入口](images/14-network.png)

  ![配置与密钥](images/15-configs.png)

- **应用交付**：**Helm 管理** 查看各命名空间下的 Release 状态、Chart 及版本、Revision 与描述，支持更新与卸载。

  ![Helm 管理](images/16-helm.png)

- **可观测性**：
  - **事件中心**：汇总各集群的 Warning 等事件，含对象、原因、次数与时间。
  - **资源统计**：跨集群的资源计数与图表分布。
  - **操作审计**：记录集群 / 资源 / 动作 / 状态 / 操作者 / 时间，支持按条件检索与导出。

  ![事件中心](images/17-events.png)

  ![资源统计](images/18-statistics.png)

  ![操作审计](images/19-audit.png)

### 3.3 机器管理

- **机器列表**：维护部署目标机器的资产清单（主机名、IP、SSH 端口与用户、角色、所属集群），展示 CPU / 内存 / 磁盘水位与最近检测时间；支持连接测试、编辑、删除，以及**在弹窗内直接 SSH 登录到那台机器**（终端支持最大化 / 最小化，会话与每条命令全程留痕）。

![机器列表](images/20-machines.png)

### 3.4 用户管理

- **用户管理**：维护平台账号、角色与所属部门，支持启用 / 禁用、重置密码、OTP 二次验证绑定；密码策略（长度、复杂度、有效期）由平台统一配置并生效。
- **部门管理**：维护组织架构与层级关系，支持部门成员归属调整，兼容「组织管理」与「部门管理」两种视图。
- **用户报表**：按部门、时间维度统计用户登录与操作情况，支持报表生成与导出。
- **权限策略**：基于 RBAC 的策略与规则管理。权限树与左侧菜单、页面按钮一一对应，可按一级菜单、页面、动作三级分别授予「允许 / 拒绝」；支持部门授权与个人授权、优先级与继承，父级设置会同步到下级页面和按钮。

![部门管理](images/21-departments.png)

![权限策略](images/22-permission-tree.png)

### 3.5 平台管理

- **平台设置**：平台名称、Logo、备案信息、访问白名单、密码策略、登录策略（验证码 / 滑块 / OTP / 短信）、水印、通知渠道、对象存储与 License 等一站式配置。
- **菜单排序**：管理员可调整各板块在左侧菜单中的展示顺序。
- **系统状态**：展示后端资源占用（CPU / 内存 / 磁盘）、网络吞吐与在线用户趋势等关键指标。
- **平台工具**：内置 Ping、Telnet、Curl、Traceroute、MTR 等常用网络运维探测工具，便于排查节点与集群链路连通性。

![平台设置](images/23-settings.png)

![系统状态](images/25-system-status.png)

![菜单排序](images/24-menu-order.png)

![平台工具](images/26-system-tools.png)

### 3.6 日志管理

- **用户日志**：集中记录用户登录、登出行为，包含登录方式、IP、时间、成功 / 失败状态及失败原因；支持按时间范围检索与导出。
- **系统日志**：记录平台关键操作审计，如集群接入、部署任务、机器变更、权限调整与系统设置变更等，满足运维可追溯要求。

![用户日志](images/27-user-logs.png)

![系统日志](images/28-system-logs.png)

---

## 四、部署一套新集群（四步向导）

在「集群接入」页点右上角的 **部署新集群** 进入向导。向导只负责收集参数，真正的安装由独立的
部署 Worker 串行执行；四步之间可以随时用「上一步」回退，表单状态在同一个页面里保留。

### 4.1 第 1 步：集群信息

![部署集群 · 集群信息](images/04-deploy-step1.png)
![部署集群 · 集群信息](images/高级配置01.png)
![部署集群 · 集群信息](images/高级配置02.png)
![部署集群 · 集群信息](images/高级配置03.png)
![部署集群 · 集群信息](images/高级配置04.png)
![部署集群 · 集群信息](images/高级配置05.png)
![部署集群 · 集群信息](images/高级配置06.png)
![部署集群 · 集群信息](images/高级配置07.png)
| 字段 | 说明 |
| --- | --- |
| 部署名称 | 集群的唯一名字，与「集群接入」里已有集群重名会当场拦下 |
| Kubernetes 版本 | 候选项来自「部署基线」（配置中心）维护的版本对应关系，括号里是配套的 kubeasz 版本。这里没得选就说明部署基线里还没维护对应关系 |
| 容器运行时 | 固定 containerd：Kubernetes 1.24 起 kubelet 已移除 Dockershim，平台不再支持 Docker 作为运行时 |
| 操作系统 | 只列出当前 Kubernetes 版本已关联并启用的系统，例如 Ubuntu 20.04 |
| 数据目录 | 不勾选「自定义数据目录」时使用平台默认的 `/var/lib`，etcd / containerd / kubelet 三个目录都在其下 |
| 容器网络插件 | Calico 或 Flannel；建好后仍可更换，但会导致 Pod 网络中断 |
| 节点网口 | 节点上承载集群通信的网卡名，例如 `eth0` |
| 容器网络 CIDR / Service IP 段 | 集群的两个大网段，创建后不可更改，且两者不能重叠 |
| 节点命名模式 | 自动命名（`master-01`、`worker-NN`）或手动命名。两种模式都会把节点名写进目标机的 `/etc/hostname`，让机器上的 hostname 与集群里的节点名始终一致 |
| 描述 | 记录集群用途、负责人或安全组说明，可留空 |

### 4.2 第 2 步：高级配置

![部署集群 · 高级配置](images/05-deploy-step2.png)

- **集群形态**：默认单节点（一台机器同时承担 etcd、master 与 node），勾选「新建多节点集群」后
  etcd 与 APIServer 以多副本部署，建议 master 节点数为 3。
- **监听端口**：ApiServer 监听端口，默认 6443，取值范围 1–65535。
- **Kube-proxy 模式**：候选项跟着该版本用的 kubeasz 制品走 —— 3.6.x 及更早支持 `ipvs` 与
  `iptables`（默认 `ipvs`，转发性能更好、负载策略更均衡），3.7.0 起换成了 `iptables` 与
  `nftables`（默认 `iptables`，选修 `nftables` 要求节点内核 ≥ 5.13 且 nft ≥ 1.0.0，预检会逐台核对）。
  界面只列可用值，真正拒绝非法组合的是后端，提交与预检各拦一次。**选择后无法修改**。
- **自定义证书有效期**：不开启时沿用 kubeasz 的默认值；开启后分别指定根证书（界面默认 10 年）与
  客户端 / 组件证书（默认 3 年，比 kubeasz 的 50 年短得多 —— 这些证书不会自动轮换，一签就该有人
  记得去换）；根证书年限不得短于客户端与组件证书。
- **组件配置**：Metrics Server、Nginx Ingress、Helm 三个开关，默认全部启用。Helm 开关会把 Helm
  客户端装到集群各节点上（与 kubectl 同一目录，已在系统 PATH 中），「Helm 管理」页即从这里取数。
- **外部访问入口**：多节点集群必选，二选一 —— **自建 l4lb + keepalived**（平台在两台独立机器上
  安装 nginx 与 keepalived，对外提供你填的 VIP）或 **云内网负载均衡**（复用云厂商的 LB，填负载
  均衡器地址，平台只负责把配置写对）。

### 4.3 第 3 步：新增节点

![部署集群 · 新增节点](images/06-deploy-step3.png)

点「添加节点」从机器列表里挑机器：

![部署集群 · 添加节点](images/07-deploy-step3-picker.png)

- 候选清单就是「机器管理 → 机器列表」里的机器，支持按主机名 / IP / 登录用户名 / 所属集群搜索；
  已经在本次节点列表里的机器会从候选里排除，已属于别的集群的机器仍可选，但会给出改绑提示。
- **角色默认由平台自动分配**（表格里「角色」一列会标上「自动」）：单节点集群那一台机器必然是
  etcd + master + node，开关此时是锁死的；多节点集群则是前 3 台承担 etcd + master + node、
  其余为纯 node，节点名按顺序编号成 `master-01`、`master-02`… 与 `worker-NN`。
  取消勾选「自动分配角色」才回到手工模式（多节点集群才能取消）—— 手工勾容易勾出后端不接受的
  组合（master 少于 3 台、etcd 台数为偶数），而且删掉中间一台后整份清单的角色都得重排。
- 第 2 步选了「自建 l4lb + keepalived」时，表格会多出一列「兼任负载均衡」：**只有承担 master 的
  节点**可以勾，勾上的机器在角色里多一个 `loadbalancer`，与第 2 步单独指定的负载均衡机器
  **合计**承担集群的对外入口。切换自动 / 手工、增删节点都会重排这份合计，已勾选的状态跟着节点走。
- 添加/移除节点的当下就会调用后端绑定/解绑接口，因此「机器管理 → 机器列表」里的角色与所属集群
  和向导始终一致；移除节点会自动解绑，恢复成「未分配」。
- 向导里还挂着「已绑定但尚未真正部署」的机器时，点「取消」或直接切走页面都会先弹确认框，说明
  这几台机器会被解绑、角色与所属集群清空；解绑失败还会给一条「仍然离开，稍后到机器列表手动
  清理」的退路，不会把人卡死在向导里。

### 4.4 第 4 步：信息确认与预检

这一步把前三步的配置摊成一份摘要，并把问题分成两类：

- **阻塞项**：前三步的字段校验没过，或预检发现的问题，按「第几步 + 提示」列出来，逐条点回对应
  步骤修改；
- **提醒**：不阻断提交，但值得看一眼，例如节点配置上的取舍。

预检分两档：

1. **配置预检**（进入这一步自动执行）：校验版本与系统的对应关系、网段是否合法、制品能力是否
   覆盖所选组件，并算出候选集群标识回填到表单 —— 提交时带着它走，机器表里的归属、预检里展示
   的目录名与真正创建出来的集群三者才是同一个值。
2. **完整预检**（点按钮触发）：会现下载一次 kubeasz 制品核对制品能力（所选 CNI 与组件在制品里
   是否齐全），再逐台 SSH 登录目标节点做只读检查 —— SSH 连通性与 sudo 权限、系统版本、磁盘余量、
   内核与 kube-proxy 所需模块、端口占用、时钟偏差、集群网段合法性、节点之间的网络互通与 etcd
   互通、还残留着哪些上一次的安装。耗时几十秒；通过后后端签发一份一次性凭据，**提交部署必须
   带上它**。

> 检测到机器上还残留着上一个集群的数据（etcd 数据、证书、占着端口的 apiserver）时，平台会在
> 部署前先清干净再装。只清 K8s 侧、保留镜像缓存；本来干净就直接装，不空转。

### 4.5 提交之后

点「开始部署」后任务进入「任务中心」，由部署 Worker 单进程串行执行，页面上是一条阶段时间轴加
中文日志 —— 七个主阶段依次是系统准备 → 部署 etcd → 部署容器运行时 → 部署 Master → 部署 Node
→ 部署网络插件 → 部署集群插件；勾了外部负载均衡的集群还会多一个可选的「部署外部负载均衡」
（kubeasz 的 `10.ex-lb`，必须等集群起来才跑，因为它要指向 apiserver）。失败点、执行的命令与
输出都在任务详情里，并会讲清「要不要重装」。

装完之后平台自动完成这几件事：登记集群记录、生成配置版本、把 kubeconfig 加密托管（国密 SM4）、
登记证书到期时间并开始倒计时。此后「集群接入」列表里就能看到它，可以继续扩容、升级与日常运维。

> 任务已经装到一半才失败时，不必手工去机器上收拾：任务详情里会给出失败点，修正后重试即可，
> kubeasz 的各阶段是幂等的。若只是最后一步「登记集群」失败（集群其实已经装好了、平台却没记上），
> 用 `python manage.py register_deployment <任务ID>` 做补登记，几秒钟就能回填，不用重跑整轮。

---

## 五、快速开始

### 5.1 启动后端

```bash
cd backend

# 1. 创建并激活虚拟环境（可选但推荐）
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# 2. 安装依赖
pip install -r requirements.txt

# 3. 初始化数据库
python manage.py migrate

# 4. 创建默认管理员 admin / admin123
python manage.py seed_demo

# 5. 启动服务，端口需与前端代理保持一致（默认 8000）
python manage.py runserver 0.0.0.0:8000
```

> **关于 `runserver`**：项目在 `INSTALLED_APPS` 中启用了 `daphne`，`manage.py runserver` 会自动切换到
> ASGI 版本，Pod 日志、进入容器、机器 SSH 这几条 WebSocket 通道才能连上。用 `gunicorn` 等 WSGI
> 服务器部署时，需要一并改用 ASGI 部署方式，否则这些页面会一直停在「已断开」。

### 5.2 启动部署 Worker（需要平台化部署集群时）

部署任务不是由 Web 进程执行的，而要另起一个 Worker 串行消费任务队列：

```bash
cd backend
python manage.py run_deploy_worker
```

> 只允许一个 Worker、一次只跑一个任务：kubeasz 的 `ezctl` 会在每个任务自己的工作区里跑 Ansible，
> 而 Ansible 的 inventory 与 playbook 是进程级共享文件，两个任务并发跑同一个集群必然互相覆盖配置。
> 因此重复启动的 Worker 会在抢锁失败后直接退出。

### 5.3 启动前端

> **部署注意事项**：前端开发服务器通过 `front/vite.config.ts` 中的代理把 `/api` 与 `/ws` 转发到后端。
> **此地址必须与你实际启动 Python 后端的地址一致**，否则前端页面会报接口连接失败、终端页会一直黑着。
>
> 当前默认配置为 `http://127.0.0.1:8000`。若后端部署在其他服务器，请改为对应 IP 与端口：
>
> ```ts
> // front/vite.config.ts
> const BACKEND_ORIGIN = 'http://127.0.0.1:8000'   // HTTP 与 WebSocket 两个代理都从这一行派生
> ```

```bash
cd front

# 1. 安装依赖
npm install

# 2. 启动开发服务器（默认 http://127.0.0.1:5173）
npm run dev
```

### 5.4 生产构建

```bash
cd front
npm run build      # 依次执行资源审计、文档审计、类型检查，产物输出到 front/dist
```

生产部署时，请确保前端能正确访问到后端 API：

- 若使用 Django 托管：`backend/backend/settings.py` 中的 `FRONT_DIST_DIR` 指向 `front/dist`，关闭 `DEBUG` 后 Django 会直接托管静态文件并代理 API。
- 若使用 Nginx 托管：将 `front/dist` 作为站点根目录，并把 `/api` 反向代理到后端实际地址、把 `/ws` 反向代理到同一地址的 WebSocket 侧（例如 `http://127.0.0.1:8000`）。

> 生产构建产物中不会再读取 `vite.config.ts` 的代理配置，因此 Nginx 或部署环境的反向代理地址务必与后端监听地址保持一致，
> 且 `/api` 与 `/ws` 必须指向同一个后端 —— 只改其中一个，页面看着正常、终端却是断的。

### 5.5 登录

浏览器打开 http://127.0.0.1:5173，使用 `admin` / `admin123` 登录。
登录页支持图形验证码与滑块验证；OTP 二次验证、短信登录可在「平台管理 → 平台设置」中开关。

---

## 六、开源协议

本项目基于 [Apache License 2.0](LICENSE) 开源，欢迎 Star、Issue 与 Pull Request。
