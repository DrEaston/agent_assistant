param(
    [string]$ProjectId = $env:GOOGLE_CLOUD_PROJECT,
    [string]$ServiceName = "dieter",
    [string]$Region = "us-central1",
    [string]$DataBucket = $env:DIETER_GCS_BUCKET,
    [string]$DataPrefix = $env:DIETER_GCS_PREFIX,
    [string]$OpenAiSecretName = "dieter-openai-api-key",
    [string]$RegistrationCode = $env:DIETER_REGISTRATION_CODE,
    [int]$MinInstances = -1,
    # Which app this service runs: "api:app" (the main app) or e.g. "apps.kitchen.thanksgiving.service:app".
    [string]$AppModule = "api:app",
    # Requests one instance handles at once. There is still only one instance (each app's SQLite file has one owner).
    [int]$Concurrency = 8,
    # Extra NAME=value settings for this service, comma-separated (e.g. "PLATFORM_URL=https://...").
    [string]$ExtraEnv = "",
    # Shared key that signs calls between Dieter services (core/internal.py). Created with a random value if missing.
    [string]$InternalKeySecretName = "dieter-internal-key",
    [string]$AppSecretKeyName = "dieter-secret-key",
    # Identity the service runs as. Default: the project's compute account. Previews use their own, narrower one.
    [string]$RuntimeServiceAccount = "",
    # Previews: read a copy of the live data at startup and never write it back (bucket read access only).
    [switch]$DataReadOnly,
    # Fine-grained GitHub token Studio uses to open issues for Claude Code (main service only, if it exists).
    [string]$GitHubTokenSecretName = "dieter-github-token"
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
$runtimeServiceAccount = if ($RuntimeServiceAccount) { $RuntimeServiceAccount } else { "$projectNumber-compute@developer.gserviceaccount.com" }
$bucketRole = if ($DataReadOnly) { "roles/storage.objectViewer" } else { "roles/storage.objectAdmin" }
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $gcloud storage buckets add-iam-policy-binding $bucketUri `
    --member "serviceAccount:$runtimeServiceAccount" `
    --role $bucketRole *> $null
$ErrorActionPreference = $previous

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
$openAiTranscriptionModel = $env:OPENAI_TRANSCRIPTION_MODEL
if (-not $openAiTranscriptionModel) {
    $openAiTranscriptionModel = Get-DotEnvValue "OPENAI_TRANSCRIPTION_MODEL"
}
if (-not $openAiTranscriptionModel) {
    $openAiTranscriptionModel = "gpt-4o-mini-transcribe"
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

# Random keys this app needs, kept in Secret Manager. Never printed; each is generated once.
#   dieter-internal-key: signs calls between Dieter services (core/internal.py)
#   dieter-secret-key:   SECRET_KEY, signs the app's confirm-before-saving tokens
function Ensure-RandomSecret {
    param([string]$Name)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $gcloud secrets describe $Name *> $null
    $exists = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $previous
    if (-not $exists) {
        $bytes = New-Object byte[] 48
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $keyFile = New-TemporaryFile
        try {
            Set-Content -Path $keyFile -Value ([Convert]::ToBase64String($bytes)) -NoNewline
            # gcloud reports progress on stderr; with "Stop" that would abort even on success.
            $ErrorActionPreference = "Continue"
            & $gcloud secrets create $Name --replication-policy automatic --data-file $keyFile *> $null
            $created = ($LASTEXITCODE -eq 0)
            $ErrorActionPreference = $previous
            if (-not $created) { throw "Failed to create Secret Manager secret '$Name'." }
        } finally {
            Remove-Item -LiteralPath $keyFile -Force -ErrorAction SilentlyContinue
        }
    }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $gcloud secrets add-iam-policy-binding $Name `
        --member "serviceAccount:$runtimeServiceAccount" `
        --role "roles/secretmanager.secretAccessor" *> $null
    $ErrorActionPreference = $previous
}
Ensure-RandomSecret $InternalKeySecretName
Ensure-RandomSecret $AppSecretKeyName

$envVars = "APP_MODULE=$AppModule,DB_PATH=/tmp/projects.db,UPLOADS_DIR=/tmp/uploads,GCS_BUCKET=$DataBucket,GCS_PREFIX=$DataPrefix,APP_TIMEZONE=America/Phoenix,LLM_PROVIDER=$llmProvider,OPENAI_MODEL=$openAiModel"
if ($env:DEMO_MODE) {
    $envVars = "$envVars,DEMO_MODE=$($env:DEMO_MODE)"
}
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
if ($openAiTranscriptionModel) {
    $envVars = "$envVars,OPENAI_TRANSCRIPTION_MODEL=$openAiTranscriptionModel"
}
if ($RegistrationCode) {
    $envVars = "$envVars,DIETER_REGISTRATION_CODE=$RegistrationCode"
}
if ($DataReadOnly) {
    $envVars = "$envVars,GCS_READ_ONLY=1"
}
if ($ExtraEnv) {
    $envVars = "$envVars,$ExtraEnv"
}

# --set-secrets replaces the whole list, so every secret the service uses is named here.
$secrets = "OPENAI_API_KEY=$OpenAiSecretName`:latest,DIETER_INTERNAL_KEY=$InternalKeySecretName`:latest,SECRET_KEY=$AppSecretKeyName`:latest"
# Studio's "Send to Claude" token, on the main app only (never on previews), once it exists in Secret Manager.
if ($ServiceName -eq "dieter" -and $GitHubTokenSecretName) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & $gcloud secrets describe $GitHubTokenSecretName *> $null
    $hasGitHubToken = ($LASTEXITCODE -eq 0)
    if ($hasGitHubToken) {
        & $gcloud secrets add-iam-policy-binding $GitHubTokenSecretName `
            --member "serviceAccount:$runtimeServiceAccount" `
            --role "roles/secretmanager.secretAccessor" *> $null
    }
    $ErrorActionPreference = $previous
    if ($hasGitHubToken) {
        $secrets = "$secrets,DIETER_GITHUB_TOKEN=$GitHubTokenSecretName`:latest"
    }
}

& $gcloud run deploy $ServiceName `
    --source . `
    --region $Region `
    --allow-unauthenticated `
    --service-account $runtimeServiceAccount `
    --min-instances $MinInstances `
    --max-instances 1 `
    --concurrency $Concurrency `
    --memory 1Gi `
    --cpu 1 `
    --update-env-vars $envVars `
    --set-secrets $secrets
