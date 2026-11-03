# Developer-only offline kernel gate. No platform login or send commands.
# 分层：-Tier fast|safety|full 只决定「什么时候跑」，不决定「跑不跑」。
# full 永远保留（nightly + 发布前手动），只有 full 能当发布凭据；任何层的通过都不得替代 full。
param(
    [ValidateSet('fast', 'safety', 'full')]
    [string]$Tier = 'full'
)
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
        'test_check_install',
        'test_scripts_encoding',
        'test_reply_windows',
        'test_reply_multi_session',
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
    # 实测分层（本机 90 模块总 585s）：fast 33 个约 14s；safety = fast + 安全边界，约 4-5 分钟；
    # full = 上面完整列表。列表改动必须同时更新本注释里的数量，避免注释与代码漂移。
    $jobkitFastTests = @(
        'test_filters', 'test_pace', 'test_source_routing', 'test_apply_retry',
        'test_applicant_job_search', 'test_applicant_job_match', 'test_applicant_job_detail',
        'test_applicant_action', 'test_applicant_greeting', 'test_applicant_conversation',
        'test_schedule', 'test_today', 'test_greet_snapshot', 'test_dom_channel',
        'test_exec_runners', 'test_startup_safety', 'test_kernel_boundary',
        'test_concurrency_probe', 'test_send_ledger', 'test_delivery_intent',
        'test_followup', 'test_followup_execution_safety', 'test_execution_names',
        'test_independence', 'test_local_access', 'test_scripts_encoding',
        'test_version_endpoint', 'test_api_contract', 'test_workspace_ui_contract',
        'test_chat_read_state', 'test_reply_draft', 'test_reply_loop',
        'test_port_consistency.TestPortConsistency.test_all_files_agree'
    )
    $jobkitSafetyTests = $jobkitFastTests + @(
        'test_kernel_integration', 'test_kernel_observability', 'test_runtime_kernel',
        'test_runtime_api', 'test_runtime_recovery', 'test_execution_receipts',
        'test_control_receipts', 'test_ledger_snapshot', 'test_outcome_gate',
        'test_send_ledger_archive', 'test_acceptance_regression', 'test_data_retention',
        'test_run_state', 'test_action_contract', 'test_api_contract_http',
        'test_restore_preflight', 'test_restore_commit', 'test_workspace_export',
        'test_applicant_send', 'test_applicant_forget', 'test_applicant_roundtrip',
        'test_port_consistency.TestPortConsistency.test_renderer_uses_its_serving_origin'
    )
    switch ($Tier) {
        'fast' { $jobkitTests = $jobkitFastTests }
        'safety' { $jobkitTests = $jobkitSafetyTests }
    }
    Write-Host ('TIER=' + $Tier + ' modules=' + $jobkitTests.Count)
    # 无缓冲 + 先落盘再回显：进程如果崩在中途，Tee 缓冲会丢掉堆栈尾部，
    # 而文件句柄由 Python 自己持有，崩溃时 OS 会 flush。artifact 已收集 kernel-check*/**/*.log。
    $jobkitLog = Join-Path $jobkitRun 'python-unittest.log'
    & python -u -m unittest -v @jobkitTests *> $jobkitLog
    $jobkitExitCode = $LASTEXITCODE
    if (Test-Path $jobkitLog) { Get-Content -Path $jobkitLog -Encoding utf8 | ForEach-Object { Write-Host $_ } }
    if ($jobkitExitCode -eq 0 -and $Tier -ne 'fast') {
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
