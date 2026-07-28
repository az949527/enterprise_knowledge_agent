# Python 运行环境

本项目只支持 Python 3.11。

## Windows 完整后端

安装：

```powershell
.\scripts\install.ps1
```

该命令使用 Python 3.11 创建 `.venv`，并安装 `requirements.txt`。

启动：

```powershell
.\scripts\run_web.ps1
```

## Windows 桌面应用

安装：

```powershell
.\scripts\install_desktop.ps1
```

启动：

```powershell
.\scripts\run_desktop.ps1
```

桌面环境位于 `.venv-desktop`，同样固定为 Python 3.11。

## 运行脚本和测试

完整后端、FAISS 和数据库相关命令使用：

```powershell
.\.venv\Scripts\python.exe <command>
```

桌面、轻量索引和 P0 回归使用：

```powershell
.\.venv-desktop\Scripts\python.exe <command>
```

不要直接使用系统 `python`。当前机器的系统命令可能仍指向 Anaconda Python 3.8，但该解释器不再受本项目支持。

## 版本约束

- `.python-version` 声明版本为 `3.11`。
- `app/__init__.py` 在导入应用代码时执行版本检查。
- Windows 安装、运行和构建脚本拒绝非 Python 3.11 环境。
- GitHub Desktop 构建任务使用 `actions/setup-python` 安装 Python 3.11。
