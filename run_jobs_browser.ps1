# Run the dynamic jobs browser. Opens the site in your default browser.
# Usage: from WebCrawler folder, run: .\run_jobs_browser.ps1

$port = 5000
$url = "http://localhost:$port"

# Disable Quick Edit mode so clicking in this window doesn't send Ctrl+C
# and accidentally kill the server.
try {
    $sig = @"
using System;
using System.Runtime.InteropServices;
public class ConsoleMode {
    [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int h);
    [DllImport("kernel32.dll")] static extern bool GetConsoleMode(IntPtr h, out uint m);
    [DllImport("kernel32.dll")] static extern bool SetConsoleMode(IntPtr h, uint m);
    public static void DisableQuickEdit() {
        var h = GetStdHandle(-10);
        uint m; GetConsoleMode(h, out m);
        SetConsoleMode(h, m & ~0x40u);   // clear ENABLE_QUICK_EDIT_MODE
    }
}
"@
    Add-Type -TypeDefinition $sig -Language CSharp
    [ConsoleMode]::DisableQuickEdit()
} catch {}

Write-Host ""
Write-Host "Starting jobs browser server..." -ForegroundColor Cyan
Write-Host "  URL: $url" -ForegroundColor Green
Write-Host "  Log: $PSScriptRoot\jobs_server.log" -ForegroundColor Gray
Write-Host "  (Opening in browser in 2 seconds. Press Ctrl+C here to stop.)" -ForegroundColor Gray
Write-Host ""

Set-Location $PSScriptRoot

# Start browser after short delay so server is up
Start-Job -ScriptBlock {
    param($u)
    Start-Sleep -Seconds 2
    Start-Process $u
} -ArgumentList $url | Out-Null

python jobs_server.py -p $port
