# 工作台内部剪映执行模块

用户选择剪映新建、续接或局部修改时读取。剪映是持续适配的正式后端，原生数据无法直接交给达芬奇不影响本接口继续使用；本模块不单独注册Skill，不复制或搬移实现和数据。

## 制作与维护

1. 先读引擎`planning`，再读`editing`、`capabilities`、`plan-format`；CLI/MCP沿用现有统一白名单。选择剪映的新项目也走本接口，不能仅允许旧项目续接。
2. 使用`jianying-engine paths`返回的正式引擎解释器及runtime。按当前操作帮助执行探测、计划校验、新草稿构建和相应交付；已保存的用户工程、原素材及源配置保持原位。返回job_id后持续检查任务与输出，按实际证据报告。
3. 剪映高级功能按capabilities准入。制作遇到未接入能力时说明缺口；用户明确要求继续适配时，读取`maintenance`与`reference/progressive-integration`，沿用原任务清单、能力缺口、资源证据和回归，不另起一套接口或将未完项当历史关闭。
4. 用户切换主软件时另存对应宿主计划，不改写剪映原生参数、工程标识和资源引用。其他软件的成功不替代剪映验收，剪映能力也不直接开放给其他宿主。

## 原路径与配置语义

已安装入口的`backends/jianying/`整合剪映原环境、配置、资源连接与历史资料，其scripts/assets/references/guides仍指向唯一正式引擎源码。Skills顶层没有另一份剪映目录。该安装专属内容不复制进源码Skill分发包，项目素材与已有工程保持原路径。

正式引擎环境支持`python -m jianying_engine`；内部原剪映环境保留原脚本兼容，不将其冒充正式引擎解释器。从`backends/jianying/scripts`调用时，库存素材接口使用同内部目录的config.env；通过正式引擎调用时默认使用LOCALAPPDATA/JianyingEngine/config.env。历史布局识别保留在代码中，但不恢复顶层旧目录。显式JIANYING_STOCK_CONFIG仍可进程级覆盖，其他接口沿用各自既有配置契约，不复制或合并凭据内容。
