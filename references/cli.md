# v2.2 默认流程与新增命令

新配置顶层 seam_policy 为 natural（默认）或 structural。旧项目缺字段保持 legacy。用下列操作显式切换并复审：

```text
python scripts/project_state.py --root RUN_ROOT policy --request policy.json
python scripts/project_state.py --root RUN_ROOT preview --request preview.json
python scripts/project_state.py --root RUN_ROOT reconsider --request reconsider.json
```

- policy.json：`{"policy":"natural","reason":"用户已批准装饰道路自然渐隐"}`。操作会使原审阅待复查，须从首段开始。
- begin.json 可增加 execution_spec：`{"scene":"当前插画内容","composition":"当前有效布局及留白","constraints":"需要保留的约束"}`。旧 continuity 与观测同时存在时必须提供；不重复失效数值。改变内容仍走 replan。
- preview.json：`{"segment_id":"s02","version":1,"self_review_path":"records/self-s02.json","plan_path":"records/pair-s01-s02.json"}`。自身报告须 content/visual/geometry/outgoing 全部通过，包含 protected_boxes、真实 evidence_files 及 reviewer。
- 两段预览方案与正式方案字段相同，但 schema_version=1.1，只含相邻两段和一个 seam，输出必须在 previews/。返回 preview_id、output 和 join。保护框来自真实审阅，不由方案自行缩小。
- 看过 join 后，在单段最终报告中加入 preview_id、previous_sha256、checks.incoming=pass，且将 join 的路径和哈希加入 evidence_files，再调用 review。
- reconsider.json：`{"segment_id":"s03","version":3,"reason":"按用户批准的新策略复用旧图做实际融合复审"}`。仅允许新策略复审此前 content 通过、incoming 失败且没有其他明确硬性失败的候选；保留旧拒绝及生图次数。
- 整图 plan 使用 schema_version=1.1，沿用审阅过的每个 seam 和对应切口；输出在 outputs/。修改参数必须重新 preview/review。

雾化 seam 字段示例（实际使用必须测量，不能照搬数值）：

```json
{
  "upper": "s01", "lower": "s02", "overlap_px": 200,
  "blend": "haze", "feather_px": 200,
  "connection_type": "replaceable_background",
  "reason": "已检查为可渐隐装饰与干净纸张",
  "haze_strength": 0.65, "background_rgb": [249, 238, 218],
  "background_reviewed": true,
  "background_samples": {
    "upper": [[40, 1350, 140, 1420]],
    "lower": [[40, 20, 140, 90]]
  }
}
```

可选 haze_mask_path/haze_mask_sha256 指向 W×overlap 的真实 L 遮罩。采样和保护范围由脚本预检，效果仍需看图。elapsed_to_register_seconds 包含等待和登记前检查，不能当作纯模型耗时。

下方为基础命令与 legacy 数据合同，新工作流优先采用本节。

---

# 本地命令与数据合同

依赖 Python 3.10+、requirements.txt 所列 Pillow；本次实际环境版本见运行报告。以下命令从 Skill 根目录运行，RUN_ROOT 替换为本次可写运行目录。初始化后源码和项目数据分开。不要把模板空值当作完成证据。

```text
python scripts/project_state.py --root RUN_ROOT init --request config.json
python scripts/project_state.py --root RUN_ROOT status
python scripts/project_state.py --root RUN_ROOT begin --request begin.json
python scripts/project_state.py --root RUN_ROOT register --request register.json
python scripts/project_state.py --root RUN_ROOT review --request review-request.json
python scripts/project_state.py --root RUN_ROOT recheck --request recheck-request.json
python scripts/project_state.py --root RUN_ROOT replan --request replan-request.json
python scripts/project_state.py --root RUN_ROOT plan --request plan-request.json
python scripts/stitch_images.py --root RUN_ROOT
python scripts/validate_output.py --root RUN_ROOT
python scripts/project_state.py --root RUN_ROOT complete --request final-request.json
python scripts/check_package.py
python -m unittest discover -s tests -v
```

所有写状态命令可加 `--revision N`，不匹配就拒绝。JSON 参数文件从指定路径读取，其中项目产物路径为 RUN_ROOT 相对路径，source_path 仅用于用户已授权的只读输入。异常返回非零退出码。没有网络调用、自动安装或自动发布。

## 配置

config.json 必填：width、user_goal、poster_plan、visual_baseline、provider_capabilities、segment_plan。段 ID 连续为 s01、s02……；每段有锁定 text 和 generation_notes。limits 可覆盖 per_segment、total_calls、seam_attempts、max_pixels；money_budget 设置后 begin 必须提交单次上限 reserved_cost，不支持在价格未知时宣称预算可控。见 templates/project-state.json 的完整初始配置。

## 生成交接

begin.json：`{"segment_id":"s01"}`。修复时可附 repair_notes，保留内容锁定条件；更改用户文案应通过 replan 更新配置后重新检查。begin 返回 attempt_id、完整 prompt、references 和上一段依赖。

定向编辑当前失败图时，另传 repair_version，begin 会把该版实际图片追加到 references 并标记 edit_target。工具收到的顺序必须与记录一致：上一段参考在前，当前编辑目标在后；先查看两张图。修复完成后 submitted_reference_hashes 包含所有实际提交的参考图哈希，不能仅登记上一段。

工具允许多个参考时，可传 connection_crop_box，例如 `[0,1300,1024,1536]`（必须按实图选择）。begin 从上一段当前归一图生成真实裁片，记录 source_box/source_sha256，并作为 connection_crop 插入 references。顺序是上一段整图、可选裁片、可选编辑目标；prompt 的 reference_roles 明确各图身份。代理先查看所有实际图，再按顺序传入工具；不要把裁片的局部高度误当最终画布高度。

register.json 必填 segment_id、attempt_id、source_path；非首段真实生成还需 submitted_reference_hashes，严格等于 begin 返回的参考哈希数组。可附真实 tool_call_id。register 保存原图和归一图，并返回尺寸、哈希和 version。

request_failed 参数：segment_id、attempt_id、definite=true、reason。只有确认失败才释放 pending；结果未知用 block：`{"reason":"具体原因及恢复线索"}`，原请求仍保留。

## 检查记录

review-request.json：`{"segment_id":"s01","version":1,"review_path":"records/qc-s01-v1.json"}`；拒绝候选加 accept=false。review_path 指向的文件采用 templates/qc-result.json：subject_sha256、reviewer（model/human/automated）、notes、checks、protected_boxes、evidence_files。每个证据项有 path、sha256。原图可以作为证据，局部截图更方便复核。

checks 必须明确 content/visual/geometry/outgoing 为 pass；非首段还需 incoming=pass 及 previous_sha256 等于当前上一段。首末段例外仍要显式填通过理由。protected_boxes 坐标取归一后的图，不存在保护内容时填 [] 并解释原因，不能漏字段。

recheck-request.json：segment_id、review_path。报告需要 subject_sha256、previous_sha256、incoming=pass、reviewer、notes 和证据。保留旧生成依赖，新增复查关系。

replan-request.json：必填 reason，可含 segment_updates（段 ID 到 text/generation_notes 改动对象的映射）、visual_baseline、poster_plan、user_goal 和 append_segments。例：`{"reason":"用户修订数字","segment_updates":{"s03":{"text":"18"}}}`。保存旧规划、标记受影响段并使旧合成失效；新段不增加额度。文案/基准改变须重新完整 review，不能仅 recheck 入接口就放行。当前版本支持原位修订和尾部追加；减少段数、重排段 ID 或改变尺寸须新建项目并明确复用记录，避免坐标静默迁移。

## 拼接和完成

plan-request.json：`{"plan_path":"records/stitch-v1.json"}`。详细格式见 execution-contract.md 与 templates/stitch-plan.json；源图片字段直接取当前 accepted 记录，不能自行估计哈希。输出必须为 outputs/ 下的新 PNG。保护框来自实际审阅记录，不允许在拼接计划里删掉以绕过检查。

脚本生成整图预览和逐缝原尺寸预览。final-request.json：`{"review_path":"records/final-review-v1.json"}`。对应报告需要最终图 subject_sha256、当前 plan_sha256、content/visual/seams/goal 全部 pass 和证据文件。

## 恢复和限制

status 显示未解决请求、链路是否有效和最后阻断原因。每次状态保存有 revision 快照。发现 writer lock 时先核实进程/作业是否仍活跃；脚本不自动删除他人的锁。文件有残留时先核对对应请求和哈希，不删除原始输入。

review/recheck 依赖真实内容与视觉观察，是明确标记的模型或人工判断；脚本无法证明其语义正确。安全检查拒绝已知不合法输入，不等于经过独立安全审计。
