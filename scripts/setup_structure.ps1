$dirs = @(
    "agents","airflow/dags","airflow/plugins","alembic/versions",
    "app/core","app/database","app/models","app/pipelines",
    "app/routers","app/scoring","app/services",
    "config","data","docker","docs","infra/terraform/snowflake",
    "mcp","output","results","scripts","streamlit_app","tests"
)
foreach ($dir in $dirs) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    Write-Host "  Created: $dir" -ForegroundColor Green
}
$pyDirs = @("agents","app","app/core","app/database","app/models",
            "app/pipelines","app/routers","app/scoring","app/services","tests")
foreach ($dir in $pyDirs) {
    if (-not (Test-Path "$dir/__init__.py")) {
        New-Item -ItemType File -Force -Path "$dir/__init__.py" | Out-Null
    }
}
@("app/config.py","app/database.py","app/main.py") | ForEach-Object {
    if (-not (Test-Path $_)) { New-Item -ItemType File -Force -Path $_ | Out-Null }
}
Write-Host "`n  Structure created. Next: cd infra/terraform/snowflake && terraform init" -ForegroundColor Cyan
