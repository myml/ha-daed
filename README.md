# daed for Home Assistant

[daed](https://github.com/daeuniverse/daed) 是 [dae](https://github.com/daeuniverse/dae)（基于 eBPF 的 Linux 透明代理）的 Web 面板。
本集成通过 daed 的 GraphQL API 把 dae 的运行状态接入 Home Assistant：

- 实时上传/下载速率、累计流量、活跃连接数、UDP 会话数
- dae 是否运行、配置是否有未应用的修改、当前选中的配置
- 分组（节点数 + 负载均衡策略）、订阅（状态、cron、更新时间）
- 每个节点的延迟与可达性（daed 自己测得的缓存值）
- 动作：应用配置、重新测试节点延迟、更新订阅、切换配置
- 诊断信息（自动隐藏密码）

## 安装

### HACS（自定义仓库）

1. HACS → 集成 → 右上角 ⋮ → 自定义存储库
2. 添加 `https://github.com/myml/ha-daed`，类别选「Integration」
3. 安装后重启 Home Assistant

### 手动

把 `custom_components/daed` 复制到 Home Assistant 的 config 目录下的 `custom_components/`，然后重启。

## 配置

设置 → 设备与服务 → 添加集成 → 搜索 **daed**，填写：

| 字段 | 说明 |
| --- | --- |
| daed base URL | 例如 `http://10.0.0.1:2023`，可省略 `http://`，末尾 `/graphql` 会被自动去掉 |
| 用户名 / 密码 | daed 面板的登录账户。集成用它换取 JWT 并自动续期 |
| 校验 SSL 证书 | 自签名证书时关闭 |

> 首次登录 daed 时 daed 会要求创建管理员账户，用那组账户即可。

`aiohttp` 已随 Home Assistant 提供，集成不需要额外依赖。

### 选项

设备与服务 → daed → 配置：

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| 运行状态轮询间隔 | 30 秒 | 实时流量与 dae 状态的读取频率（最小 10 秒） |
| 节点延迟轮询间隔 | 600 秒 | 读取 daed 缓存的节点延迟；设为 `0` 则只由动作触发 |

## 实体

设备：`daed`（制造商 daeuniverse，model `dae / daed`，固件版本 = dae 版本）。

### 传感器

| 实体 | 说明 |
| --- | --- |
| `sensor.daed_download_rate` / `upload_rate` | 实时速率（原生 B/s，界面显示 kB/s） |
| `sensor.daed_downloaded` / `uploaded` | 累计字节（原生 B，显示 GB，`total_increasing`） |
| `sensor.daed_active_connections` / `udp_sessions` | 连接数 |
| `sensor.daed_groups` / `subscriptions` | 分组数 / 订阅数 |
| `sensor.daed_group_<name>_nodes` | 某分组的节点数，`policy` 属性为均衡策略 |
| `sensor.daed_subscription_<name>` | 订阅健康度 `ok` / `error`，属性带原始 status、cron 等 |
| `sensor.daed_node_<node>_latency` | 节点延迟（ms），属性 `alive`、`tested_at` |
| `sensor.daed_config` / `version` | 诊断信息；`version` 的属性带上 `last_updated`（上次轮询时间）、`running`、`modified` |

### 二进制传感器

| 实体 | 说明 |
| --- | --- |
| `binary_sensor.daed_dae_running` | dae 进程是否在运行 |
| `binary_sensor.daed_config_modified` | 配置有改动但尚未应用（`problem`） |
| `binary_sensor.daed_subscription_errors` | 存在失败的订阅（`problem`） |
| `binary_sensor.daed_node_<node>_reachable` | 节点可达（`connectivity`） |

分组、订阅和节点实体在首次加载时按当时的数据创建；新增节点后重载集成即可出现。

## 动作

| 动作 | 说明 |
| --- | --- |
| `daed.run` | 应用当前配置（`run(dry: false)`） |
| `daed.test_node_latency` | 让 daed 重新探测所有节点并刷新延迟实体 |
| `daed.update_subscriptions` | 刷新全部订阅，或用 `subscription_ids` 指定 |
| `daed.select_config` | 用 `config_id` 选中一个已保存的配置 |

多实例时通过 `entry_id` 指定目标。

## 实现说明

- **认证**：`token(username, password)` 换取 JWT，缓存后以 `Authorization: Bearer` 调用；daed 拒绝令牌（过期或 daed 重启）时自动重登一次并重试。
- **查询**：`general { dae runtimeOverview }` 取状态与计数，`nodes` / `groups` / `subscriptions` / `configs` 取拓扑，`nodeLatencies` 取延迟缓存。
- **计数语义**：daed 每次查询都会重算 `runtimeOverview`，`uploadTotal`/`downloadTotal` 是 daed 进程内的累计值，因此直接映射为 `total_increasing`，不做本地累加。
- **实时速率**：daed 顶层的 `uploadRate`/`downloadRate` 不可靠（实测大多数时候是 `0`，偶尔才有值），真正反映流量的是 `samples` 里的采样点。因此速率以 `samples` 为准：取最近 3 个采样点，**跳过其中为 0 的跳变**再求平均（daed 会在没有流量的采样刻度上写 0，直接平均会把满速线路拉低）；全部为 0 才判定为空载。只有在 daed 完全没返回采样时才退回顶层字段。
- **延迟**：轮询只读 daed 的缓存，不触发全节点探测；全量探测仅在 `daed.test_node_latency` 时发生。
- **令牌与密钥**：密码只保存在配置条目中，不写日志；诊断输出会脱敏。
- **避免刷爆活动日志**：上次轮询时间只是 `sensor.daed_version` 的属性。它每轮都会变化，若做成独立实体，就会每 30 秒往 recorder 写一行，而这一行没有任何信息量。从旧版本升级时，集成会自动清理遗留的 `sensor.daed_last_updated`。高频但确实有意义的实体（速率、累计流量、连接数）仍会记录；若仍嫌吵，可在 `recorder:` 里排除，或把轮询间隔调大。

## 开发

```bash
python -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest -q
```

测试通过一个脚本化的 daed GraphQL 传输层跑集成，完全离线。

## 许可

MIT。daed/dae 属于 daeuniverse 项目，见 [NOTICE](NOTICE)。
