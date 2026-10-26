# VideoForge 外壳

本包负责 VideoForge 品牌、项目列表、工作台面板和 VideoForge 模型设置。

模型设置页读取当前 Python 后端的文本、视频、图像、配音与型号清单。用户可以保留自定义型号和已有中转站，逐家配置配音型号，并设置输出、缓存、FFmpeg 和白膜公网通道路径。

保存时只提交改动字段。密钥留空保留现值；服务商密钥和型号字典采用合并更新。清空模型或接口地址表示使用后端默认值，文件系统路径留空则保留原值。AI 对话模型仍在 Harness 的独立模型页设置。

运行针对性设置测试：`pnpm exec vitest run packages/client/ui-vf-brand/tests/settings-model.client.spec.ts`。
