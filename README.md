# Infra Lab

独立的个人运维实验工作台。拖拽 Nginx 和 HTTP 应用，写真实 `nginx.conf`，由 Docker 创建实例并执行真实请求。没有模拟运行指标，也不依赖面试题库。

## 开发

需要 Node.js 22+、Python 3.12+、uv。前端使用 npm 锁文件。

```powershell
cd D:\codes\infra-lab\frontend
npm ci
npm run dev
```

另一个终端：

```powershell
cd D:\codes\infra-lab\backend
uv sync --default-index https://pypi.org/simple
uv run --no-sync uvicorn infra.api:app --host 127.0.0.1 --port 8000
```

访问 Vite 提示的本地地址。Windows 可以编辑草稿；真实部署需 Linux Docker 执行器。未连接执行器时界面明确显示未知/不可执行。

## 部署到 Linux

在本地运行 `npm ci`、`npm run build`，将项目上传到服务器（包含 `frontend/dist`，排除 `.venv`、`node_modules`、`.git`、本地运行数据）。在服务器项目目录执行：

```sh
sh scripts/deploy.sh
docker compose ps
docker compose logs --tail=100 api worker
```

若构建阶段容器 DNS 无法解析包仓库，可只对本次镜像构建使用 `INFRA_BUILD_NETWORK=host sh scripts/deploy.sh`；不需要修改 Docker 全局网络配置。

部署脚本只构建本项目镜像并启动本项目 Compose。不会清理其他镜像、容器或数据。

控制 API 绑定服务器 `127.0.0.1:8088`。从自己的电脑建立隧道：

```sh
ssh -N -L 127.0.0.1:8088:127.0.0.1:8088 -L 127.0.0.1:8089:127.0.0.1:8089 root@YOUR_SERVER
```

浏览器打开 `http://127.0.0.1:8088`。保持 SSH 连接。首版不含账户系统，不应将 8088 暴露公网。API 与 Worker 挂载 Docker socket，属于受信任的主机管理组件；实验容器不挂载该 socket。

Windows 也可以运行 `./scripts/connect.ps1 -Server root@YOUR_SERVER`。如果本地 8088 被占用，增加 `-LocalPort 18088`。

## 第一次实验

1. 创建实验，载入“一进三出拓扑”，或拖入 Nginx 和应用节点，将 Nginx 右端连接到应用左端。
2. 保存草稿，然后应用拓扑。执行记录显示每一步，检查器区分容器和服务健康状态。
3. 在请求实验里选择 Nginx，设置请求数、并发、时间上限，发起真实请求。
4. 修改某条连接权重、应用后再次请求，对比实际分流。
5. 给应用增加 2600 ms 延迟；默认 Nginx 读取超时为 2 秒，可在 upstream 日志里观察超时重试。可停止某个应用再测试。
6. 在 Nginx 编辑器写错误配置并应用：`nginx -t` 失败，旧的运行配置保留，错误写入任务日志。

请求结果只统计实际完成的尝试。时限可能使完成数小于请求数；未知不显示为零。分布只统计响应中有实例标识的请求，错误率独立统计。Upstream 日志来自 Nginx；手动删除日志格式后不再承诺能解析重试路径。

## 配置与生命周期

- 生成模式：保存草稿时按连线生成 `nginx.conf`；手动编辑后进入手动模式，后续连线变化不覆盖文件。
- “重新生成 / 比较”提供差异视图，采用生成配置后回到生成模式。版本入口查看最近 30 个保存版本。
- 手动配置应使用非特权端口 8080、`/tmp` 下的 PID/临时目录，并保留 `/__infra_health` 返回 200。Nginx 以 UID 101 运行；不支持任意镜像、特权节点或宿主机挂载。
- 保存不部署；节点移动只影响布局。实时添加模式会为新增节点和连线提交应用任务；移除节点仍需明确应用草稿。
- 应用包含顺序执行的真实步骤，不是跨容器事务。失败可能留下已创建/更新节点；最近完整部署版本不代表失败任务完全没有副作用。检查任务和真实容器后重新应用或清理。
- 资源参数或应用响应行为改变会重建该节点；Nginx 配置改变使用校验和 reload。重新应用会启动处于停止状态的期望节点。
- Worker 使用持久化 SQLite 队列和单实例文件锁。运行中崩溃的任务标记为中断，不自动重放。重启后按双标签重新探测资源；最多并行执行两个不同实验的任务，同一实验始终串行。
- 每实验最多 10 节点，默认每节点 64 MB / 0.25 CPU；平台默认实验内存总预算 768 MB（`INFRA_TOTAL_MEMORY_MB`，由 Compose 注入 Worker）。配额按拓扑应用期间的峰值计算，包含未删除旧节点和 Nginx 校验容器。请求上限 1000、并发 20、时限 60 秒。
- 实验使用独立 internal Docker 网络，不发布业务端口。它用于个人受信任实验，不是对不可信多租户的强安全沙箱。
- 清理按钮仅删除同时匹配 `infra.owner=infra-lab-v1` 与实验 ID 的容器和网络；保留草稿、任务和配置历史。首版节点无持久业务数据，不创建数据库数据卷。
- 实验选择菜单中的“删除当前实验”会撤销网站入口，删除该实验双标签匹配的容器和网络，并永久移除草稿、配置、任务及请求历史。删除前需等待当前实验任务完成；该操作不可撤销，与只回收运行资源的“清理实验”不同。
- `docker compose down` 只停止控制服务，不会自动删除实验；请先在网页清理。不要使用全局 `docker system prune`。

## 验证

```powershell
cd backend
uv run --no-sync pytest -q
uv run --no-sync ruff check infra tests
cd ../frontend
npm run build
```

Linux 环境启动且隧道连接后，在项目根目录执行：

```powershell
backend\.venv\Scripts\python.exe scripts/acceptance.py --url http://127.0.0.1:8088
```

它创建专属实验，验证分流、改权重、无效配置保护、停机重试、慢响应、清理和重新部署。最后保留一个健康实验供检查；证据保存在忽略提交的 `artifacts/acceptance.json`。

发布回归使用统一入口，先保持管理端口 8088 和网站端口 8089 的 SSH 隧道：

```powershell
backend\.venv\Scripts\python.exe scripts/release_acceptance.py --ssh-server root@YOUR_SERVER --output artifacts/release-check.json
```

脚本创建两个同名、独立标记的实验：主实验包含 Nginx、两个应用、三级 Redis 复制链；隔离对照实验包含一个运行中的应用。它验证真实网站、GET/POST 表单、静态资源、Cookie、单次请求、SSE 序号续传、自定义响应、处理中配置快照、独立发布、无效配置保护、Nginx 重载与 503 重试、Redis 复制和整套启停。相同任务会重复提交以检查去重，并核对原有实验草稿和容器是否变化。通过后保留测试实验，供浏览器检查；失败会记录原因并保留当时的资源。`--output` 必须使用新文件，避免覆盖旧证据。

需要为新增的 7 个 64 MB 服务容器、校验容器和请求发生器预留额度；已停止实验容器也计入平台预算。额度不足时根据服务器资源调整 `INFRA_TOTAL_MEMORY_MB`，验收清理后恢复原值。验收期间避免从其他窗口修改原有实验，否则隔离检查会报告基线变化。

响应边界检查使用主实验内的 Nginx 临时文件，验证网站完整返回 2 MiB、单次预览只保留 1 MiB、10 秒请求超时、真实 503 页面、上传限制和缺日志时的部分链路；完成后恢复原 Nginx 配置：

```powershell
backend\.venv\Scripts\python.exe scripts/release_acceptance.py --phase limits --ssh-server root@YOUR_SERVER --output artifacts/release-check.json
```

确认没有其他实验任务执行后，恢复阶段显式允许重启控制服务：

```powershell
backend\.venv\Scripts\python.exe scripts/release_acceptance.py --phase recovery --ssh-server root@YOUR_SERVER --allow-control-restart --output artifacts/release-check.json
```

这一步会依次中断 Worker 请求任务、重启 API、重启网站入口采集器，验证中断状态、历史恢复及 SSE 续传；管理页面和网站连接会短暂断开。浏览器检查完成后，使用同一份证据清理脚本创建的两个实验：

```powershell
backend\.venv\Scripts\python.exe scripts/release_acceptance.py --phase cleanup --ssh-server root@YOUR_SERVER --output artifacts/release-check.json
```

清理前核对实验 ID 和本轮生成的名称，清理后核对双标签匹配的容器与网络均为零，并再次比较原有实验。脚本不会清理其他实验；`--ssh-server` 只使用已有 SSH 配置或免密登录，不接收密码和私钥。API 或入口端口不同可通过 `--url`、`--portal-url` 指定。

## 接口

交互式接口文档：`/docs`。核心接口：`/api/labs`、`/api/labs/{id}/draft`、`/revisions`、`/tasks`、`/events`、`/nodes/{node_id}/logs`。节点和连线作为带乐观版本检查的完整拓扑保存；任务使用 request_id 去重，保存提交时的拓扑快照。每实验最多保留 500 条完整终态任务记录；更早记录会归档为轻量去重凭据，同一请求标识不会因此被重复执行。SSE 负责观测，不根据画布连线伪造运行状态。

## 排错

- 引擎未连接：检查 `docker compose logs worker`、Docker socket 和 `/api/health`。
- 节点不健康：打开容器日志，检查手动配置的端口和健康路径。容器保留用于排查。
- 配置不生效：对比草稿版本、最近完整部署版本及任务状态；读取日志确认校验或 reload 的具体错误。
- 资源配额不足：清理不再需要的实验，或根据服务器实际余量调整平台预算。
- SSE 断线：保留最后一次观测并标为过期；页面重新连接后继续更新。未保存的编辑会触发离开提醒。
- 无法访问：确认 SSH 隧道和端口，勿通过公网 IP 直接打开管理服务。

Redis 主从已实现；Sentinel 自动切换、Redis Cluster 与 MySQL 复制尚未实现。

## 客户端预览、启停与链路

在下方“客户端”选择已部署的 Nginx，输入 `/page` 或相对路径及查询参数，点击“单次请求”。请求由实验网络内的受限发生器执行；页面预览、响应详情、放大预览共用这一次响应，不会重新请求后端。HTML 使用无权限 sandbox 和严格 CSP 静态展示，不执行脚本、不加载外部资源、不支持页面内跳转；JSON 格式化展示。响应体最多 1 MB，总请求时限 10 秒；HTTP 503 等响应仍展示真实正文，截断与传输失败单独标识。

工具栏的“启动实验”和“停止实验”只操作当前实验已有服务容器。启动先应用后 Nginx，并检查健康；停止先 Nginx 后应用。容器、网络、配置和历史均保留，不部署草稿、不升级镜像；空实验应先“应用拓扑”。部分失败逐节点记入任务记录，并将任务标记为失败。

“请求链路”新入口默认关闭，同时作用于客户端单次请求与该入口后续浏览器请求。开启后显示已收到的客户端、应用事件；Nginx 完成日志提供实际 upstream 地址、状态及重试顺序。回放是基于日志的事后还原，不表示网络包的实时位置。尚未观测的步骤显示等待确认；停止容器的 IP 若来自历史记录，会标注“上次地址”，未知节点保留原地址。客户端超时不表示应用取消；采集器最多收尾到任务执行后约 25 秒，缺失结束事件时保留不完整状态。

关闭开关立即隐藏覆盖层；已开始的追踪继续收尾，后续请求不新增详细事件。批量压测不启用逐请求动画。最近 100 次已结束请求与其事件持久化保留；排队和执行中的请求不会被清除。Worker 中断后标为 interrupted，不自动重发请求；失败现场只在明确清理时移除。

本功能使用 `infra-lab-app:1.2.0`。旧实验会显示能力提示，需要主动重新“应用拓扑”才升级应用容器；已有草稿中的修改也会一起应用，请先核对。控制服务升级只增量建表，不自动重建实验。

新增任务类型：`start_lab`、`stop_lab`、`client_request`。单次请求参数为 `node_id`、`path`、`trace`；任务 ID 同时作为服务端生成的请求 ID。请求列表 `/api/labs/{id}/requests`，详情 `/requests/{request_id}`，事件流 `/requests/events?after={seq}`；支持 `Last-Event-ID`，按全局递增 seq 续传及去重。

真实验收脚本 `scripts/client_acceptance.py` 创建独立实验，`scripts/client_recovery.py` 使用其产物继续验证 SSE 续传和 Worker 中断。恢复脚本会重启本项目 Worker，运行前应确保没有其他实验任务在执行。仍通过原 SSH 隧道访问，不增加公网端口。

已完成的本地和真实服务器验收见 [VERIFICATION.md](VERIFICATION.md)。配置支持展开编辑，实验结果可以切换历史运行进行比较。

## 真实网站入口与工作台

在“客户端”选择已部署 Nginx，点击“打开网站”。新标签页通过随机 `.localhost` 域名访问本机 8089 隧道，再由入口代理转发到实验 Nginx。支持刷新、链接、GET 查询、POST 表单、JavaScript、同源资源和 Cookie；内置应用首页提供这些操作。已有实验需要主动“应用拓扑”升级到应用 1.2.0，启动实验不会升级镜像或应用草稿。

入口代理仅监听服务器 127.0.0.1:8089。连接脚本同时转发 8088 和 8089；如果旧隧道占用端口，先关闭旧连接再运行脚本。管理页面与网站不同源，每个入口的 Cookie 限定当前主机。撤销入口使旧网址失效；停止或删除 Nginx 时返回不可用页面，不自动恢复容器。入口只映射当前实验的指定 Nginx，不能通过 URL 选择任意宿主机服务。代理使用 Linux host 网络访问实验私网，但实验服务没有新增宿主机公网端口。

网站请求直接转发，不进入部署队列，也不自动重试 POST。请求体上限 1 MB、整次请求最长 30 秒，最多 16 个同时处理的入口请求；响应流式发送，不截断为 1 MB。已发送响应头后的传输错误无法改写 HTTP 状态，工作台仍会单独标记传输失败。不支持 WebSocket、大文件上传或任意第三方网站透明代理。静态“单次请求”预览仍维持 1 MB/10 秒限制。

开启“请求链路”后，页面、表单及资源各自生成请求记录，来源为“网站”。选择一条查看已确认事件及事后路径；浏览器缓存没有到达代理时不会生成记录。浏览器请求不保存 Cookie、认证头、表单内容或完整响应正文。独立采集循环不受 Worker 部署任务排队影响，最长收尾 45 秒；采集器重启将未完成记录标为中断，不重发请求。关闭追踪后后续请求只有指标，不新增详细事件，已开始的追踪继续收尾。入口追踪设置持久化，新入口默认关闭。

工具栏左侧按钮可折叠节点库；选中节点后才显示检查器，可拖动左边界调整宽度或关闭。拖动画布与工具区之间的分隔条调整高度；底部支持收起、最大化与还原，尺寸保存在当前浏览器。客户端、请求记录、请求实验、执行记录和日志分别使用标签页，仅内容区滚动。

入口 API：`GET/POST /api/labs/{id}/entries`、`PATCH/DELETE /entries/{entry_id}`。PATCH 设置 `trace`；浏览器请求复用请求列表、详情与 SSE 事件接口，增加 `source`、`method`、`entry_id` 等元数据。数据迁移增量建表，不删除旧实验或历史。

验收脚本 `scripts/portal_acceptance.py` 创建独立实验，`scripts/portal_faults.py` 继续验证该实验并重启本项目 Worker/入口采集器。运行前确认无其他正在执行的任务；故障脚本会中断同时进行的网站采集。验收后通过平台“清理资源”回收其服务容器。

## Redis 主从实验

新建空实验，选择“Redis 一主两从”模板，或拖入 Redis 节点，从主节点右侧连接到从节点左侧。检查器的“复制来源”也可以调整关系。保存并应用拓扑后，每个节点运行固定镜像 `redis:7.4.7-alpine`；不向宿主机发布 6379。连接关系不能形成环，一个从节点只能有一个来源，也支持级联复制。

在“Redis 读写”选择已部署节点，可执行 GET、SET、DEL、TTL、DBSIZE、PING。SET 支持过期秒数，0 为不过期；键最长 256 字符、值最长 4096 字符。操作复用持久化任务和去重接口，Docker 内通过固定 argv 调用 redis-cli，不能输入任意命令、CONFIG 或宿主机脚本。操作历史保存键和值，仅用于测试数据。显示的耗时包含平台 Docker 执行开销，不作为 Redis 基准延迟。

运行角色、来源、连接状态和偏移来自实际 INFO replication，断连或过期时显示未知。Redis 复制是异步的，写入主节点成功不代表所有从节点已经可读到该值；从节点拒绝写入并返回真实 READONLY 错误。停止主节点后，从节点可继续读取已有数据，但不会自动提升为主节点；自动切主留待 Sentinel 阶段。参考 [Redis 官方复制说明](https://redis.io/docs/latest/operate/oss_and_stack/management/replication/)。

AOF 每秒落盘，数据放在容器可写层；普通停止/启动保留数据，异常崩溃可能丢失尚未同步到磁盘的写入。删除节点、清理实验或改变资源规格触发重建会清空该节点数据。改变复制来源可能通过全量同步替换从节点原数据。只修改复制连线不重建容器；取消复制会在同一实例执行 REPLICAOF NO ONE，保留现有数据。这些是实验环境数据，不提供备份或生产持久化保证。

默认实验总内存预算仍为 768 MB，已停止容器也占用预算；遇到配额不足时，先清理不用的实验资源。Redis 的 maxmemory 为容器预算的四分之一（至少 16 MB），策略为 noeviction，保留复制和 AOF 的内存余量。

`scripts/redis_acceptance.py` 创建专属验收实验，检查复制、只读、过期、启停、取消复制和重新连接；可用 `INFRA_REDIS_ACCEPTANCE_LAB` 指定之前失败的同名验收实验继续执行。脚本保留实验供浏览器检查，结束后需通过平台清理其资源。

## 应用自定义响应与 Nginx 重载

选中 HTTP 应用，在“响应设置”中切换“默认网站 / 自定义响应”。自定义响应作用于整个节点的业务路径，可以设置 200–599 状态码、JSON/文本/HTML 正文、延迟和响应头。点击“保存响应”只保存版本；“应用响应配置”会先保存修改，再通过独立任务热发布，不应用拓扑草稿或重建容器。切回默认网站并应用即可恢复原交互页面。默认网站故障参数仍通过应用拓扑生效；自定义模式不叠加随机错误。

每个请求开始时固定配置快照，已执行的请求继续使用旧版本。响应头 X-Infra-Node、X-Request-ID、X-Infra-Response-Version 标识实际应用与版本；正文保持原样。Nginx 可能重试、缓存或改写响应，应结合响应头及链路判断实际结果。HEAD 不发送正文，204/205/304 禁止正文；健康检查不受响应设置影响。

正文最多 64 KiB（UTF-8 字节），响应头最多 32 个、合计 8 KiB，支持重复 Set-Cookie。Content-Type 由正文类型决定，长度、连接和平台标识头不可覆盖。头值禁止控制字符，非 Latin-1 文本需自行编码。支持 GET/HEAD/POST/PUT/PATCH/DELETE/OPTIONS；不支持路径规则和响应序列。

界面分开显示未保存修改、已保存版本、运行版本与任务状态。保存使用版本冲突检测，运行版本来自真实健康探测；失败时回退，无法确认则显示未知。Worker 在原子发布后中断时，恢复后核对文件摘要与应用报告，不重放任务；任务仍可显示中断，但运行版本会独立更新。重建容器仅恢复已确认生效版本，不应用配置草稿。停止的应用不能发布配置。

本功能使用应用镜像 `infra-lab-app:1.3.0`。旧应用需要用户主动“应用拓扑”升级；控制服务升级不自动重建实验。应用镜像构建上下文已改为仓库根目录：`docker build -f services/app/Dockerfile -t infra-lab-app:1.3.0 .`，部署脚本已更新。版本校验代码由控制后端与应用镜像共享。

接口：`GET/PUT /api/labs/{lab}/nodes/{node}/response`，PUT 包含 `base_version` 与 `config`；任务 `apply_response` 包含 `node_id` 和 `response_version`，引用固定的已保存版本。响应配置独立于拓扑版本，增量建表保留旧历史。自定义响应和响应头属于用户配置，会保存在服务端数据库中，请只使用实验数据。

Nginx 检查器新增“重载”：校验并重载当前容器内配置，不提交草稿。只有正在运行且观测有效时可点击；校验错误保留现有运行服务。任务类型为 `reload`。节点及整套实验启停按钮按真实状态禁用无效操作，状态过期或任务执行中时禁用。

`scripts/response_acceptance.py` 验证真实响应与热更新；`scripts/response_recovery.py` 会短暂暂停其专属验收应用进程并重启本项目 Worker，验证回退与中断恢复，应在没有其他任务时执行。两者的验收实验结束后通过平台清理。
