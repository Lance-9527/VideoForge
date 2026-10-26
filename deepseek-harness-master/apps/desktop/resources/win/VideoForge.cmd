@echo off
setlocal
rem ==========================================================================
rem  VideoForge launcher (portable / troubleshooting)
rem
rem  WHY: Chromium resolves its disk-cache directory BEFORE our JS runs, so
rem  app.setPath("userData") is too late. Double-clicking the exe gives
rem  "Unable to move the cache: Access is denied" and the process exits at once.
rem  Passing --user-data-dir fixes it completely (process stays alive).
rem
rem  Installed builds do not need this file: NSIS writes the same argument
rem  into the desktop / start-menu shortcuts.
rem ==========================================================================

set "VF_ROOT=%LOCALAPPDATA%\VideoForge"
set "VF_UDD=%VF_ROOT%\electron"

rem Create the chain up-front: Chromium does not create parents on first run.
if not exist "%VF_UDD%" mkdir "%VF_UDD%" >nul 2>&1

rem The DSH running on this machine exports ELECTRON_RUN_AS_NODE, which makes
rem the Electron binary behave like plain node (no window, exits immediately).
set "ELECTRON_RUN_AS_NODE="

rem Detach so the console closes and the app keeps running.
start "" "%~dp0VideoForge.exe" "--user-data-dir=%VF_UDD%" %*

endlocal
