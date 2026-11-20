# 科考船采样数据导入与复核服务

把甲板采样记录、样瓶交接单和实验室盐度结果整理成可查询、可追溯、可复核的采样表。

- 批量导入 HTML 与纯文本材料，自动识别材料类型与栏目（不同船次的列名、列序可不同）。
- 每条入库结果都带原文位置（材料、小节、页码、行号、原文摘录）与抽取置信度。
- 样瓶交接单划定送检批次；实验室结果与瓶一一配对。
- 整批配不齐的批次暂缓发布，其余批照常处理。
- 复核队列展示候选与原文；核正后结果立即反映到查询接口。
- 单独替换或撤回一份材料只重跑受影响的批次；材料含义未变时既有复核决定继续有效。
- 重复导入不增加记录；中断后可重跑。

---

## 1. 快速开始（无需安装任何依赖）

嵌入式模式只使用 Python 3.10+ 标准库。

```bash
# 一条命令完成校验：跑全部自动化测试 + 打印夹具走查结果
python3 verify.py
```

正常输出结尾为：

```
Tests: PASS   elapsed: 7.4s
```

（安装依赖的时间不计入；本项目的嵌入式模式与测试均不需要安装任何依赖。）

启动服务：

```bash
python3 run.py serve --database sqlite:///var/sampling.db --port 8080
```

导入项目自带的示例材料（五个文件，两种版式，含翻页续写与瓶签复用）：

```bash
python3 run.py import --database sqlite:///var/sampling.db \
    --fixtures fixtures --voyage KX-26-03
```

命令行查询：

```bash
python3 run.py batches --voyage KX-26-03
python3 run.py query   --batch KX-2603-A
python3 run.py review  --status open
```

---

## 2. 输入格式

导入接口接受一组材料，每份材料是：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 文件名，用于推断类型与显示 |
| `content` | 是* | 材料的完整文本（HTML 或纯文本） |
| `path` | 是* | 与 `content` 二选一，服务端读取该文件 |
| `kind` | 否 | `deck` / `handover` / `lab`；省略时按内容与文件名推断 |
| `slug` | 否 | 材料标识；省略时由文件名生成。同名材料第二次导入即为“替换” |
| `voyage` | 否 | 航次命名空间，默认 `default`。同一航次内批次号唯一 |
| `replace` | 否 | 请求级参数；语义见第 4 节 |

\* `content` 与 `path` 至少提供一个。

### 三类材料

**甲板采样记录（`deck`）**
- 站次标题管辖其后的明细行；有「站次/站位/站号」标题时，行内可没有站次列。
- 列名按同义词识别，列序任意：`瓶号/样品编号/样号/瓶签`、`采样时间/取样时间/时间`、`深度`、`样品类型`、`备注`、`批次` 等。
- 表格跨页续写时常见重印栏头；重印的栏头只用于续接列映射，不产生明细。
- 「（续）」「续页」「continued」等标记同样不产生明细。
- 采样时间可以是 `08:10`（继承标题中的日期）、`2026-03-01 08:10` 或时间区间 `08:00—08:20`。

**样瓶交接单（`handover`）**
- 划定送检批次：每个「送检批次 X」标题（或表格中的批次列）开启一个批次。
- 同批次下每行 = 一个样瓶；给出站次与采样时段（可写在标题、段落或行内）即作为该批次的配对依据。
- 瓶签可跨批次重复使用（例如复采批），因此样瓶身份 = `(批次, 标签, 站次, 时段)`。

**实验室盐度结果（`lab`）**
- 每个行 = 一条结果，含瓶号（可只是瓶签片段，如 `…05-001`、`S07-00…`、`—`）与盐度值。
- 行内没有站次/时段时，从最近的批次标题继承，并标注为「inferred」。
- 若同时存在「样品编号」与「瓶号」两列，优先使用瓶号列；只有一列时该列即身份。

---

## 3. 服务接口

启动后所有接口返回 JSON。`GET /api/docs` 返回下面的接口说明，`GET /` 返回端点列表。

### 导入

```
POST /api/materials
{
  "voyage": "KX-26-03",
  "replace": false,
  "materials": [
    {"name": "deck_log.html", "kind": "deck", "content": "<html>…"},
    {"name": "lab.txt",       "kind": "lab",  "content": "…"}
  ]
}
```

返回每份材料的处理结果：`status`（`created` / `unchanged` / `reverted` / `restored` / `withdrawn`）、抽到的记录数 `records`、受影响的批次 `affected_batches`、`warnings`。

- 相同内容重复提交（包括 `replace: true`）不会新建材料，也不会改变任何批次。
- 文件名相同但内容不同：替换该材料，旧版本标记为 `superseded`，只重跑受影响的批次。

### 替换 / 撤回一份材料

```
POST /api/materials/{slug}/revise      {"content": "……", "kind": "lab"}   # kind 可省略
POST /api/materials/{slug}/withdraw
```

- 替换只重跑消费该材料的批次（如改一条盐度值只会重算该实验室报告覆盖的批次）。
- 撤回实验室报告 → 依赖它的批次转 `held`；其他批次照常。
- 撤回交接单 → 该交接单划定的批次转为 `held`，其余批次照常。

### 查询

```
GET /api/samples?voyage=KX-26-03&batch=KX-2603-A&station=S01&label=S01-001
                &min_salinity=33&max_salinity=35&include_unpublished=true&limit=200&offset=0
```

每条记录包含：

```json
{
  "batch": "KX-2603-A", "station": "S01", "label": "S01-001",
  "sampled_at": "2026-03-01T08:00", "salinity": 33.842,
  "state": "auto", "published": true,
  "confidence": {"overall": 0.94, "extraction": 0.935, "matching": 0.838},
  "provenance": {
    "handover": {"material": "handover", "file": "handover.html", "line": 12,
                 "section": "送检批次 KX-2603-A", "page": 1, "excerpt": "S01-001 | 08:00 | 5 | 完好"},
    "deck":     [{"material": "deck_log", "file": "deck_log.html", "line": 15,
                  "section": "站次 S01 · 2026-03-01", "excerpt": "S01-001 | 08:00 | 5 | 盐度 | 表层"}],
    "lab":      {"material": "lab_results_1", "file": "lab_results_1.txt", "line": 10,
                 "excerpt": "LAB-A-01 | S01-001 | 33.842"}
  }
}
```

默认只返回已发布批次（`published` / `in_review`）中已确定归属的行；`include_unpublished=true` 时也返回候选行。

批量：

```
GET /api/batches?voyage=KX-26-03&status=held
GET /api/batches/{batch_id 或 batch_code}
GET /api/materials?voyage=KX-26-03&history=true
GET /api/materials/{slug}
```

### 复核队列

```
GET  /api/review?status=open&batch=KX-2603-G&voyage=KX-26-03
       # status: open | resolved | stale | dismissed | acknowledged | closed
GET  /api/review/{item_id}          # 附带 source.content 原文，便于对照
POST /api/review/{item_id}/decide
```

复核项类型：

| `kind` | 含义 | 可用 `action` |
| --- | --- | --- |
| `ambiguous_assignment` | 该瓶有多个可行结果 | `confirm`（`value.result` = 选定的结果记录键）、`reject` |
| `batch_hold` | 整批配不齐，暂缓发布 | `acknowledge`、`reject`；若材料确实支持完整配对，可用 `confirm` + `value.pairs` |
| `low_confidence_extraction` | 抽取字段置信度低 | `correct`（`value.fields`）、`confirm`、`reject` |

示例：

```json
{"action": "confirm", "reviewer": "张敏", "note": "按抄录顺序核对",
 "value": {"result": "lab_a0e453cc992ee2c673f5"}}
```

```json
{"action": "correct", "reviewer": "张敏",
 "value": {"fields": {"label_raw": "S05-002"}}}
```

状态取值：`open`（待处理）、`resolved`、`stale`（前提已变化）、`dismissed`（复核人驳回）、`acknowledged`（已知悉暂缓）、`closed`（条件消失，无需再处理）。

---

## 4. 配对与发布规则

1. **样瓶侧**由交接单定义；同一交接单内重复出现的瓶只算一瓶。
2. **结果侧**为实验室报告中的盐度行；一份结果的归属由以下同时成立的证据决定：
   - 瓶签相同，或瓶签片段（≥3 个有效字符）被完整瓶签包含；
   - 站次相同（`S01` 与 `01` 视作兼容但非精确）；
   - 采样时间在瓶/批次的采样时段内（无时间段时按站次与日期约束）。
3. **整批一致性**：把批次视为二分图，求覆盖所有瓶的完美匹配。
   - 不存在完美匹配 → 整批 `held`，不发布任何一行；复核队列生成 `batch_hold`，其中列出每瓶的可行候选。
   - 存在完美匹配但存在歧义 → 批次 `in_review`；**只在所有可行完美匹配中归属都相同的结果才自动发布**，其余进入 `ambiguous_assignment`。
   - 唯一完美匹配 → `published`。
4. **一果一瓶**：一条实验室结果最多属于一个瓶、一个批次；同航次内跨批次解析时会互相排除。
5. 复核人确认配对后，该批次的确认结果以 `confirmed` 状态发布，查询与复核队列同步更新为同一版本。

---

## 5. 复核决定的保留规则

复核决定绑定一个**语义指纹**，由以下内容组成：批次码、样瓶的（标签、站次、时段）、结果的（标签、站次、采样时间、所属批次）、以及全部可行配对边。

- 仅仅某个**盐度数值**被更正：指纹不变 → 决定继续有效，批次保持已发布，只是新版数值生效。
- 标签、站次、时段或**可行配对集合**发生变化：指纹改变 → 旧决定标记为 `invalidated`，批次重新进入复核，复核项重新打开。
- 抽取更正（`low_confidence_extraction`）会在材料被重新导入时**重放**：只要该行仍以相同语义存在，更正继续生效；若该行本身变了，更正自动失效并记录原因。

### 材料身份与航次隔离

材料的记录身份由**航次、材料标识、行语义与出现序号**共同决定（`voyage|slug`）。由此得到：

- 同名同内容的材料导入到两个航次，是两份不同的材料；各自的查询行、出处与复核决定互不影响，撤回或替换其中一份不会改变另一份。
- 同一材料的换版（含等价换版：仅版式、页码或行号变化）沿用原记录身份，因此既有核正与配对决定继续落在当前版本的行上；复核项展示的原文、查询出处（甲板/交接/实验室原文位置）都取**当前航次 + 当前材料版本**的行，不引用已被替换的旧行。
- 核正只会写入当前活动版本的行；若该记录已不在任何活动材料中（例如已换版并被撤回），接口拒绝写入并提示刷新复核队列。
- 重复导入同一材料仍按材料身份撤回与恢复：撤回后再导入相同内容为 `restored`，不新增材料记录。

---

## 6. 生产部署（PostgreSQL）

安装驱动：

```bash
python3 -m pip install "psycopg[binary]"
```

设置连接串并启动：

```bash
export DATABASE_URL="postgresql://sampling:secret@db.internal:5432/sampling"
python3 run.py serve --host 0.0.0.0 --port 8080
```

- 启动时自动建表（幂等）。
- SQL 只使用 `?` 占位符书写，PostgreSQL 模式下自动翻译为 `%s`；不依赖 SQLite 专有函数。
- 所有写操作在事务中执行；导入中途失败会整体回滚，并留下 `runs` 记录（状态 `failed`）。
- 进程启动时会把遗留的 `running` 运行标记为 `interrupted`，因此中断后可直接重跑。

本机没有 PostgreSQL 时，`tests/test_postgres_path.py` 用一个只接受 `%s` 的假驱动完整跑通导入—查询—复核流程，确保生产路径的 SQL 与事务写法可用；真实环境仍建议做一次连库烟测：

```bash
python3 run.py --database "$DATABASE_URL" import --fixtures fixtures --voyage KX-26-03
```

---

## 7. 测试与夹具

```bash
python3 verify.py                        # 全部测试 + 走查（约 7–8 秒，远低于 80 秒）
python3 -m unittest discover -s tests -t tests -v
python3 repro.py                         # 离线复现：同名同内容跨航次导入、换版后核正再撤回
```

`repro.py` 只用自带夹具和临时 SQLite 数据库，逐条打印以下不变量是否成立：两个航次导入同名同内容材料后互不影响、重复导入不新增记录、换版后复核原文与核正落在当前版本、等价换版后核正继续有效、撤回后再导入为 `restored` 而非新增。

夹具（`fixtures/`）覆盖题目要求的各种材料特征，且无需访问外部站点：

| 文件 | 类型 | 特征 |
| --- | --- | --- |
| `deck_log.html` | 甲板记录 | 站次标题管辖明细；`colspan/rowspan`；第 2 页为 S03 续页并重印栏头；第 4 页 S01 复采，瓶签复用 |
| `deck_log_2.txt` | 甲板记录 | 纯文本 + Tab 分隔；另一种列名与列序；第 2 页「（续页）」且重印栏头 |
| `handover.html` | 交接单 | 8 个送检批次，跨页；批 H 复用批 A 的瓶签（同批内瓶号唯一） |
| `lab_results_1.txt` | 实验室 | 4 个批次，含「样品编号 + 瓶号」两列 |
| `lab_results_2.html` | 实验室 | 瓶签片段（`…05-001`、`S07-00…`）与缺失瓶号（`—`），需要结合站次与采样时段；含一个不属于任何交接批次的批次 `KX-2603-I` |

由此产生的预期结果：

| 批次 | 状态 | 说明 |
| --- | --- | --- |
| KX-2603-A/B/C/D/F/H | `published` | 站次、时段与瓶签可唯一配对 |
| KX-2603-E | `held` | 缺一条实验室结果（`E-2` 瓶号未抄），整批暂缓；复核核正后可发布 |
| KX-2603-G | `in_review` | 后两条结果的瓶签都只抄到 `S07-00…`，可互换；只在所有可行配对中一致的前两瓶自动发布，其余待复核 |
| KX-2603-I | — | 只有实验室结果、没有交接单，不构成送检批次，不发布 |

---

## 8. 项目结构

```
app/
  config.py     配置与连接串脱敏
  db.py         统一 DB-API（SQLite 嵌入式 / PostgreSQL 生产），事务与表结构
  document.py   HTML / 文本 → 统一事件流（标题/段落/表格，含 colspan/rowspan）
  extract.py    三类材料的抽取、栏名同义词、翻页续写、页码与置信度
  matching.py   瓶签/站次/时段兼容性、完美匹配、候选集与语义指纹
  service.py    导入、批次解析、版本发布、复核决定、查询
  api.py        stdlib HTTP 服务与 JSON 接口
run.py          命令行入口（serve / import / revise / withdraw / query / review …）
verify.py       一条命令完成验证
repro.py        离线复现跨航次导入与换版—核正—撤回流程
fixtures/       材料夹具
tests/          自动化测试（标准库 unittest）
```
