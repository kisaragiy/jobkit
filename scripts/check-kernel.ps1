# Developer-only offline kernel gate. No platform login or send commands.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$jobkitRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$jobkitPreviousDb = $env:JOBKIT_DB
$jobkitPreviousEncoding = $env:PYTHONIOENCODING
$jobkitModelEnv = @{}
foreach ($jobkitName in @('JOBKIT_DRAFT_BACKEND', 'JOBKIT_LLM_TIMEOUT_SECONDS', 'JOBKIT_LLM_PROVIDER',
    'JOBKIT_LLM_MODEL', 'JOBKIT_LLM_API_KEY', 'JOBKIT_LLM_EXTERNAL_ALLOWED',
    'JOBKIT_LLM_DAILY_REQUEST_LIMIT', 'JOBKIT_LLM_DAILY_RESERVATION_LIMIT')) {
    $jobkitModelEnv[$jobkitName] = [Environment]::GetEnvironmentVariable($jobkitName, 'Process')
}
$jobkitExitCode = 0
Push-Location $jobkitRoot
try {
    $null = Get-Command python -ErrorAction Stop
    $null = Get-Command node -ErrorAction Stop
    $jobkitRun = Join-Path $jobkitRoot ('tmp\kernel-check-' + [guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $jobkitRun
    $env:JOBKIT_DB = Join-Path $jobkitRun 'isolated.db'
    $env:PYTHONIOENCODING = 'utf-8'
    $env:JOBKIT_DRAFT_BACKEND = 'rules'
    $env:JOBKIT_LLM_TIMEOUT_SECONDS = '15'
    $env:JOBKIT_LLM_PROVIDER = ''
    $env:JOBKIT_LLM_MODEL = ''
    $env:JOBKIT_LLM_API_KEY = ''
    $env:JOBKIT_LLM_EXTERNAL_ALLOWED = 'no'
    $env:JOBKIT_LLM_DAILY_REQUEST_LIMIT = '100'
    $env:JOBKIT_LLM_DAILY_RESERVATION_LIMIT = '1000000'
    $jobkitTests = @(
        'test_manual_queue',
        'test_batch_drafting',
        'test_manual_draft',
        'test_task_search',
        'test_queue_triage',
        'test_workspace_export',
        'test_restore_preflight',
        'test_restore_commit',
        'test_combined_export',
        'test_llm_draft',
        'test_llm_transport',
        'test_model_usage',
        'test_model_review',
        'test_model_readiness',
        'test_model_cancellation',
        'test_model_tls_io',
        'test_database_backup',
        'test_applicant_delivery',
        'test_applicant_browser',
        'test_applicant_observation',
        'test_applicant_greeting_intent',
        'test_applicant_salary',
        'test_applicant_viewport',
        'test_applicant_salary_review',
        'test_applicant_binding',
        'test_applicant_chat',
        'test_applicant_messages',
        'test_applicant_reply_intent',
        'test_applicant_send',
        'test_applicant_forget',
        'test_applicant_roundtrip',
        'test_api_contract',
        'test_api_contract_http',
        'test_applicant_job_search',
        'test_job_search_contract',
        'test_applicant_job_match',
        'test_applicant_job_detail',
        'test_job_search_details',
        'test_kernel_integration',
        'test_version_endpoint',
        'test_check_version',
        'test_scripts_encoding',
        'test_reply_windows',
        'test_concurrency_probe',
        'test_execution_names',
        'test_applicant_action',
        'test_action_contract',
        'test_applicant_greeting',
        'test_greeting_contract',
        'test_real_chat',
        'test_applicant_conversation',
        'test_workspace_ui_contract',
        'test_applicant_queue',
        'test_acceptance_regression',
        'test_send_ledger',
        'test_send_ledger_archive',
        'test_outcome_gate',
        'test_run_state',
        'test_data_retention',
        'test_draft_replacement',
        'test_execution_receipts', 'test_control_receipts', 'test_kernel_observability',
        'test_startup_safety', 'test_runtime_recovery', 'test_runtime_kernel',
        'test_runtime_api', 'test_kernel_boundary', 'test_local_access',
        'test_delivery_intent', 'test_apply_retry', 'test_reply_draft', 'test_chat_send',
        'test_followup_execution_safety', 'test_followup', 'test_exec_runners',
        'test_reply_loop', 'test_ledger_snapshot',
        'test_port_consistency.TestPortConsistency.test_all_files_agree',
        'test_port_consistency.TestPortConsistency.test_renderer_uses_its_serving_origin',
        'test_source_routing', 'test_pace', 'test_filters', 'test_dom_channel',
        'test_schedule', 'test_today', 'test_independence', 'test_chat_read_state',
        'test_greet_snapshot'
    )
    & python -m unittest @jobkitTests
    $jobkitExitCode = $LASTEXITCODE
    if ($jobkitExitCode -eq 0) {
        foreach ($jobkitScript in @('test_local_access.js', 'test_delivery_loop.js',
            'test_login_probe.js', 'test_delivery.js', 'test_blocker.js', 'test_workspace.js', 'test_applicant_dom.js', 'test_messages_dom.js', 'test_applicant_job_detail.js')) {
            & node (Join-Path 'shell' $jobkitScript)
            if ($LASTEXITCODE -ne 0) {
                $jobkitExitCode = $LASTEXITCODE
                break
            }
        }
    }
    if ($jobkitExitCode -eq 0) { Write-Host 'PASS: selected Python kernel suite and eight JavaScript checks.' }
    Write-Host ('Isolated test data: ' + $jobkitRun)
}
catch {
    Write-Host ('CHECK FAILED: ' + $_.Exception.Message)
    $jobkitExitCode = 1
}
finally {
    foreach ($jobkitName in $jobkitModelEnv.Keys) {
        [Environment]::SetEnvironmentVariable($jobkitName, $jobkitModelEnv[$jobkitName], 'Process')
    }
    if ($null -eq $jobkitPreviousDb) { Remove-Item Env:JOBKIT_DB -ErrorAction SilentlyContinue }
    else { $env:JOBKIT_DB = $jobkitPreviousDb }
    if ($null -eq $jobkitPreviousEncoding) { Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue }
    else { $env:PYTHONIOENCODING = $jobkitPreviousEncoding }
    Pop-Location
}
exit $jobkitExitCode
