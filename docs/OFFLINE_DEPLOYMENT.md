# 离线部署指南

系统提供两条可验收的离线交付路径。构建阶段在可联网、与目标环境兼容的机器上完成；内网安装阶段不访问 PyPI、APT、npm、CDN、浏览器下载站或模型厂商。

## 路径 A：源码与依赖离线包

### Ubuntu：外网构建

构建机必须与目标机使用相同 Ubuntu 大版本和 CPU 架构：

```bash
bash deploy/ubuntu/build_offline_bundle.sh
```

默认生成：

```text
dist/proposal-agent-ubuntu-offline/
dist/proposal-agent-ubuntu-offline.tar.gz
```

包内包含源码、Prompt Pack、Mermaid JavaScript、Python wheelhouse、Python/Chromium/Noto CJK/LibreOffice/Poppler 及其 deb 依赖、systemd 文件、备份恢复脚本和 SHA-256 manifest。

### Ubuntu：内网一键安装

```bash
tar -xzf proposal-agent-ubuntu-offline.tar.gz
cd proposal-agent-ubuntu-offline
sudo bash install.sh
```

安装位置：

```text
/opt/proposal-agent
/etc/proposal-agent/proposal-agent.env
/var/lib/proposal-agent
/var/log/proposal-agent
```

管理与维护：

```bash
sudo systemctl status proposal-agent
sudo systemctl restart proposal-agent
sudo journalctl -u proposal-agent -f
sudo bash backup.sh
sudo bash restore.sh /var/backups/proposal-agent/proposal-agent-YYYYMMDD_HHMMSS.tar.gz
KEEP_DATA=true sudo bash uninstall.sh
```

安装脚本执行 manifest 校验、deb 离线安装、虚拟环境创建、`pip --no-index` 安装、服务注册与健康检查。它不会运行 `apt update` 或访问网络。

### Windows：外网构建

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\deploy\windows\build_offline_bundle.ps1
```

默认生成：

```text
dist\proposal-agent-windows-offline\
dist\proposal-agent-windows-offline.zip
```

包内包含源码、wheelhouse、官方 Python 3.12 安装器、Playwright Chromium、Mermaid JavaScript、安装/启动/停止/备份/恢复/卸载脚本和 SHA-256 manifest。构建机可以通过 `-PythonInstaller` 指定已下载并经过组织审核的 Python 安装包。

### Windows：内网一键安装

以管理员 PowerShell 执行：

```powershell
Expand-Archive .\proposal-agent-windows-offline.zip -DestinationPath .\proposal-agent-windows-offline
cd .\proposal-agent-windows-offline
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

脚本先用 PowerShell 原生 SHA-256 校验包；若系统没有 Python 3.12，则静默安装包内 Python；随后使用 `pip --no-index` 安装 wheelhouse、复制包内 Chromium、生成配置、注册系统启动计划任务并执行健康检查。

维护命令：

```powershell
.\backup.ps1
.\restore.ps1 -BackupZip C:\ProgramData\ProposalAgentBackups\proposal-agent-*.zip
.\uninstall.ps1 -KeepData
```

## 路径 B：Docker 镜像离线导入

应用镜像已包含 Python 依赖、Chromium、Mermaid JavaScript、中文字体、LibreOffice 和 Poppler。内网只需具备 Docker Engine / Docker Desktop 与 Compose 插件。

### 外网构建纯离线应用包

```bash
bash deploy/docker/build_image_bundle.sh
```

PowerShell：

```powershell
.\deploy\docker\build_image_bundle.ps1 -Mode offline
```

### 外网构建混合部署包

混合包额外包含 SearXNG 镜像：

```bash
bash deploy/docker/build_image_bundle.sh dist/proposal-agent-docker-hybrid hybrid
```

```powershell
.\deploy\docker\build_image_bundle.ps1 -OutputDir dist\proposal-agent-docker-hybrid -Mode hybrid
```

### 内网加载与启动

Linux：

```bash
tar -xzf proposal-agent-docker-offline.tar.gz
cd proposal-agent-docker-offline
bash load_and_run.sh offline
```

Windows：

```powershell
Expand-Archive .\proposal-agent-docker-offline.zip -DestinationPath .\proposal-agent-docker-offline
cd .\proposal-agent-docker-offline
.\load_and_run.ps1 -Mode offline
```

首次执行会生成 `proposal-agent.env` 并停止，管理员配置离线模型地址和模型名后再次运行。`load_and_run` 会验证 manifest、执行 `docker load`、启动 Compose 并检查 `/api/health`。

## 离线模型服务

应用镜像和依赖包不包含模型权重。内网应另行部署 OpenAI-compatible 模型服务：

```env
MODEL_RUNTIME_MODE=LIVE
OFFLINE_LLM_ENABLED=true
OFFLINE_LLM_BASE_URL=http://model-server:8000/v1
OFFLINE_LLM_API_KEY=...
OFFLINE_GENERAL_MODEL=...
OFFLINE_CRITIC_MODEL=...
```

必需接口：

```text
POST <OFFLINE_LLM_BASE_URL>/chat/completions
```

模型应支持 `messages` 和 JSON 文本输出。网关优先请求严格 `json_schema`；端点返回 400、404 或 422 时，自动退回 `json_object`，结果仍须通过项目 Schema 校验。

## 验收

```bash
python scripts/check_config.py --render-mermaid --probe
pytest -q
curl --fail http://127.0.0.1:8080/api/health
```

配置检查不会输出 API Key。生产环境应限制环境文件权限，并纳入组织级密钥管理、备份和集中审计。
