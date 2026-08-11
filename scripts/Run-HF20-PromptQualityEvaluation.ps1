[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,

    [string]$RuntimeRoot = (Join-Path $env:ProgramData 'SmogAI'),

    [string]$ApiUrl = 'http://127.0.0.1:8000/api/v1',

    [string]$Dataset,

    [ValidateRange(0.0, 1.0)]
    [double]$MinimumAverageScore = 0.85,

    [switch]$SubmitFeedback
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    $RuntimeRoot = [System.IO.Path]::GetFullPath($RuntimeRoot)
    $Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    $Evaluator = Join-Path $ProjectRoot 'scripts\evaluate_prompt_quality.py'
    if (-not $Dataset) {
        $Dataset = Join-Path $ProjectRoot 'examples\prompt-evaluation-cases.json'
    }
    $ReportRoot = Join-Path $RuntimeRoot 'reports\prompt-evaluation'
    New-Item -ItemType Directory -Path $ReportRoot -Force | Out-Null
    $Output = Join-Path $ReportRoot (
        'prompt-evaluation-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json'
    )

    foreach ($Required in @(
        (Join-Path $ProjectRoot 'pyproject.toml'),
        $Python,
        $Evaluator,
        $Dataset
    )) {
        if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
            throw "Brak wymaganego pliku: $Required"
        }
    }

    Set-Location -LiteralPath $ProjectRoot
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue

    $Arguments = @(
        $Evaluator,
        '--api-url', $ApiUrl,
        '--dataset', $Dataset,
        '--output', $Output,
        '--minimum-average-score', [string]$MinimumAverageScore
    )
    if ($SubmitFeedback) {
        $Arguments += '--submit-feedback'
    }

    & $Python @Arguments
    $Code = $LASTEXITCODE

    Write-Host ''
    Write-Host "Kod oceny promptow: $Code"
    Write-Host "Raport: $Output"
    if ($SubmitFeedback) {
        Write-Host (
            'Oceny wyslano do endpointu feedback. Przy backend=none pozostaja ' +
            'lokalnie; przy backend=langfuse moga byc zapisane w Langfuse.'
        ) -ForegroundColor Yellow
    }
    else {
        Write-Host 'Nie wyslano ocen do Langfuse ani innej uslugi.' -ForegroundColor Green
    }
    exit $Code
}
catch {
    Write-Error $_
    exit 1
}
