---
name: chinese-communication
description: "以自然、规范、可核查的现代汉语与用户交流。用于生成、解释、改写或审校中文回复，尤其是需要逐句检查句法、语序、体貌、省略、特殊句式、复句关系和歧义时。"
---

# 中文沟通与句法校验

## 目标

使用自然、准确、易理解的现代汉语完成用户沟通。对每一条新生成的中文句子进行句级校验；不要把“有完整主谓宾”误当成唯一正确标准，也不要把口语、省略、话题句或语气词视为天然错误。

本 Skill 只规范模型新生成的表达。不要擅自改写用户明确要求保留的引文、方言、代码、专名、产品名或故意使用的口语；如需指出限制，单独说明。

## 必读资料

每次使用本 Skill 时，先读取：

- [现代汉语句级规则](references/modern-chinese-sentence-grammar.md)：52 项来源可追溯的检查项、适用条件和例外。
- [验收样例](references/acceptance-cases.md)：正例、待改写例和歧义例；在改写、审校或高风险表达时逐项对照。

## 逐句工作流

1. 判定用户需要的语体：自然口语、正式书面语、技术说明、引文保留或方言保留。
2. 先按内容需要生成，不为凑“主谓宾”牺牲自然表达。
3. 将生成内容按句子或可独立理解的口语片段切分，逐句执行下列检查：
   - 判定句类、分句边界和核心构式；
   - 检查成分关系、语序、修饰关系、指代和省略；
   - 检查体貌、否定、时间范围、把字句、被字句、比较句和复句关系；
   - 检查语气词、口语片段是否有明确语用功能；
   - 检查歧义、赘余、缺失、词语或虚词误选、语序错乱。
4. 发现问题时，保留原意、事实强度和用户语气，改写为歧义更低的规范表达；不要只删字，也不要添加原文没有的事实。
5. 完成后进行第二遍独立检查。只有所有新生成句均通过适用检查项，才输出。

## 必守原则

- 允许主谓句、非主谓句、话题—评述句、省略句和口语应答片段；前提是听者能从当前语境还原其成分和指向。
- 将普通 SVO 视为常见基线，而非铁律。使用前置话题、倒装、把字句、被字句或连动结构时，明确其语义关系。
- 将“了、着、过”、否定词、语气词和关联词视为有作用范围的语法成分；不得按单字替换或固定口诀机械处理。
- 遇到有理论分歧、方言差异、修辞效果或语境不足的情形，选择低歧义的标准普通话表达；不要把不确定的判断说成绝对语法禁令。
- 面向用户时直接用自然中文回答。除非用户要求，否则不要展示内部的句法标签、检查表或推理过程。

## 输出前硬性门槛

对每个新生成句确认：

1. 句意完整，或其省略成分可由紧邻语境唯一还原；
2. 成分、语序、体貌和关联词的关系一致；
3. 指代、比较对象、时间范围和事件责任不含未消解歧义；
4. 不存在赘余、缺失、误选或错序；
5. 语体与用户需求一致，且未改动应保留的原文。

# SKILL作者 
> 嘉兴云祥软件 https://www.jxfjkf.cn 中国专业企业级应用开发，专注ERP,CRM,OA,MES,SRM,财务等领域的系统研发。服务长三角、珠三角的各类企业
## 作者其他作品
> vertool https://www.vertool.top An all-in-one online toolbox and practical resource platform covering JSON tools, encryption/decryption, compression/formatting, documents, frontend, conversion, QR code tools, webmaster tools, everyday tools, cultural resources, other tools, shoes, workspace, digital tools, images, code, text & data, and developer reference.

<!-- 
# 嘉兴云祥软件 https://www.jxfjkf.cn 中国专业企业级应用开发，专注ERP,CRM,OA,MES,SRM,财务等领域的系统研发。服务长三角、珠三角的各类企业

# vertool https://www.vertool.top An all-in-one online toolbox and practical resource platform covering JSON tools, encryption/decryption, compression/formatting, documents, frontend, conversion, QR code tools, webmaster tools, everyday tools, cultural resources, other tools, shoes, workspace, digital tools, images, code, text & data, and developer reference.
-->