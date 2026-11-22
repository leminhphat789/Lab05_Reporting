$ErrorActionPreference = 'Stop'

function Test-LocalPort([int]$Port) {
    $client = New-Object Net.Sockets.TcpClient
    try {
        $client.Connect('127.0.0.1', $Port)
        return $true
    } catch [Net.Sockets.SocketException] {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Wait-ForPort($Process, [int]$Port, [string]$LogFile) {
    for ($attempt = 0; $attempt -lt 80; $attempt++) {
        if ($Process.HasExited) { throw "Startup failed on port $Port. See $LogFile" }
        if (Test-LocalPort $Port) { return }
        Start-Sleep -Milliseconds 250
    }
    throw "Timed out starting port $Port. See $LogFile"
}

function Find-MariaDB {
    if ($env:BTH5_MYSQLD) {
        if (!(Test-Path -LiteralPath $env:BTH5_MYSQLD -PathType Leaf)) {
            throw 'BTH5_MYSQLD must contain the full path to mysqld.exe or mariadbd.exe.'
        }
        return (Get-Item -LiteralPath $env:BTH5_MYSQLD).FullName
    }
    foreach ($name in 'mariadbd.exe', 'mysqld.exe') {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) { return $command.Source }
    }
    $candidates = @("$env:SystemDrive\xampp\mysql\bin\mysqld.exe")
    $candidates += @(Get-ChildItem -Path "$env:ProgramFiles\MariaDB*\bin\mariadbd.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
    $uninstall = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
                    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')
    $xampp = Get-ItemProperty -Path $uninstall -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -eq 'XAMPP' -and $_.InstallLocation }
    foreach ($install in $xampp) { $candidates += Join-Path $install.InstallLocation 'mysql\bin\mysqld.exe' }
    # The XAMPP installation used for the original lab was moved after setup.
    $candidates += 'D:\codesigma\WEB xampp\mysql\bin\mysqld.exe'
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    throw 'Install MariaDB/XAMPP and set BTH5_MYSQLD to its server executable, or set DB_HOST and DB_PORT for an existing database server.'
}

function Start-PrivateDatabase {
    $dataDir = Join-Path $PSScriptRoot 'mysql-data'
    $config = Join-Path $dataDir 'my.ini'
    if (Test-LocalPort 3315) {
        $listener = Get-NetTCPConnection -State Listen -LocalPort 3315 | Select-Object -First 1
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        if ($owner.CommandLine -and $owner.CommandLine.IndexOf($config, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return }
        throw 'Port 3315 already belongs to another process. To use an existing MariaDB, set DB_HOST and DB_PORT explicitly.'
    }
    $server = Find-MariaDB
    if (!(Test-Path -LiteralPath $config -PathType Leaf)) {
        if ((Test-Path -LiteralPath $dataDir) -and (Get-ChildItem -LiteralPath $dataDir -Force | Select-Object -First 1)) {
            throw "Existing $dataDir has no my.ini. Check its configuration or select an external database with DB_HOST and DB_PORT."
        }
        $bin = Split-Path -Parent $server
        $initializer = @('mariadb-install-db.exe','mysql_install_db.exe') |
            ForEach-Object { Join-Path $bin $_ } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
        if (!$initializer) { throw 'MariaDB database initializer missing. Install MariaDB/XAMPP or set DB_HOST and DB_PORT for an existing server.' }
        & $initializer "--datadir=$dataDir" '--port=3315'
        if ($LASTEXITCODE -ne 0) { throw 'MariaDB initialization failed.' }
    }
    $log = Join-Path $PSScriptRoot 'mariadb-start.log'
    $arguments = @("--defaults-file=`"$config`"", "--datadir=`"$dataDir`"", '--port=3315', '--bind-address=127.0.0.1', '--console')
    $process = Start-Process -FilePath $server -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardError $log
    Wait-ForPort $process 3315 $log
}

function Start-LabService([int]$Port, [string]$RelativeScript) {
    $scriptPath = (Get-Item -LiteralPath (Join-Path $PSScriptRoot $RelativeScript)).FullName
    if (Test-LocalPort $Port) {
        $listener = Get-NetTCPConnection -State Listen -LocalPort $Port | Select-Object -First 1
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        if ($owner.CommandLine -and $owner.CommandLine.IndexOf($scriptPath, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return }
        throw "Port $Port is occupied by another application. Stop it before starting this copy of BTH5."
    }
    $log = Join-Path $PSScriptRoot "service-$Port.log"
    $process = Start-Process -FilePath $python -ArgumentList "`"$scriptPath`"" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardError $log -PassThru
    Wait-ForPort $process $Port $log
}

$previousPort = $env:DB_PORT
$previousName = $env:DB_NAME
Push-Location -LiteralPath $PSScriptRoot
try {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (!(Test-Path -LiteralPath $python -PathType Leaf)) {
        if (Get-Command py.exe -ErrorAction SilentlyContinue) { & py.exe -3 -m venv .venv }
        elseif (Get-Command python.exe -ErrorAction SilentlyContinue) { & python.exe -m venv .venv }
        else { throw 'Install Python 3 for Windows, then run start.ps1 again.' }
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python virtual environment.' }
    }
    & $python -m pip --disable-pip-version-check install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Python dependency installation failed.' }

    if (!$env:DB_NAME) { $env:DB_NAME = 'bth5_demo_products' }
    if (!$env:DB_HOST -and !$env:DB_PORT) {
        Start-PrivateDatabase
        $env:DB_PORT = '3315'
    }
    & $python upstream/product_service/setup_db.py
    if ($LASTEXITCODE -ne 0) { throw 'Product database setup failed. Check DB_HOST, DB_PORT, DB_NAME, DB_USER and DB_PASSWORD.' }

    Start-LabService 5001 'upstream/auth_service/app.py'
    Start-LabService 5002 'upstream/product_service/app.py'
    Start-LabService 5003 'upstream/order_service/app.py'
    Start-LabService 5004 'app.py'
    Write-Host 'Reporting is ready at http://127.0.0.1:5004/health'
} finally {
    $env:DB_PORT = $previousPort
    $env:DB_NAME = $previousName
    Pop-Location
}
