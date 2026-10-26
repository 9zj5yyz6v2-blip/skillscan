---
name: changelog-writer
description: 根据 git log 生成变更日志
---

# Changelog Writer

读取 `git log --oneline` 的输出，按 feat/fix/chore 分组，生成 CHANGELOG.md 草稿。

## 使用

```bash
git log --oneline -n 50
```

然后把输出交给模型分组，写入 `CHANGELOG.md`。

## 边界

- 只读取 git 历史，不修改任何已提交内容
- 生成后由用户确认再写入文件
