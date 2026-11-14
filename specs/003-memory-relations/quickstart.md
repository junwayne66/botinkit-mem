# Validation quickstart

1. 使用隔离 Store 创建共同来源、多个父级派生、两次修订及独立节点。
2. 通过本机 TestClient 验证图谱、来源事件、cloud私密过滤和遗忘后的结果。
3. 执行 `uv run pytest tests/test_graph.py tests/test_recompute.py -q`，数据指定独立 `--basetemp`。
4. Python编译、`node --check src/bmem/static/app.js`和`git diff --check`。
5. 在隔离浏览器样本验证首页连线、完整图筛选、节点修订定位、连线及来源详情。
6. 第一切片交独立review，批准后本地提交部署。第二切片补齐时间策略与兼容数据验收。
