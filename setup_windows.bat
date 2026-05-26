@echo off
chcp 65001 >nul
echo ============================================================
echo  oppo-webserver Windows 首次安装
echo ============================================================

:: ── 1. 检查 Python ──────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 python 命令。
    echo 请先安装 Python 3.9+ 并勾选 "Add Python to PATH"
    echo https://www.python.org/downloads/
    pause
    exit /b 1
)
echo [OK] Python 已找到：
python --version

:: ── 2. 安装 pip 依赖 ─────────────────────────────────────────
echo.
echo [1/3] 安装项目依赖（requirements.txt）...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] pip install 失败，请检查网络或代理设置
    pause
    exit /b 1
)
echo [OK] requirements.txt 安装完成

:: ── 3. 安装 hotwords_service 依赖 ───────────────────────────
echo.
echo [2/3] 安装 hotwords_service 依赖（hanlp / jieba / pypinyin）...
python -m pip install -r hotwords_service\requirements.txt
if errorlevel 1 (
    echo [警告] hotwords_service 依赖安装有问题，hanlp 可能需要手动安装
)
echo [OK] hotwords_service 依赖安装完成

:: ── 4. 安装 Playwright Chromium ─────────────────────────────
echo.
echo [3/3] 安装 Playwright Chromium 浏览器（约 200MB）...
python -m playwright install chromium
if errorlevel 1 (
    echo [错误] Playwright 浏览器安装失败
    echo 请手动运行：python -m playwright install chromium
    pause
    exit /b 1
)
echo [OK] Playwright Chromium 安装完成

:: ── 5. 初始化配置文件 ─────────────────────────────────────────
echo.
if not exist .env.dev (
    if exist .env.example (
        copy .env.example .env.dev
        echo [OK] 已从 .env.example 创建 .env.dev，请按需修改路径配置
    ) else (
        echo [警告] 未找到 .env.example，请手动创建 .env.dev
    )
) else (
    echo [OK] .env.dev 已存在，跳过
)

:: ── 6. 创建输出目录 ──────────────────────────────────────────
if not exist output mkdir output
echo [OK] output\ 目录已就绪

echo.
echo ============================================================
echo  安装完成！运行方式：
echo    run_windows.bat          （拉取全部数据 + 生成 rank）
echo    python fetch_and_rank.py --help  （查看所有参数）
echo ============================================================
pause
