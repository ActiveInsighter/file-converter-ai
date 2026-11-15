# OCR 插图碎片诊断与修复（2026-11-15）

## 已确认的原因

百度 PaddleOCR-VL 的版面检测会分别识别流程图中的树、箭头和标注；旧客户端把每个 `markdown.images` 项直接作为独立插图输出。它没有把这些碎片恢复成原来的图形布局，因此 Markdown 渲染会把横向流程拆成多张纵向图片。

检查最近的 408 真题转换（[43732455386](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43732455386)）：PDF 第 190 页有 19 张候选图片，其中前 17 张属于三行平衡二叉树操作示意图，最后两张是独立的表达式树和 DAG。第 284 页的九张树形子图各有编号。原 PDF 每页均为整页扫描图片，并不是源文件拆成了这些图块。

官方 `layoutMergeBboxesMode` 的 `large` 只在互相包含的检测框中保留大框，不会把相邻、不相交的图块自动拼成一图。`mergeLayoutBlocks` 则服务于跨栏正文的版面合并，不能据其名字认定能修复图形碎片。参考：[参数文档](https://www.paddleocr.ai/main/version3.x/pipeline_usage/PaddleOCR-VL.html)、[上游默认配置](https://github.com/PaddlePaddle/PaddleOCR/blob/main/deploy/paddleocr_vl_docker/pipeline_config_fastdeploy.yaml)。

## 修复行为与边界

在 OCR 草稿交给校对模型之前，识别连续图片引用中空间相邻、纵向重叠的同一图行，从本地原始 PDF 的对应区域重新渲染为 240 DPI PNG。恢复原图中的空隙和连接关系，使用一个 `images/pNNN-merged-N.png` 替换该组碎片的引用；原来的图题和正文保留。

完整图题、正文、表格、未关联的不同图行及距离较远的插图阻断自动合并。很短的图内节点标签和带编号的 h= 子图标签须匹配真实版面坐标，才会随图一起重新裁取。原图坐标不完整、页面比例不匹配或 OCR 使用了几何预处理时保留原裁图。这里选择保守恢复，不把一页的所有图片盲目合成一张；连续编号、在相邻两行排列的同组子图可恢复为包含全部编号的一张组合图。

链接、relay 和分段上传共用同一个结果恢复路径；局部 PDF 的坐标按 OCR 页面宽高缩放回原始 PDF，因此原始页码非 1 和不同上传 DPI 都可处理。已恢复的碎片无需再次从百度下载。

## 验证

- 本地完整测试：281 项通过；Python 编译和 `git diff --check` 通过。
- 新增 8 项测试覆盖图形几何、正文/图题阻断、不同图行、异常坐标、重复引用，以及恢复图片进入 OCR 草稿且不重复下载的完整客户端路径。
- 真实 API 参数对照：[43734833865](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43734833865)。
- 修复后真实 OCR → 模型 → Markdown 验证：[43735093962](https://github.com/ActiveInsighter/file-converter-ai/actions/runs/43735093962)。

实测默认配置与仅改图片类 `layoutMergeBboxesMode={"14":"large"}` 后，原始图块数均为第 190 页 20 张、第 284 页 10 张（各包含一张页脚二维码），因此没有修改线上 OCR 参数。

使用真实返回的 JSON 对原 PDF 重放修复逻辑：第 190 页的前 17 个碎片恢复为 3 张完整流程图，另两张独立图保留；第 284 页的 9 个子图恢复为 1 张包含全部 ①–⑨ 编号和 h 标注的组合图。逐张查看恢复后的 PNG，与原页核对了箭头、插入/删除文字、节点（包括被 OCR 单独识别的“⑥”）及九个子图的完整性。真实坐标和经过删减的 OCR 草稿已加入回归夹具。

首轮端到端运行只恢复了 8 个碎片，暴露了过严的距离限制和图内单字标签阻断问题；该运行仅用于诊断，不能作为最终修复效果的证明。后续真实运行另列。
