param(
    [string]$ProjectId = $env:GOOGLE_CLOUD_PROJECT,
    [string]$ServiceName = "dieter",
    [string]$Region = "us-central1",
    [string]$DataBucket = $env:DIETER_GCS_BUCKET,
    [string]$DataPrefix = $env:DIETER_GCS_PREFIX,
    [string]$OpenAiSecretName = "dieter-openai-api-key",
    [string]$RegistrationCode = $env:DIETER_REGISTRATION_CODE,
    [int]$MinInstances = -1
)

$ErrorActionPreference = "Stop"

$gcloud = "gcloud"
$localGcloud = Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
if (Test-Path $localGcloud) {
    $gcloud = $localGcloud
} else {
    $gcloudCommand = Get-Command gcloud -ErrorAction SilentlyContinue
    if ($gcloudCommand) {
        $gcloud = $gcloudCommand.Source
    } else {
        throw "gcloud is not installed or is not on PATH. Install Google Cloud CLI, then run: gcloud init"
    }
}

if (-not $ProjectId) {
    throw "Set GOOGLE_CLOUD_PROJECT or pass -ProjectId your-gcp-project-id."
}
if (-not $DataBucket) {
    $DataBucket = "$ProjectId-dieter-data"
}
if (-not $DataPrefix) {
    $DataPrefix = $ServiceName
}

function Get-DotEnvValue {
    param([string]$Name)
    if (-not (Test-Path ".env")) {
        return ""
    }
    $line = Get-Content ".env" | Where-Object { $_ -match "^\s*$([regex]::Escape($Name))\s*=" } | Select-Object -First 1
    if (-not $line) {
        return ""
    }
    return ($line -replace "^\s*$([regex]::Escape($Name))\s*=\s*", "").Trim().Trim('"').Trim("'")
}

& $gcloud config set project $ProjectId

& $gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com storage.googleapis.com secretmanager.googleapis.com

$bucketUri = "gs://$DataBucket"
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $gcloud storage buckets describe $bucketUri *> $null
$bucketDescribeExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($bucketDescribeExitCode -ne 0) {
    & $gcloud storage buckets create $bucketUri --location $Region --uniform-bucket-level-access
}

$projectNumber = (& $gcloud projects describe $ProjectId --format "value(projectNumber)").Trim()
$runtimeServiceAccount = "$projectNumber-compute@developer.gserviceaccount.com"
& $gcloud storage buckets add-iam-policy-binding $bucketUri `
    --member "serviceAccount:$runtimeServiceAccount" `
    --role "roles/storage.objectAdmin" *> $null

$openAiApiKey = $env:OPENAI_API_KEY
if (-not $openAiApiKey) {
    $openAiApiKey = Get-DotEnvValue "OPENAI_API_KEY"
}
$llmProvider = $env:LLM_PROVIDER
if (-not $llmProvider) {
    $llmProvider = Get-DotEnvValue "LLM_PROVIDER"
}
if (-not $llmProvider) {
    $llmProvider = "openai"
}
$openAiModel = $env:OPENAI_MODEL
if (-not $openAiModel) {
    $openAiModel = Get-DotEnvValue "OPENAI_MODEL"
}
if (-not $openAiModel) {
    $openAiModel = "gpt-4o-mini"
}
$openAiReviewModel = $env:OPENAI_REVIEW_MODEL
if (-not $openAiReviewModel) {
    $openAiReviewModel = Get-DotEnvValue "OPENAI_REVIEW_MODEL"
}
$openAiPlannerModel = $env:OPENAI_PLANNER_MODEL
if (-not $openAiPlannerModel) {
    $openAiPlannerModel = Get-DotEnvValue "OPENAI_PLANNER_MODEL"
}
if (-not $openAiPlannerModel) {
    $openAiPlannerModel = $openAiReviewModel
}
$recipeOcrModel = $env:RECIPE_OCR_MODEL
if (-not $recipeOcrModel) {
    $recipeOcrModel = Get-DotEnvValue "RECIPE_OCR_MODEL"
}
$recipeCleanupModel = $env:RECIPE_CLEANUP_MODEL
if (-not $recipeCleanupModel) {
    $recipeCleanupModel = Get-DotEnvValue "RECIPE_CLEANUP_MODEL"
}
if (-not $RegistrationCode) {
    $RegistrationCode = Get-DotEnvValue "DIETER_REGISTRATION_CODE"
}
if ($MinInstances -lt 0) {
    if ($env:DIETER_MIN_INSTANCES) {
        $MinInstances = [int]$env:DIETER_MIN_INSTANCES
    } else {
        $MinInstances = 0
    }
}
if ($MinInstances -lt 0) {
    throw "MinInstances must be 0 or greater."
}

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $gcloud secrets describe $OpenAiSecretName *> $null
$secretDescribeExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference

if ($openAiApiKey) {
    if ($secretDescribeExitCode -ne 0) {
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $gcloud secrets create $OpenAiSecretName --replication-policy automatic *> $null
        $secretCreateExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorActionPreference
        if ($secretCreateExitCode -ne 0) {
            throw "Failed to create Secret Manager secret '$OpenAiSecretName'."
        }
    }
    $secretFile = New-TemporaryFile
    try {
        Set-Content -Path $secretFile -Value $openAiApiKey -NoNewline
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & $gcloud secrets versions add $OpenAiSecretName --data-file $secretFile *> $null
        $secretVersionExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorActionPreference
        if ($secretVersionExitCode -ne 0) {
            throw "Failed to add a Secret Manager version for '$OpenAiSecretName'."
        }
    } finally {
        Remove-Item -LiteralPath $secretFile -Force -ErrorAction SilentlyContinue
    }
} elseif ($secretDescribeExitCode -ne 0) {
    throw "OPENAI_API_KEY was not found in the environment or .env, and Secret Manager secret '$OpenAiSecretName' does not exist."
}

$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $gcloud secrets add-iam-policy-binding $OpenAiSecretName `
    --member "serviceAccount:$runtimeServiceAccount" `
    --role "roles/secretmanager.secretAccessor" *> $null
$secretIamExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($secretIamExitCode -ne 0) {
    throw "Failed to grant Cloud Run access to Secret Manager secret '$OpenAiSecretName'."
}

$envVars = "DB_PATH=/tmp/projects.db,UPLOADS_DIR=/tmp/uploads,GCS_BUCKET=$DataBucket,GCS_PREFIX=$DataPrefix,APP_TIMEZONE=America/Phoenix,LLM_PROVIDER=$llmProvider,OPENAI_MODEL=$openAiModel"
if ($openAiReviewModel) {
    $envVars = "$envVars,OPENAI_REVIEW_MODEL=$openAiReviewModel"
}
if ($openAiPlannerModel) {
    $envVars = "$envVars,OPENAI_PLANNER_MODEL=$openAiPlannerModel"
}
if ($recipeOcrModel) {
    $envVars = "$envVars,RECIPE_OCR_MODEL=$recipeOcrModel"
}
if ($recipeCleanupModel) {
    $envVars = "$envVars,RECIPE_CLEANUP_MODEL=$recipeCleanupModel"
}
if ($RegistrationCode) {
    $envVars = "$envVars,DIETER_REGISTRATION_CODE=$RegistrationCode"
}

& $gcloud run deploy $ServiceName `
    --source . `
    --region $Region `
    --allow-unauthenticated `
    --min-instances $MinInstances `
    --max-instances 1 `
    --concurrency 1 `
    --memory 1Gi `
    --cpu 1 `
    --update-env-vars $envVars `
    --set-secrets "OPENAI_API_KEY=$OpenAiSecretName`:latest"
