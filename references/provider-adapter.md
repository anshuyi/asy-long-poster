# 当前宿主适配：内置 imagegen

这是给运行 Skill 的代理使用的薄适配约定，不是 Python SDK，也不要求环境变量或 API key。

1. 先发现当前实际工具。此实现面向 `image_gen.imagegen`（编排环境中可显示为 `tools.image_gen__imagegen`）：新图只传 `prompt`；需要承接上一段时传 `prompt` 与 `referenced_image_paths`。宿主工具文档优先于本参考中的名称。
2. 用 begin 登记请求。读取返回的 references 文件，调用 view_image 查看上一段真实图；再把实际绝对路径交给生图工具。不要仅把文件名写进提示词。
3. v2.2 返回的 prompt 是从原方法原则与当前项目编译出的单段请求。五段原文仍完整保存，但不再把整段流程与过时数值原样塞入单次生图。存在旧 continuity 与实际观测时，在 begin 传入 execution_spec（scene/composition/constraints），明确有效构图。每次只生成一段。工具没有显式尺寸参数时，在 prompt 写入尺寸并测量结果；不得伪称设置了不存在的字段。
4. 只有参考图没有本地路径时，按宿主文档用最小必要的 `num_last_images_to_include`；不能同时传两个参考机制。记录实际来源和哈希，无法核实引用时停止生成。
5. 返回生成结果后按工具要求显示预览；得到实际文件后 register，提交实际传入参考图的哈希列表。工具没有返回 call_id 可为空，不虚构。生成文件可能位于宿主默认目录，register 将原图复制进本项目。
6. 若结果是 data URL / 平台附件，使用宿主支持的保存方式先获得真实文件；路径不明就查实际返回值，不猜路径。未取得可读图片不能登记成功。
7. 工具失败但明确没有产物时 request_failed；结果未知保留 pending 并 block，不自动重新付费。内置工具失败不静默改用外部 API。

能力记录支持 reference/edit/user_supplied/text_only/unavailable/synthetic_test。前两者要有真实图片输入；synthetic_test 仅用于单元测试，永远不算真实生图验收。扩图、遮罩、像素锁定能力在未核验时标 unknown；本 Skill 的 mask 拼接是本地合成能力，不能冒充生成工具的 mask 能力。

工具调用由宿主执行；begin/register 只做可审计的交接。submitted_reference_hashes 是代理的调用记录，无法替代工具执行证据。最终测试报告同时注明模型自检和真实调用记录。
