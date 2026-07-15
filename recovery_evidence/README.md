# Recovery Evidence

该目录只保留恢复证据的目录规范和说明。实际 G0 ZIP、验证 JSON、日志和数据库快照由 `.github/workflows/g0.yml` 在运行时生成，并作为 GitHub Actions Artifact 上传。

推荐路径：

```text
recovery_evidence/
└── g0/
    └── <commit>/
        ├── G0_VALIDATION.json
        ├── G0_RECOVERY_VERIFY.json
        └── g0-recovery-<commit>.zip
```

大型恢复包和运行时数据库不提交到 Git。
