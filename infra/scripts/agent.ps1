<#
.SYNOPSIS
    RemoteDev Transparent Agent Launcher for Windows.
.DESCRIPTION
    Launches an interactive prompt session or sends a single prompt to the remote Linux agent.
    Streams real-time thoughts, file access, and tokens while synchronizing Antigravity configs.
.EXAMPLE
    .\infra\scripts\agent.ps1 "Inspect the Flutter project and list errors"
#>
param(
    [Parameter(Position=0, ValueFromRemainingArguments=$true)]
    [string]$Prompt = ""
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$InfraDir = Split-Path -Parent $ScriptDir
$CliScript = Join-Path $InfraDir "bridge\agent_cli.py"

if ($Prompt -ne "") {
    python "$CliScript" "$Prompt"
} else {
    python "$CliScript"
}
