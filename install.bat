@echo off
rem ============================================================
rem  wechatauto-channels one-click installer (Windows)
rem
rem  Double-click from a release zip or a git clone. It:
rem    1. finds a usable Python (Hermes venv / Octop runtime /
rem       own venv / py launcher / python on PATH)
rem    2. pip-installs wechatauto-channels[send-ocr]
rem    3. writes repo_root into ~/.wechatauto/manager.json
rem    4. registers the manager in Startup (bridge watchdog lives
rem       inside the manager)
rem    5. creates a desktop shortcut
rem    6. launches the manager GUI
rem
rem  Per-host integration (Hermes plugin / Octop channel /
rem  OpenClaw plugin) is then one button inside the manager GUI.
rem ============================================================
setlocal EnableExtensions
set "ROOT=%~dp0"
set "WCA_HOME=%USERPROFILE%\.wechatauto"
set "VENV=%WCA_HOME%\venv"
set "HERMES_PY=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
set "OCTOP_PY=%USERPROFILE%\.octop\portable\runtime\python.exe"

echo [wechatauto] installing from %ROOT%

set "TARGET="
if exist "%HERMES_PY%" goto use_hermes
if exist "%VENV%\Scripts\python.exe" goto use_venv
goto make_venv

:use_hermes
rem Hermes plugin imports wechatauto_channels inside the Hermes venv,
rem so the package MUST land there.
set "TARGET=%HERMES_PY%"
echo [wechatauto] using Hermes venv: %TARGET%
goto install

:use_venv
set "TARGET=%VENV%\Scripts\python.exe"
echo [wechatauto] using existing venv: %TARGET%
goto install

:make_venv
set "SEED="
if exist "%OCTOP_PY%" set "SEED=%OCTOP_PY%"
rem winsdk (dep of wechatauto-replica) ships wheels only for cp39-cp312;
rem on 3.13+ pip falls back to an MSVC source build that dies on
rem non-ASCII user profile paths -> always prefer a <=3.12 interpreter.
if not defined SEED (
  for %%V in (3.12 3.11 3.10) do (
    if not defined SEED (
      py -%%V --version >nul 2>nul
      if not errorlevel 1 set "SEED=py -%%V"
    )
  )
)
if not defined SEED (
  where python >nul 2>nul
  if not errorlevel 1 set "SEED=python"
)
if not defined SEED (
  echo [wechatauto] ERROR: no Python found. Install Octop, Hermes,
  echo   or Python 3.12 first, then re-run install.bat
  exit /b 1
)
echo [wechatauto] creating venv %VENV% from: %SEED%
call %SEED% -m venv "%VENV%"
if errorlevel 1 (
  echo [wechatauto] ERROR: venv creation failed
  exit /b 1
)
set "TARGET=%VENV%\Scripts\python.exe"

:install
rem winsdk has no cp313+ wheels; refuse a >3.12 target early instead of
rem letting pip wander into a doomed source build.
"%TARGET%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] <= (3, 12) else 1)"
if errorlevel 1 (
  echo [wechatauto] ERROR: %TARGET% is Python 3.13+, but winsdk
  echo   ships wheels only for Python 3.9-3.12. Install Python 3.12
  echo   (or Hermes/Octop, which bundle a compatible runtime), then re-run.
  exit /b 1
)
echo [wechatauto] installing package into: %TARGET%
"%TARGET%" -m pip --version >nul 2>nul
if errorlevel 1 "%TARGET%" -m ensurepip --upgrade >nul 2>nul
"%TARGET%" -m pip install --upgrade "%ROOT%python[send-ocr]" --only-binary winsdk
if errorlevel 1 (
  echo [wechatauto] ERROR: pip install failed
  exit /b 1
)

rem -- record repo_root so the manager's inject buttons work --
"%TARGET%" -c "import json,pathlib,os; p=pathlib.Path(os.environ['USERPROFILE'])/'.wechatauto'/'manager.json'; d={}; d.update(json.loads(p.read_text('utf-8')) if p.exists() else {}); d['repo_root']=r'%ROOT%'.rstrip('\\'); p.parent.mkdir(parents=True,exist_ok=True); p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')"
if errorlevel 1 echo [wechatauto] WARN: could not write manager.json

rem -- autostart: manager (+its watchdog) on login --
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
for %%I in ("%TARGET%") do set "PYW=%%~dpIpythonw.exe"
> "%STARTUP%\wechatauto-manager.bat" echo @echo off
>>"%STARTUP%\wechatauto-manager.bat" echo start "" "%PYW%" -m wechatauto_channels.manager
echo [wechatauto] autostart registered: %STARTUP%\wechatauto-manager.bat

rem -- desktop shortcut: keep the manager discoverable after installation --
rem Note: one-click install must leave a discoverable entry point; see the onboarding Agent Note.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ws=New-Object -ComObject WScript.Shell; $name='WeChatAuto '+[char]0x7BA1+[char]0x7406+[char]0x53F0+'.lnk'; $lnk=$ws.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'),$name)); $lnk.TargetPath='%PYW%'; $lnk.Arguments='-m wechatauto_channels.manager --respawn'; $lnk.WorkingDirectory='%USERPROFILE%'; $lnk.Description='WeChatAuto channel manager'; $lnk.IconLocation='$env:SystemRoot\System32\shell32.dll,44'; $lnk.Save()"
if errorlevel 1 (
  echo [wechatauto] WARN: could not create desktop shortcut
) else (
  echo [wechatauto] desktop shortcut created
)

rem -- launch the manager GUI --
start "" "%PYW%" -m wechatauto_channels.manager

echo.
echo [wechatauto] done. In the manager window:
echo   - WeChat tab-free: bridge auto-starts via watchdog
echo   - Hermes tab  -> one-click onboard (installs the plugin)
echo   - Octop tab   -> one-click onboard (injects channel + restarts)
echo   - OpenClaw tab-> one-click onboard (needs openclaw CLI)
exit /b 0
