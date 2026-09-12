param(
    [string]$Master,
    [string]$RunFile,
    [string]$Suffix = "2",
    [string]$ComparisonJson = ""
)
# Copies a run's sheets into the master workbook (renaming them with the
# suffix) and rebuilds the Comparison sheet. Excel does the copying, so
# formulas, dropdowns, highlighting and charts survive. Called by
# combine_runs.py. The master workbook must be closed in Excel.
#
# Note the variable names: PowerShell ignores case, so a local $master would be
# the same variable as the [string]$Master parameter, and assigning a workbook
# to it would silently turn the workbook into a string.
$ErrorActionPreference = 'Stop'
$missing = [System.Reflection.Missing]::Value
$xl = New-Object -ComObject Excel.Application
try {
    $xl.Visible = $false
    $xl.DisplayAlerts = $false

    $target = $xl.Workbooks.Open($Master)
    # Excel returns nothing rather than failing when it cannot really open a
    # file -- another Excel holding it, or Protected View. Say so plainly.
    if ($null -eq $target) { throw "Excel could not open '$Master'. Close it in Excel and try again." }
    if ($target.ReadOnly) { throw "'$Master' is open in Excel. Close it and run this again." }
    $source = $xl.Workbooks.Open($RunFile)
    if ($null -eq $source) { throw "Excel could not open '$RunFile'." }
    "opened {0} ({1} sheets) and {2} ({3} sheets)" -f $target.Name, $target.Sheets.Count, $source.Name, $source.Sheets.Count

    $before = @($target.Worksheets | ForEach-Object { $_.Name })
    # Copy all the run's sheets in one go, so their formulas point at each
    # other rather than back at the run's own file. Excel needs its "missing"
    # marker for the Before argument and a real sheet for After; $null for
    # either makes it refuse with "Unable to get the Copy property".
    $source.Sheets.Copy($missing, $target.Sheets.Item($target.Sheets.Count))
    $source.Close($false)

    $added = @($target.Worksheets | ForEach-Object { $_.Name } | Where-Object { $before -notcontains $_ })
    foreach ($name in @('Summary', 'Results', 'Cases')) {
        $copy = $added | Where-Object { $_ -like "$name*" } | Select-Object -First 1
        if ($copy) {
            $target.Worksheets.Item($copy).Name = "$name $Suffix"
            "renamed '$copy' -> '$name $Suffix'"
        }
    }

    if ($ComparisonJson -and (Test-Path -LiteralPath $ComparisonJson)) {
        $plan = Get-Content -Raw -Encoding UTF8 $ComparisonJson | ConvertFrom-Json
        $old = $target.Worksheets | Where-Object { $_.Name -eq 'Comparison' }
        if ($old) { $old.Delete() }
        $sheet = $target.Worksheets.Add($missing, $target.Worksheets.Item($target.Worksheets.Count))
        $sheet.Name = 'Comparison'
        for ($i = 0; $i -lt $plan.widths.Count; $i++) {
            $sheet.Columns.Item($i + 1).ColumnWidth = $plan.widths[$i]
        }
        $r = 1
        foreach ($row in $plan.rows) {
            $c = 1
            foreach ($cell in $row) {
                $at = $sheet.Cells.Item($r, $c)
                if ($cell.format) { $at.NumberFormat = $cell.format }
                if ($cell.formula) { $at.Formula = "=" + $cell.formula }
                elseif ($null -ne $cell.text) { $at.Value2 = $cell.text }
                if ($cell.bold) { $at.Font.Bold = $true }
                $c++
            }
            $r++
        }
        $null = $sheet.UsedRange.Rows.AutoFit()
        $sheet.Activate()
        "comparison sheet rebuilt ({0} rows)" -f ($r - 1)
    }

    $target.Save()
    $target.Close($false)
    "saved"
}
finally {
    $xl.Quit()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($xl)
}
