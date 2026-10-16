# ASY·连续长海报

把文章、知识内容或已有图片制作成一张完整的纵向长海报。先规划整张画布，再逐段生成、检查真实接缝，最后裁切、融合和导出。

技能标识：`asy-long-poster`。包版本：`0.2.0`；工作流规范：`2.2`。

## 能做什么

- 全画布内容、风格和阅读顺序规划。
- 顺序生成分段，以上一段的真实图片作为参考。
- 自然过渡（natural）、结构连续（structural）与旧项目兼容（legacy）。
- 接缝预览、羽化、遮罩、背景雾化和最终拼接。
- 保存版本、生成次数、质量检查与文件哈希，支持局部修复和恢复项目。

## 运行条件

- Python 3.10 或更新版本。
- `requirements.txt` 中指定的 Pillow。
- 制作新图时，宿主 AI 助手需要真实图片输入、生图和看图能力。Python 脚本本身不提供生图服务。
- 仅使用已有图片时，可以执行本地检查和拼接。

## 安装

将本仓库内容放入宿主支持的技能目录下的 `asy-long-poster` 文件夹，确保入口为 `asy-long-poster/SKILL.md`。具体技能目录以宿主配置为准。

在技能根目录安装 Python 依赖：

```sh
python -m pip install -r requirements.txt
```

## 开始使用

向支持该技能的 AI 助手提供文章或素材，例如：

> 使用 $asy-long-poster，把这篇文章做成一张三段连续长海报，暖白底、深蓝和橘色插画。先规划完整画布，逐段检查接缝，最后展示完整长图。

生成作品和项目状态请保存在技能目录之外的独立可写目录。完整流程见 [SKILL.md](SKILL.md)，命令和输入合同见 [references/cli.md](references/cli.md)，更多例子见 [examples/usage.md](examples/usage.md)。

## 验证

```sh
python scripts/check_package.py
python -m unittest discover -s tests -v
```

自动化测试使用合成图片，验证状态管理、路径边界和拼接机制；测试通过不代表真实生图质量已经验证。实际作品仍需要看图检查文字、主体和接缝。

## 来源与许可

作者安十一已确认本包全部内容为其原创，并授权公开发布。本项目采用 [MIT License](LICENSE)，允许使用、修改和分发，需保留许可证和版权声明。

五段原提示词的来源及完整性记录见 [references/source-manifest.md](references/source-manifest.md) 和 [prompt-manifest.json](prompt-manifest.json)。提示词采用 LF 换行以保持校验一致。
