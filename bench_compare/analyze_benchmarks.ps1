function Get-Num {
    param([object]$Value)
    if ($null -eq $Value) { return $null }
    $s = [string]$Value
    if ([string]::IsNullOrWhiteSpace($s)) { return $null }
    return [double]$s
}

function Get-Stats {
    param([double[]]$Values)
    $xs = @($Values | Where-Object { $_ -ne $null })
    if ($xs.Count -eq 0) {
        return [ordered]@{
            n = 0
            mean = $null
            sd = $null
            min = $null
            max = $null
        }
    }

    $mean = ($xs | Measure-Object -Average).Average
    $sumSq = 0.0
    foreach ($x in $xs) {
        $sumSq += ($x - $mean) * ($x - $mean)
    }
    $variance = $sumSq / $xs.Count
    return [ordered]@{
        n = $xs.Count
        mean = $mean
        sd = [Math]::Sqrt($variance)
        min = ($xs | Measure-Object -Minimum).Minimum
        max = ($xs | Measure-Object -Maximum).Maximum
    }
}

function Round-Num {
    param(
        [Nullable[Double]]$Value,
        [int]$Digits = 3
    )
    if ($null -eq $Value) { return $null }
    return [Math]::Round($Value, $Digits)
}

function Get-PctFaster {
    param(
        [Nullable[Double]]$Baseline,
        [Nullable[Double]]$Candidate
    )
    if ($null -eq $Baseline -or $null -eq $Candidate -or $Baseline -eq 0) {
        return $null
    }
    return (($Baseline - $Candidate) / $Baseline) * 100.0
}

function Get-SpeedupRatio {
    param(
        [Nullable[Double]]$Baseline,
        [Nullable[Double]]$Candidate
    )
    if ($null -eq $Baseline -or $null -eq $Candidate -or $Candidate -eq 0) {
        return $null
    }
    return $Baseline / $Candidate
}

function To-Ms {
    param([Nullable[Double]]$Ns)
    if ($null -eq $Ns) { return $null }
    return $Ns / 1e6
}

function Get-StageColumns {
    param([object[]]$Rows)
    if ($Rows.Count -eq 0) { return @() }
    return @(
        $Rows[0].PSObject.Properties.Name |
            Where-Object {
                $_ -like "*_ns" -and
                $_ -notin @("offline_ns", "online_ns", "wall_ns")
            }
    )
}

function Get-RunAverageSummary {
    param(
        [object[]]$Rows,
        [string]$ProjectOverride = ""
    )

    $stageCols = Get-StageColumns $Rows
    $metrics = @("offline_ns", "online_ns", "wall_ns") + $stageCols
    $measured = @($Rows | Where-Object { $_.warmup -ne "True" })
    $grouped = @{}

    foreach ($row in $measured) {
        $project = if ($ProjectOverride) { $ProjectOverride } else { [string]$row.project }
        $key = "$project|$($row.run_idx)"
        if (-not $grouped.ContainsKey($key)) {
            $grouped[$key] = @{
                project = $project
                sums = @{}
                counts = @{}
            }
            foreach ($metric in $metrics) {
                $grouped[$key].sums[$metric] = 0.0
                $grouped[$key].counts[$metric] = 0
            }
        }

        foreach ($metric in $metrics) {
            $value = Get-Num $row.$metric
            if ($null -ne $value) {
                $grouped[$key].sums[$metric] += $value
                $grouped[$key].counts[$metric] += 1
            }
        }
    }

    $perProject = @{}
    foreach ($entry in $grouped.Values) {
        $project = $entry.project
        if (-not $perProject.ContainsKey($project)) {
            $perProject[$project] = @{
                offline_ns = @()
                online_ns = @()
                wall_ns = @()
                total_ns = @()
                stages = @{}
            }
            foreach ($stage in $stageCols) {
                $perProject[$project].stages[$stage] = @()
            }
        }

        $offline = if ($entry.counts["offline_ns"]) {
            $entry.sums["offline_ns"] / $entry.counts["offline_ns"]
        } else { $null }
        $online = if ($entry.counts["online_ns"]) {
            $entry.sums["online_ns"] / $entry.counts["online_ns"]
        } else { $null }
        $wall = if ($entry.counts["wall_ns"]) {
            $entry.sums["wall_ns"] / $entry.counts["wall_ns"]
        } else { $null }

        if ($null -ne $offline) { $perProject[$project].offline_ns += $offline }
        if ($null -ne $online) { $perProject[$project].online_ns += $online }
        if ($null -ne $wall) { $perProject[$project].wall_ns += $wall }
        if ($null -ne $offline -and $null -ne $online) {
            $perProject[$project].total_ns += ($offline + $online)
        }

        foreach ($stage in $stageCols) {
            if ($entry.counts[$stage]) {
                $perProject[$project].stages[$stage] += ($entry.sums[$stage] / $entry.counts[$stage])
            }
        }
    }

    $summary = @{}
    foreach ($project in $perProject.Keys) {
        $summary[$project] = [ordered]@{
            offline_ns = Get-Stats $perProject[$project].offline_ns
            online_ns = Get-Stats $perProject[$project].online_ns
            wall_ns = Get-Stats $perProject[$project].wall_ns
            total_ns = Get-Stats $perProject[$project].total_ns
            stages = [ordered]@{}
        }
        foreach ($stage in $stageCols) {
            $summary[$project].stages[$stage] = Get-Stats $perProject[$project].stages[$stage]
        }
    }

    return [ordered]@{
        stageColumns = $stageCols
        summary = $summary
    }
}

function Get-ExpandSummary {
    param([object[]]$Rows)
    $measured = @($Rows | Where-Object { $_.warmup -ne "True" })
    $metrics = @($measured.metric | Sort-Object -Unique)
    $summary = [ordered]@{}

    foreach ($metric in $metrics) {
        $htVals = @($measured | Where-Object { $_.metric -eq $metric -and $_.project -eq "HT" } | ForEach-Object { [double]$_.per_ns })
        $stdVals = @($measured | Where-Object { $_.metric -eq $metric -and $_.project -eq "STD" } | ForEach-Object { [double]$_.per_ns })
        $htStats = Get-Stats $htVals
        $stdStats = Get-Stats $stdVals

        $summary[$metric] = [ordered]@{
            HT = $htStats
            STD = $stdStats
            delta_ns = $stdStats.mean - $htStats.mean
            pct_faster = Get-PctFaster $stdStats.mean $htStats.mean
            speedup_ratio = Get-SpeedupRatio $stdStats.mean $htStats.mean
        }
    }

    return $summary
}

function Get-Comparison {
    param(
        [hashtable]$Summary,
        [string]$BaselineProject,
        [string]$CandidateProject,
        [string]$Metric
    )

    $baseline = $Summary[$BaselineProject][$Metric].mean
    $candidate = $Summary[$CandidateProject][$Metric].mean
    return [ordered]@{
        baseline = $baseline
        candidate = $candidate
        delta_ns = $baseline - $candidate
        pct_faster = Get-PctFaster $baseline $candidate
        speedup_ratio = Get-SpeedupRatio $baseline $candidate
    }
}

function Get-ProjectMetricRow {
    param(
        [hashtable]$Summary,
        [string]$Project,
        [string]$Metric
    )
    $s = $Summary[$Project][$Metric]
    return [ordered]@{
        n = $s.n
        mean_ms = Round-Num (To-Ms $s.mean) 3
        sd_ms = Round-Num (To-Ms $s.sd) 3
        min_ms = Round-Num (To-Ms $s.min) 3
        max_ms = Round-Num (To-Ms $s.max) 3
    }
}

$protocolRows = Import-Csv (Join-Path $PSScriptRoot "results.csv")
$expandRows = Import-Csv (Join-Path $PSScriptRoot "expand_results.csv")
$originalRows = Import-Csv (Join-Path $PSScriptRoot "external_fss_results.csv") | ForEach-Object {
    $_ | Add-Member -NotePropertyName project -NotePropertyValue "ORIG" -PassThru
}

$protocolSummary = Get-RunAverageSummary $protocolRows
$originalSummary = Get-RunAverageSummary $originalRows "ORIG"
$expandSummary = Get-ExpandSummary $expandRows

$topline = [ordered]@{
    ORIG = [ordered]@{
        offline = Get-ProjectMetricRow $originalSummary.summary "ORIG" "offline_ns"
        online = Get-ProjectMetricRow $originalSummary.summary "ORIG" "online_ns"
        total = Get-ProjectMetricRow $originalSummary.summary "ORIG" "total_ns"
        wall = Get-ProjectMetricRow $originalSummary.summary "ORIG" "wall_ns"
    }
    STD = [ordered]@{
        offline = Get-ProjectMetricRow $protocolSummary.summary "STD" "offline_ns"
        online = Get-ProjectMetricRow $protocolSummary.summary "STD" "online_ns"
        total = Get-ProjectMetricRow $protocolSummary.summary "STD" "total_ns"
        wall = Get-ProjectMetricRow $protocolSummary.summary "STD" "wall_ns"
    }
    HT = [ordered]@{
        offline = Get-ProjectMetricRow $protocolSummary.summary "HT" "offline_ns"
        online = Get-ProjectMetricRow $protocolSummary.summary "HT" "online_ns"
        total = Get-ProjectMetricRow $protocolSummary.summary "HT" "total_ns"
        wall = Get-ProjectMetricRow $protocolSummary.summary "HT" "wall_ns"
    }
}

$htVsStdOffline = Get-Comparison $protocolSummary.summary "STD" "HT" "offline_ns"
$htVsStdOnline = Get-Comparison $protocolSummary.summary "STD" "HT" "online_ns"
$htVsStdTotal = Get-Comparison $protocolSummary.summary "STD" "HT" "total_ns"
$htVsStdWall = Get-Comparison $protocolSummary.summary "STD" "HT" "wall_ns"

$stdVsOrigOffline = [ordered]@{
    baseline = $originalSummary.summary["ORIG"].offline_ns.mean
    candidate = $protocolSummary.summary["STD"].offline_ns.mean
}
$stdVsOrigOnline = [ordered]@{
    baseline = $originalSummary.summary["ORIG"].online_ns.mean
    candidate = $protocolSummary.summary["STD"].online_ns.mean
}
$stdVsOrigTotal = [ordered]@{
    baseline = $originalSummary.summary["ORIG"].total_ns.mean
    candidate = $protocolSummary.summary["STD"].total_ns.mean
}

$htVsOrigOffline = [ordered]@{
    baseline = $originalSummary.summary["ORIG"].offline_ns.mean
    candidate = $protocolSummary.summary["HT"].offline_ns.mean
}
$htVsOrigOnline = [ordered]@{
    baseline = $originalSummary.summary["ORIG"].online_ns.mean
    candidate = $protocolSummary.summary["HT"].online_ns.mean
}
$htVsOrigTotal = [ordered]@{
    baseline = $originalSummary.summary["ORIG"].total_ns.mean
    candidate = $protocolSummary.summary["HT"].total_ns.mean
}

$stdStageMeans = @{}
$htStageMeans = @{}
foreach ($stage in $protocolSummary.stageColumns) {
    $stdStageMeans[$stage] = $protocolSummary.summary["STD"].stages[$stage].mean
    $htStageMeans[$stage] = $protocolSummary.summary["HT"].stages[$stage].mean
}

$stageSavings = @()
foreach ($stage in $protocolSummary.stageColumns) {
    $baseline = $stdStageMeans[$stage]
    $candidate = $htStageMeans[$stage]
    if ($null -ne $baseline -and $null -ne $candidate) {
        $stageSavings += [pscustomobject]@{
            stage = $stage
            std_mean_ms = Round-Num (To-Ms $baseline) 3
            ht_mean_ms = Round-Num (To-Ms $candidate) 3
            delta_ms = Round-Num (To-Ms ($baseline - $candidate)) 3
            pct_faster = Round-Num (Get-PctFaster $baseline $candidate) 2
        }
    }
}
$stageSavings = $stageSavings | Sort-Object delta_ms -Descending

$stdOnlineDeltaVsOrig = $protocolSummary.summary["STD"].online_ns.mean - $originalSummary.summary["ORIG"].online_ns.mean
$stdOfflineDeltaVsOrig = $protocolSummary.summary["STD"].offline_ns.mean - $originalSummary.summary["ORIG"].offline_ns.mean
$stdVerifyMean = $stdStageMeans["online_o6_vidpf_verify_ns"]

$impactedConservativeStd = ($stdStageMeans["offline_b2a_idpf_gen_mem_ns"] + $stdStageMeans["online_o4a_middle_idpf_ns"])
$impactedBroadStd = $impactedConservativeStd + $stdStageMeans["online_o3_round_0_ns"]

$output = [ordered]@{
    topline = $topline
    comparisons = [ordered]@{
        HT_vs_STD = [ordered]@{
            offline_ms_delta = Round-Num (To-Ms $htVsStdOffline.delta_ns) 3
            online_ms_delta = Round-Num (To-Ms $htVsStdOnline.delta_ns) 3
            total_ms_delta = Round-Num (To-Ms $htVsStdTotal.delta_ns) 3
            wall_ms_delta = Round-Num (To-Ms $htVsStdWall.delta_ns) 3
            total_pct_faster = Round-Num $htVsStdTotal.pct_faster 2
            total_speedup_ratio = Round-Num $htVsStdTotal.speedup_ratio 3
        }
        STD_vs_ORIG = [ordered]@{
            offline_ms_delta = Round-Num (To-Ms ($stdVsOrigOffline.candidate - $stdVsOrigOffline.baseline)) 3
            online_ms_delta = Round-Num (To-Ms ($stdVsOrigOnline.candidate - $stdVsOrigOnline.baseline)) 3
            total_ms_delta = Round-Num (To-Ms ($stdVsOrigTotal.candidate - $stdVsOrigTotal.baseline)) 3
        }
        HT_vs_ORIG = [ordered]@{
            offline_ms_delta = Round-Num (To-Ms ($htVsOrigOffline.candidate - $htVsOrigOffline.baseline)) 3
            online_ms_delta = Round-Num (To-Ms ($htVsOrigOnline.candidate - $htVsOrigOnline.baseline)) 3
            total_ms_delta = Round-Num (To-Ms ($htVsOrigTotal.candidate - $htVsOrigTotal.baseline)) 3
        }
    }
    expand = [ordered]@{}
    protocol_stages = [ordered]@{
        std_mean_ms = [ordered]@{}
        ht_mean_ms = [ordered]@{}
        top_stage_savings_ms = @($stageSavings | Select-Object -First 8)
    }
    propagation = [ordered]@{
        expand_tls_pct_faster = Round-Num $expandSummary["Full expand (TLS)"].pct_faster 2
        expand_tls_speedup_ratio = Round-Num $expandSummary["Full expand (TLS)"].speedup_ratio 3
        offline_b2a_pct_faster = Round-Num (Get-PctFaster $stdStageMeans["offline_b2a_idpf_gen_mem_ns"] $htStageMeans["offline_b2a_idpf_gen_mem_ns"]) 2
        online_o4a_pct_faster = Round-Num (Get-PctFaster $stdStageMeans["online_o4a_middle_idpf_ns"] $htStageMeans["online_o4a_middle_idpf_ns"]) 2
        offline_total_pct_faster = Round-Num $htVsStdOffline.pct_faster 2
        online_total_pct_faster = Round-Num $htVsStdOnline.pct_faster 2
        total_pct_faster = Round-Num $htVsStdTotal.pct_faster 2
        conservative_impacted_share_of_std_total_pct = Round-Num (($impactedConservativeStd / $protocolSummary.summary["STD"].total_ns.mean) * 100.0) 2
        broad_impacted_share_of_std_total_pct = Round-Num (($impactedBroadStd / $protocolSummary.summary["STD"].total_ns.mean) * 100.0) 2
    }
    tag_overhead = [ordered]@{
        std_minus_orig_offline_ms = Round-Num (To-Ms $stdOfflineDeltaVsOrig) 3
        std_minus_orig_online_ms = Round-Num (To-Ms $stdOnlineDeltaVsOrig) 3
        verify_mean_ms = Round-Num (To-Ms $stdVerifyMean) 3
        verify_share_of_std_online_pct = Round-Num (($stdVerifyMean / $protocolSummary.summary["STD"].online_ns.mean) * 100.0) 4
        verify_share_of_std_minus_orig_online_delta_pct = Round-Num (($stdVerifyMean / $stdOnlineDeltaVsOrig) * 100.0) 2
        note = "Original-vs-Standard uses separate benchmark datasets; Standard/HT runs include timing instrumentation, so treat the absolute delta as an upper bound on pure tag-only overhead."
    }
}

foreach ($metric in $expandSummary.Keys) {
    $output.expand[$metric] = [ordered]@{
        std_mean_ns = Round-Num $expandSummary[$metric].STD.mean 2
        ht_mean_ns = Round-Num $expandSummary[$metric].HT.mean 2
        delta_ns = Round-Num $expandSummary[$metric].delta_ns 2
        pct_faster = Round-Num $expandSummary[$metric].pct_faster 2
        speedup_ratio = Round-Num $expandSummary[$metric].speedup_ratio 3
    }
}

foreach ($stage in $protocolSummary.stageColumns) {
    $output.protocol_stages.std_mean_ms[$stage] = Round-Num (To-Ms $stdStageMeans[$stage]) 3
    $output.protocol_stages.ht_mean_ms[$stage] = Round-Num (To-Ms $htStageMeans[$stage]) 3
}

Write-Host "=== Topline (mean ms, party-averaged per run) ==="
foreach ($project in @("ORIG", "STD", "HT")) {
    $row = $output.topline[$project]
    Write-Host ("{0}: offline={1} ms, online={2} ms, total={3} ms, wall={4} ms" -f `
        $project, $row.offline.mean_ms, $row.online.mean_ms, $row.total.mean_ms, $row.wall.mean_ms)
}
Write-Host ""
Write-Host "=== HT vs STD ==="
Write-Host ("offline delta={0} ms, online delta={1} ms, total delta={2} ms, total faster={3}%" -f `
    $output.comparisons.HT_vs_STD.offline_ms_delta,
    $output.comparisons.HT_vs_STD.online_ms_delta,
    $output.comparisons.HT_vs_STD.total_ms_delta,
    $output.comparisons.HT_vs_STD.total_pct_faster)
Write-Host ""
Write-Host "=== Seed Expansion (production TLS metric) ==="
$expandTls = $output.expand["Full expand (TLS)"]
Write-Host ("STD={0} ns, HT={1} ns, delta={2} ns, faster={3}%" -f `
    $expandTls.std_mean_ns, $expandTls.ht_mean_ns, $expandTls.delta_ns, $expandTls.pct_faster)
Write-Host ""
Write-Host "=== Largest HT Stage Savings (ms) ==="
$stageSavings | Select-Object -First 8 | ForEach-Object {
    Write-Host ("{0}: STD={1} ms, HT={2} ms, delta={3} ms ({4}%)" -f `
        $_.stage, $_.std_mean_ms, $_.ht_mean_ms, $_.delta_ms, $_.pct_faster)
}
Write-Host ""
Write-Host "=== Tag Overhead ==="
Write-Host ("STD-ORIG offline delta={0} ms, STD-ORIG online delta={1} ms, verify={2} ms" -f `
    $output.tag_overhead.std_minus_orig_offline_ms,
    $output.tag_overhead.std_minus_orig_online_ms,
    $output.tag_overhead.verify_mean_ms)
Write-Host ""
Write-Host "=== JSON ==="
$output | ConvertTo-Json -Depth 10
