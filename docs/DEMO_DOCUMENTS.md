# Demo Enterprise Documents

这是一组用于没有真实业务数据时继续开发和评估的模拟企业知识库文档。

当前 `demo_documents/` 包含 10 篇短文档。

加载到完整开发版知识库：

```bash
python scripts/load_demo_documents.py --load
```

如果已经加载过并希望替换同名 demo 文档：

```bash
python scripts/load_demo_documents.py --load --force
```

构建轻量版本地索引：

```bash
python scripts/lite_index.py --source-dir demo_documents
```

轻量版 CLI 查询：

```bash
python scripts/lite_query.py "远程办公需要提前多久申请？" --no-llm
```

轻量版 Web：

```bash
python scripts/run_lite.py
```

打开：

```text
http://127.0.0.1:8010/
```

对应评估集：

```bash
evals/demo_enterprise_eval_dataset.json
```
