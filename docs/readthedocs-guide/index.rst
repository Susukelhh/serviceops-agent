====================================================================================================
ServiceOps Agent：从代码到面试
====================================================================================================

这不是一份“技术名词大全”，而是一份项目说明书。当前版本已经完成独立 Qdrant 与完整混合召回升级。
目标是让你能回答三件事：

1. 用户说一句话后，系统从哪里进、经过哪里、最后把什么保存下来？
2. 每一种技术是为了解决什么具体问题，为什么不直接让大模型全权处理？
3. 面试官追问时，哪些是项目已经证明的，哪些只是未来可以补充的？

.. important:: 当前可演示版本

   Docker Compose 会启动 Nginx、两只 Agent、PostgreSQL 和独立 Qdrant。
   FAQ 不再是“向量先选、BM25 只复查”，而是 Qdrant 和 BM25 分别检索完整知识库，再用 RRF 合并两张榜。
   教学调试页会展示每条证据的向量名次、BM25 名次和最终 RRF 分数。

.. warning:: 最新质量结论

   第31步四路对照证明RRF改善当前小语料的证据排序，但一次性锁定集因两条新域外表达误召回而
   ``Gate FAIL``。项目随后把Bad Case转入第32步意图专项：千问v2冻结候选通过32条开发题与16条
   一次性锁定题并晋级；RRF的1.50权重仍未晋级，项目也不宣称Recall已经提升。第34步冻结了
   “端到端有据回答成功率”契约，零费用风险对照为14/25（56%）；首次千问候选为10/25（40%），
   虽将知识缺口正确拒答提高到5/6，但可回答题完整通过只有5/19，因此质量门失败并原样冻结。第35步
   私有诊断后用v1.1开发口径零费用重放同一批原回答为20/25（80%），但仍有一条无依据回答红线，
   Gate继续FAIL；该结果是已揭晓开发重评分，不替换首次40%。

.. tip::

   第一次阅读先看“项目全貌”和“一次请求的一生”；第二次重点阅读“RAG”和“Docker”；面试前再看“项目边界”和“面试题库”。

.. toctree::
   :maxdepth: 2
   :caption: 第一部分：先看懂全貌

   01_project_overview
   02_module_map
   03_request_lifecycle
   04_three_business_paths

.. toctree::
   :maxdepth: 2
   :caption: 第二部分：逐层拆开代码

   modules/01_api_and_security
   modules/02_langgraph_brain
   modules/03_rag_pipeline
   modules/04_tools_and_approval
   modules/05_persistence_and_recovery
   modules/06_reliability_and_observability
   modules/07_frontend_and_debugger
   modules/08_evaluation_and_experiments
   modules/09_container_and_delivery

.. toctree::
   :maxdepth: 2
   :caption: 第三部分：从技术回到问题

   05_technology_choices
   06_limits_and_next_steps
   09_hybrid_experiment_result
   10_intent_classification_result
   11_public_demo
   12_end_to_end_blind_test
   13_grounded_answer_success_rate
   14_grounded_answer_v2_development
   15_grounded_answer_v2_sealed
   16_evaluator_audit_and_scope_v2
   17_grounded_answer_v3_sealed
   18_semantic_judge_calibration
   19_hybrid_grounded_evaluator
   07_code_reading_route
   08_glossary

.. toctree::
   :maxdepth: 2
   :caption: 第四部分：面试准备

   interview/01_hr_questions
   interview/02_technical_questions
   interview/03_pressure_questions
   interview/04_answer_framework

文档依据
====================================================================================================

本教程依据当前 ``D:\\serviceops-agent`` 源码、实验报告、架构记录，以及
``ai-agent-interview-guide-main`` 中的基础概念、核心框架、RAG、工具调用、工程化、STAR 和面试问答集整理。
参考资料只用于提出问题；回答以本项目真实实现为准。
