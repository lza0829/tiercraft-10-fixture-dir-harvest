# 05-tiercraft（筑层）

筑层 — 数仓分层建设台：贴源入仓 → 明细清洗标准化 → 汇总落表 → 层间行数/键对账。

**不是** 多源 Feed 归并，**不是** 作业依赖编排，**不是** CDC 变更流按序落库，**不是** 质检隔离签发。
**也不是** 报表看板 / CRUD 后台 / 电商订单前台 / OA 工单。

## 启动

```bash
docker compose up --build
```

本地开发：

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=..\data
uvicorn app.main:app --reload --port 10500

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5500 |
| 接口 | http://localhost:10500 |

## 主链

登记主题域 → 采集位点按整文件增量装载本地夹具目录切片 → 贴源层 → 明细层清洗标准化 → 汇总层聚合 → 层间对账报告与各层列表数字同源

## 采集位点

- 采集源限定本地夹具目录 `fixtures/<主题域>/`，目录内是按业务日命名的切片文件（如 `2026-11-11.jsonl`）。
- 位点持久化为「已成功完整摄入的文件集合」（`harvest_watermarks`），每次采集只处理位点之外的新切片。
- 位点推进按整文件事务：文件全部行写入贴源、并在同一事务内登记位点后才算成功；任一行失败则整文件回滚、位点不收录，任务记失败并保留失败文件名与原因，后续切片不再继续。
- 任务记录（`harvest_jobs`）的位点前/后、尝试/成功/失败文件清单与新增行数，均由同一次落库反查贴源与位点集合得出，不做内存计数。
- 入口：界面「采集位点」页可查看位点、待采切片与采集任务记录，并触发采集；接口为 `GET /api/harvest/watermark`、`GET /api/harvest/jobs`、`POST /api/harvest/run`。

## 界面约定

页面文案全部中文；分层称「贴源层 / 明细层 / 汇总层」，界面不出现英文缩写站名。
