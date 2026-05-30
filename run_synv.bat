@echo off
chcp 65001 >nul
cd /d "G:\Downloads\cfnb-main"

echo [1/2] 正在运行 CloudflareSpeedTest ...
:: -p 0 避免测速完需要按回车退出；-f 指定输出文件；其他参数按需调整
cfst.exe -dn 50 -tl 150 -p 0 -httping false -url http://cs.otakulin.cc.cd

if %errorlevel% neq 0 (
    echo ❌ CFST 执行失败，退出
    exit /b 1
)

echo [2/2] 正在执行 Git 同步脚本 ...
:: -ExecutionPolicy Bypass 临时绕过执行策略限制
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0git_sync.ps1"

if %errorlevel% equ 0 (
    echo ✅ 全部完成！
) else (
    echo ❌ 同步脚本执行失败
)
pause