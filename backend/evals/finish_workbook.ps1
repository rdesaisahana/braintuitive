param(
    [string]$Path,
    [string]$ChartRange = "",
    [string]$ChartAnchor = "A40",
    [string]$ChartSheet = "Summary"
)
# Finishes an eval workbook in Excel: fits every row to its text, adds a chart
# of your grades against their targets, and saves (which also stores the
# results of the formulas). Called by run_quiz_generator_eval.py.
$ErrorActionPreference = 'Stop'
$xl = New-Object -ComObject Excel.Application
try {
    $xl.Visible = $false
    $xl.DisplayAlerts = $false
    $wb = $xl.Workbooks.Open($Path)
    foreach ($ws in $wb.Worksheets) { $null = $ws.UsedRange.Rows.AutoFit() }
    $chart = "no chart"
    if ($ChartRange) {
        try {
            $ws = $wb.Worksheets.Item($ChartSheet)
            $anchor = $ws.Range($ChartAnchor)
            # 216 = default chart style, 57 = clustered bar
            $shape = $ws.Shapes.AddChart2(216, 57, $anchor.Left, $anchor.Top, 560, 300)
            $shape.Chart.SetSourceData($ws.Range($ChartRange))
            $shape.Chart.HasTitle = $true
            $shape.Chart.ChartTitle.Text = "Your grades against their targets"
            $chart = "chart added"
        }
        catch { $chart = "chart skipped: " + $_.Exception.Message }
    }
    $wb.Worksheets.Item(1).Activate()
    $wb.Save()
    $wb.Close($false)
    "rows fitted, $chart"
}
finally {
    $xl.Quit()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($xl)
}
