# 逐段质量检查

v2.2：按项目 seam_policy 评估。新项目 natural 允许装饰道路渐隐；structural 要求有效连续。内容/visual/geometry/outgoing 的自身检查通过后，调用 preview 合成候选接口，不能仅凭原始边界错位重生成。查看输出与原尺寸 join，再在最终单段报告中加入 incoming=pass、previous_sha256、preview_id，并把 join 的实际路径/哈希加入 evidence_files。通过 preview 不等于通过 review。

geometry 指文件尺寸与处理区域合法；natural 项目不把装饰道路宽度差作为硬性几何错误。保护框、文字、主体和有信息含义的结构不能靠雾化掩盖。入接口验收以实际合成效果为证据；出接口只确认有足够可处理空间，不提前宣称下一段接缝通过。

完整项目沿用 continuous-generation-prompt.md 第九部分。首段也检查；最后一段底部允许收束。按 cli.md 生成审阅 JSON，绑定实际文件 SHA-256、保护框、证据图和 reviewer。检查 content/visual/geometry/outgoing；非首段还检查 incoming 与 previous_sha256。不可读或未检查项记 pending，不填 pass。reject 只隔离候选，保留旧合格版。接缝需要看原尺寸局部；OCR 只能辅助。

把实际底部元素位置、宽度、方向、颜色、纹理、虚线等写入 connection_observations。试验中道路实际宽度曾明显偏离规划示意；begin 会读取上一段审阅记录，把真实观测连同图片传给下一段请求。不得继续只传旧示意数字；也不能用“观察更新”放行本来不可用的接口。
