# T146 用户授权的 AI 参考标注（2026-10-03）

当前状态：AI 代拟、源材料核对及独立于业务代码的参考核算已制作。用户已授权代做标注；是否以 AI 参考口径替代 T146 原“独立教师”完成要求，等待本轮验收口径选择。作者 Codex，委托人 current_chat_user。未记为用户亲自制作或复核，独立教师标签仍 0。

## 输入、身份与版本

原包 ../v2-draft-20261001 完整保留，原57例不删除、不回填；原扫描件为合成稿栅格化PDF，未声称实拍扫描或真实学生记录。新增4例只补齐明确无问题、真实选择题无歧义及显式参与事实，版本 v2-ai-reference-20261003-1。冻结ID、顺序、分层和文件摘要见 manifest.json。

5个正常导入案例共16题，本轮检查所有原页及原PNG；既有T160的5例用户确认只按 prior_human_review 引用，未扩大为本轮11语义/7图片/统计标签复核。PERF-IMPORT-1复用3题，10/50页逐页读取原文字并核对120题，首尾页视觉检查，共139个导入参考题实例（含工作负载复用，不是139独立题）。题型未明示、可靠边界未测量保留 null；无答案不代算，错序 C/A/B/D 不排序。

## 文件结构

- annotations.json：61例索引；annotation_ref 为同目录文件及JSON Pointer，不复制多份可分叉标签。
- cases.json / cases.csv：固定ID、家族、场景、源引用、标签入口；原57例＋4明确的新对照。
- import_annotations.json：5正常＋3工作负载、题序/原字段/原页/缺失/未知/资产及原T160确认引用。
- semantic_controls.json / semantic_annotations.json / semantic_labels.csv：原8例＋3控制；四项44标签、逐项证据、applicable及可评分掩码。
- image_annotations.json：7例、逐条件像素依据及原资产；低清未知不从高清反填。fixture UUID 不冒充业务 file_id。
- assembly_annotations.json：300候选逐题核算、12组卷及4评分。只证明数学见证与输入条件，未把 intended_status=Approved 当真实批准。
- statistics_controls.json / statistics_annotations.json / statistics_reference.csv：原2统计＋新STARTED及1反馈；两场分母、分布、逐题、知识点重叠与来源，CSV156行。
- scenario_annotations.json / scenario_inputs.json：故障、UI、交付的固定前置与预期。actual_outcome、真实Provider失败、截图、EXE/备份产物仍为空，留给对应执行任务。
- environment.json：本次实际机器/已安装准备工具；未运行模型或数据库，不拿安装版本当运行配置。
- validation.json：输入检查回执；由 benchmark/t146/validate_inputs.py 重复生成，不是业务验收结果。

## 判定与分母

true 为该类有问题，false 为无该问题，null 为无法判定；只有 applicable=true 且非null进入语义可评集合，不适用不算TN。四类均有可评正负例。新选择题SEM-CLEAR-OPTIONS是无歧义负例，原短答不充当选项负例。指标均为AI参考对照；独立教师准确率分母仍0。未知/故障/未执行不算正确或TN，不因看到系统输出改变旧标签。

组卷和统计用Decimal及独立整数分/Fraction复算，未调用待测业务服务。A/B最终样本各2、均分2.00/4.00；逐题得分率1/2、1/3；一次函数2/5、表达与计算1/3。知识点失分合计A10.00/B20.00包含重叠，整卷仅A6.00/B12.00，不能重复累计。原参与事实不足保留null；新STARTED明确参与5。EMPTY按固定非Final选择，分母0、均分/率/失分为null。

尾差+0.01/-0.01的分配只是AI建议；confirmed_points/confirmation仍null，不能据此发布。ASM-EDIT的to_index=0为夹具数组坐标，执行适配必须明确转成合同API位置1，不改变API1基语义。“未审核”反馈题ID实际上不在池中，只标missing；练习第1题没有真实批准，不能宣称当前可推荐。无学生原答案，不编造失分过程或诊断解释。

## 后续执行

保留T142质量3轮、导入/组卷3冷＋5暖及预热、1/10/50页、1/25/100题、300候选；200题仅旧请求边界。运行环境/有效模型/Prompt/配置、完整真实故障、页面截图、EXE与备份由T168/T179/T184/T189/T190/T191另开批次采集；本任务不做质量阈值确认，不改业务门禁或性能预算。

在仓库根使用现有Python运行 `benchmark/t146/validate_inputs.py --receipt benchmark/corpus/t146-ai-authorized-20261003/validation.json`。包摘要验证只用于确认本输入，不能代替语义正确、教师身份、授权、业务批准或性能证据。
