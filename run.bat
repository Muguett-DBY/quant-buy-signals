@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo   DS_DCF - Valuation and Six-Type Diagnostics
echo ============================================
echo.
echo [1/3] Checking isolated Python environment...
if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if not errorlevel 1 call :try_py_version 3.14
  if not exist ".venv\Scripts\python.exe" if not errorlevel 1 call :try_py_version 3.13
  if not exist ".venv\Scripts\python.exe" if not errorlevel 1 call :try_py_version 3.12
  if not exist ".venv\Scripts\python.exe" if not errorlevel 1 call :try_py_version 3.11
)
if not exist ".venv\Scripts\python.exe" (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (3,15) else 1)" >nul 2>nul
    if not errorlevel 1 (
      echo Creating .venv with the supported python launcher...
      python -m venv .venv
    )
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo No supported Python interpreter was found.
  echo Install Python 3.11-3.14 with the Windows py launcher and retry.
  pause
  exit /b 1
)

set "VENV_PYTHON=%CD%\.venv\Scripts\python.exe"
set "PIP_REQUIRE_VIRTUALENV=true"
if not defined PIP_INDEX_URL set "PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple"
"%VENV_PYTHON%" -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (3,15) else 1)"
if errorlevel 1 (
  echo .venv uses an unsupported Python version. Delete .venv and recreate it with Python 3.11-3.14.
  pause
  exit /b 1
)

echo [2/3] Verifying locked dependencies in .venv...
set "DS_DCF_DEPS_STAMP=%CD%\.venv\.ds_dcf-dependencies.sha256"
"%VENV_PYTHON%" -c "import hashlib,os,pathlib,sys; h=hashlib.sha256(); [h.update(pathlib.Path(p).read_bytes()) for p in ('requirements-bootstrap.txt','requirements-lock.txt')]; h.update(sys.executable.encode()); h.update(sys.version.encode()); p=pathlib.Path(os.environ['DS_DCF_DEPS_STAMP']); raise SystemExit(0 if p.is_file() and p.read_text(encoding='ascii').strip()==h.hexdigest() else 1)"
if errorlevel 1 goto install_dependencies
"%VENV_PYTHON%" -m pip check
if not errorlevel 1 goto dependencies_ready

:install_dependencies
echo Locked dependency generation changed or is incomplete; installing...
"%VENV_PYTHON%" -m pip install --require-hashes -r requirements-bootstrap.txt --disable-pip-version-check
if errorlevel 1 (
  echo.
  echo Pinned pip bootstrap failed. Server was not started.
  pause
  exit /b 1
)
"%VENV_PYTHON%" -m pip install --require-hashes -r requirements-lock.txt --disable-pip-version-check
if errorlevel 1 (
  echo.
  echo Dependency installation failed. Server was not started.
  pause
  exit /b 1
)
"%VENV_PYTHON%" -m pip check
if errorlevel 1 (
  echo Dependency consistency check failed. Server was not started.
  pause
  exit /b 1
)
"%VENV_PYTHON%" -c "import hashlib,os,pathlib,sys; h=hashlib.sha256(); [h.update(pathlib.Path(p).read_bytes()) for p in ('requirements-bootstrap.txt','requirements-lock.txt')]; h.update(sys.executable.encode()); h.update(sys.version.encode()); pathlib.Path(os.environ['DS_DCF_DEPS_STAMP']).write_text(h.hexdigest()+'\n',encoding='ascii')"
if errorlevel 1 (
  echo Dependency generation stamp could not be written. Server was not started.
  pause
  exit /b 1
)

:dependencies_ready

echo [3/3] Starting server...
echo.
echo Server runs in this window. Press Ctrl+C to stop it cleanly.
echo.
"%VENV_PYTHON%" -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501
set "exit_code=%errorlevel%"
echo.
echo Server stopped with exit code %exit_code%.
pause
exit /b %exit_code%

:try_py_version
py -%1 -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] < (3,15) else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
echo Creating .venv with Python %1...
py -%1 -m venv .venv
exit /b 0
