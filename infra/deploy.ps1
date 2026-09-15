<#
.SYNOPSIS
  Build + deploy the LeadRadar web app to Azure Container Apps.

.DESCRIPTION
  Builds the Docker image remotely in Azure Container Registry (no local Docker
  needed), polls the build until it finishes, pushes secrets and environment
  variables from a local .env file, then rolls the Container App to the new
  image. Sign-in (Easy Auth) is configured separately in the Azure Portal.

  Prerequisites: Azure CLI logged in (`az login`), an existing ACR with the
  admin user enabled, and an existing Container App.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File infra/deploy.ps1 `
      -Subscription my-sub -ResourceGroup rg-leadradar -Registry myregistry -AppName leadradar

.EXAMPLE
  # Re-deploy the current :latest image with updated settings only
  powershell -ExecutionPolicy Bypass -File infra/deploy.ps1 -Subscription my-sub `
      -ResourceGroup rg-leadradar -Registry myregistry -AppName leadradar -SkipBuild
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Subscription,
    [Parameter(Mandatory)] [string]$ResourceGroup,
    [Parameter(Mandatory)] [string]$Registry,
    [Parameter(Mandatory)] [string]$AppName,
    [string]$ImageRepo = "leadradar",
    [string]$ImageTag,
    [string]$EnvFile   = ".env",
    [switch]$SkipBuild
)

$ErrorActionPreference = "Continue"
$ProgressPreference    = "SilentlyContinue"
$env:AZURE_CORE_ONLY_SHOW_ERRORS = "true"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
chcp 65001 > $null

$RepoRoot    = Split-Path -Parent $PSScriptRoot
$LoginServer = "$Registry.azurecr.io"
Set-Location $RepoRoot

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# Load .env
if (-not (Test-Path $EnvFile)) { throw "Env file '$EnvFile' not found." }
$envMap = @{}
Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $eq = $line.IndexOf("=")
    if ($eq -lt 1) { return }
    $k = $line.Substring(0, $eq).Trim()
    $v = $line.Substring($eq + 1).Trim()
    if ($v.StartsWith('"') -and $v.EndsWith('"')) { $v = $v.Substring(1, $v.Length - 2) }
    $envMap[$k] = $v
}
function GetEnv([string]$k, [string]$d = "") {
    if ($envMap.ContainsKey($k) -and $envMap[$k] -ne "") { return $envMap[$k] }
    return $d
}
function GetEnvRequired([string]$k) {
    $v = GetEnv $k
    if ($v -eq "") { throw "Required variable '$k' missing or empty in $EnvFile." }
    return $v
}

# Set subscription
Write-Step "Subscription"
az account set --subscription $Subscription 2>$null
az account show --query "{name:name, user:user.name}" -o table

# Build image
if (-not $SkipBuild) {
    if (-not $ImageTag) { $ImageTag = (Get-Date -Format "yyyyMMdd-HHmm") }
    Write-Step "Building $ImageRepo`:$ImageTag in ACR $Registry"

    $buildJson = az acr build --registry $Registry `
        --image "${ImageRepo}:${ImageTag}" `
        --image "${ImageRepo}:latest" `
        --no-logs --output json $RepoRoot 2>$null

    $runId = ($buildJson | ConvertFrom-Json).runId
    if (-not $runId) { throw "az acr build did not return a run ID. Check ACR permissions." }

    Write-Host "    Build queued: $runId - polling status..."
    $buildStatus = "Queued"
    while ($buildStatus -eq "Running" -or $buildStatus -eq "Queued") {
        Start-Sleep -Seconds 15
        $runs = az acr task list-runs --registry $Registry --top 20 -o json 2>$null | ConvertFrom-Json
        $run = $runs | Where-Object { $_.runId -eq $runId } | Select-Object -First 1
        $buildStatus = $run.status
        Write-Host "    Status: $buildStatus"
    }

    if ($buildStatus -ne "Succeeded") { throw "ACR build $runId ended with status: $buildStatus" }
    Write-Host "    Build $runId succeeded."
} else {
    $ImageTag = "latest"
}

$ImageRef = "$LoginServer/${ImageRepo}:${ImageTag}"

# Secrets and env vars
Write-Step "Preparing secrets and env vars"

# Always-required secrets
$secretArgs = [System.Collections.Generic.List[string]]@(
    "azure-api-key=$(GetEnvRequired 'AZURE_API_KEY')",
    "azure-sql-server=$(GetEnvRequired 'AZURE_SQL_SERVER')",
    "azure-sql-database=$(GetEnvRequired 'AZURE_SQL_DATABASE')",
    "azure-sql-username=$(GetEnvRequired 'AZURE_SQL_USERNAME')",
    "azure-sql-password=$(GetEnvRequired 'AZURE_SQL_PASSWORD')"
)
$envVarArgs = [System.Collections.Generic.List[string]]@(
    "AZURE_API_KEY=secretref:azure-api-key",
    "AZURE_ENDPOINT=$(GetEnvRequired 'AZURE_ENDPOINT')",
    "DEPLOYMENT_SCORING=$(GetEnv 'DEPLOYMENT_SCORING' 'gpt-4.1-mini')",
    "DEPLOYMENT_WRITING=$(GetEnv 'DEPLOYMENT_WRITING' 'gpt-4.1-mini')",
    "LLM_PROVIDER=$(GetEnv 'LLM_PROVIDER' 'azure')",
    "BUSINESS_PROFILE=$(GetEnv 'BUSINESS_PROFILE' 'profiles/acme_equipment_rental.toml')",
    "MAIL_BACKEND=$(GetEnv 'MAIL_BACKEND' 'graph')",
    "EMAIL_SENDER=$(GetEnvRequired 'EMAIL_SENDER')",
    "EMAIL_RECIPIENTS=$(GetEnv 'EMAIL_RECIPIENTS' '')",
    "EMAIL_CC=$(GetEnv 'EMAIL_CC' '')",
    "APP_BASE_URL=$(GetEnv 'APP_BASE_URL' '')",
    "AZURE_SQL_SERVER=secretref:azure-sql-server",
    "AZURE_SQL_DATABASE=secretref:azure-sql-database",
    "AZURE_SQL_USERNAME=secretref:azure-sql-username",
    "AZURE_SQL_PASSWORD=secretref:azure-sql-password"
)

# Optional secrets: only pushed if present in .env, so values managed in the
# portal are never wiped by a deploy from a machine that doesn't have them.
$optionalSecrets = [ordered]@{
    "AAD_CLIENT_SECRET"   = "aad-client-secret"
    "GRAPH_CLIENT_SECRET" = "graph-client-secret"
    "SESSION_SECRET_KEY"  = "session-secret-key"
    "CLAY_API_KEY"        = "clay-api-key"
    "SERPER_API_KEY"      = "serper-api-key"
}
foreach ($k in $optionalSecrets.Keys) {
    $v = GetEnv $k
    $secretName = $optionalSecrets[$k]
    if ($v -ne "") {
        $secretArgs.Add("${secretName}=$v")
        $envVarArgs.Add("${k}=secretref:${secretName}")
        Write-Host "    ${secretName}: from .env"
    } else {
        Write-Host "    ${secretName}: not in .env, keeping existing"
    }
}

# Optional plain settings
foreach ($k in @("AAD_TENANT_ID", "AAD_CLIENT_ID", "AAD_REDIRECT_URI", "GRAPH_TENANT_ID", "GRAPH_CLIENT_ID",
                 "CLAY_DOMAIN_ROUTINE_ID", "CLAY_ENRICH_COMPANY_ROUTINE_ID", "CLAY_EMAIL_ROUTINE_ID", "CLAY_PHONE_ROUTINE_ID")) {
    $v = GetEnv $k
    if ($v -ne "") { $envVarArgs.Add("${k}=${v}") }
}

# ACR admin credentials for Container App image pull
Write-Step "Reading ACR admin credentials"
$adminUser = az acr credential show --name $Registry --query username -o tsv 2>$null
$adminPass = az acr credential show --name $Registry --query "passwords[0].value" -o tsv 2>$null
if (-not $adminUser -or -not $adminPass) { throw "Could not read ACR admin credentials for '$Registry'." }

# Update Container App
Write-Step "Updating Container App $AppName"
az containerapp registry set --name $AppName --resource-group $ResourceGroup `
    --server $LoginServer --username $adminUser --password $adminPass --output none 2>&1
az containerapp secret set --name $AppName --resource-group $ResourceGroup `
    --secrets $secretArgs --output none 2>&1
az containerapp update --name $AppName --resource-group $ResourceGroup `
    --image $ImageRef --set-env-vars $envVarArgs `
    --min-replicas 1 --max-replicas 1 --output none 2>&1
if ($LASTEXITCODE -ne 0) { throw "containerapp update failed (exit $LASTEXITCODE)" }

# Wait for revision
Write-Step "Waiting for revision to settle..."
$state = "InProgress"
while ($state -eq "InProgress") {
    Start-Sleep -Seconds 5
    $state = az containerapp show --name $AppName --resource-group $ResourceGroup `
        --query "properties.provisioningState" -o tsv 2>$null
    Write-Host "    State: $state"
}

$fqdn = az containerapp show --name $AppName --resource-group $ResourceGroup `
    --query "properties.configuration.ingress.fqdn" -o tsv 2>$null

Write-Step "Done."
Write-Host "  State : $state"
Write-Host "  URL   : https://$fqdn"
