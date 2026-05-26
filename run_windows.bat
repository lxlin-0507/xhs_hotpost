@echo off
chcp 65001 >nul
setlocal

:: ── 切换到脚本所在目录（双击时工作目录可能不对）──────────────
cd /d "%~dp0"

:: ── 可选：在这里修改默认参数 ──────────────────────────────────
::   --sources weibo douyin_hotlist xhs_hot xhs_hotpost douyin
::   --segmenter hanlp   （或 api / both）
::   --out-dir D:\my_rank_output
set EXTRA_ARGS=

:: ── 检查 Python ──────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 python，请先运行 setup_windows.bat 完成安装
    pause
    exit /b 1
)

:: ── 启动 ─────────────────────────────────────────────────────
echo ============================================================
echo  fetch_and_rank  %date% %time%
echo ============================================================
python fetch_and_rank.py %EXTRA_ARGS% %*
set EXIT_CODE=%errorlevel%

echo.
if %EXIT_CODE% equ 0 (
    echo [完成] 所有任务运行成功
) else (
    echo [警告] 部分任务失败，请查看上方日志
)

:: 保持窗口（直接双击时能看到输出），如果从 cmd/PowerShell 调用则不卡住
echo.
echo 按任意键退出...
pause >nul
endlocal
exit /b %EXIT_CODE%
