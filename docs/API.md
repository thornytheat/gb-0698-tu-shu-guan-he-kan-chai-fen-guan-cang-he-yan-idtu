# 接口

`POST /api/run`：{"text":"CSV内容"}。同规则版本与输入摘要只产生一条批次。

`GET/POST /api/1` 至 `/api/7`：独立样例或自定义文件计算。各模块返回 report、quarantine、balance。

`POST /api/review`：id、整数revision、decision取通过或退回。旧版本409；阻断项不能通过。复核追加不可改写快照，保存当时册数、覆盖期次与结果摘要。

`POST /api/amend`：id、整数revision、changes列表、可选note。每条change用 `record_ids` 或 `issn/year/copy` 指向目标，`segments` 给出装订分段（`issues`、`binding`、可选`record_id`）。只重排目标已覆盖的期次：分段多于目标即拆分，少即合并，相等为重述。返回 `status`（applied/replayed）与该批次当前派生结果。

修订号裁决：同号同值重办，不生成版本；同号异值409并隔离，当前事实不变；低于当前已生效修订的晚到修订409，不回退当前事实；更高修订号先整体重放校验、再原子写入。

`GET /api/amend/{id}`：修订留痕与派生状态。`GET /api/compare/{id}`：原批次与当前批次并排指标。

`GET /api/export/{id}`：唯一证据CSV，含运行摘要、原批次清单、并排核对、修订与复核留痕、隔离记录。`GET /health`：存活检查。

错误：结构/数值422，版本/修订冲突409，不存在404，文件过大413。逐行异常进入quarantine。切换失败时页面显示最后一次已签复核的结果。
