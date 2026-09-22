$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$pythonLocal = Join-Path $PSScriptRoot '.tools\python\python.exe'
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    if (Test-Path $pythonLocal) {
        & $pythonLocal -m venv .venv
    } else {
        python -m venv .venv
    }
    if ($LASTEXITCODE -ne 0) { throw 'Instale Python con Tcl/Tk y vuelva a ejecutar iniciar.ps1.' }
}
& '.\.venv\Scripts\python.exe' -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'No se pudo instalar pySerial.' }
& '.\.venv\Scripts\python.exe' app.py
