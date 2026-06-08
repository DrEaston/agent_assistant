param(
    [string]$LocalUrl = "http://localhost:8000/apps/recipes",
    [string]$WifiUrl = "http://192.168.2.40:8000/apps/recipes"
)

$ErrorActionPreference = "Stop"

function Test-AppPage {
    param(
        [string]$Url,
        [string[]]$ExpectedText
    )

    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing
    if ($response.StatusCode -ne 200) {
        throw "$Url returned status $($response.StatusCode)."
    }

    foreach ($text in $ExpectedText) {
        if (-not $response.Content.Contains($text)) {
            throw "$Url did not contain expected text: $text"
        }
    }

    Write-Output "$Url OK"
}

Test-AppPage -Url $LocalUrl -ExpectedText @("Recipe App", "Complete Meals", "Meal Components")
Test-AppPage -Url $WifiUrl -ExpectedText @("Recipe App", "Complete Meals", "Meal Components")
