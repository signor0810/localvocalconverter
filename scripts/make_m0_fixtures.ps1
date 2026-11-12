# Generates the M0 synthetic benchmark set (owned by Orchestrator).
# Public computational-lithography wording only (no company data), zh-TW + en mixed,
# synthesized with Windows SAPI (Microsoft Hanhan zh-TW). Output: benchmarks/m0/*.wav + *.json
param([string]$OutDir = "$PSScriptRoot\..\benchmarks\m0")
Add-Type -AssemblyName System.Speech
New-Item -ItemType Directory -Force $OutDir | Out-Null

$items = @(
  @{ t = "今天先講 ILT 的基本概念，它把光罩設計當成一個反向問題來解。"; k = @("ILT") },
  @{ t = "SMO 會同時最佳化 source 跟 mask，讓製程窗口變大。"; k = @("SMO","source","mask") },
  @{ t = "在 High NA EUV 底下，焦深變淺，所以 defocus 的影響更明顯。"; k = @("High NA","EUV","defocus") },
  @{ t = "我們的 cost function 包含 EPE 跟 image contrast 兩個項目。"; k = @("cost function","EPE") },
  @{ t = "curvilinear mask 可以讓 OPC 的結果更接近理想的 aerial image。"; k = @("curvilinear mask","OPC","aerial image") },
  @{ t = "SRAF 的擺放位置會影響主圖形在不同 dose 下的穩定度。"; k = @("SRAF","dose") },
  @{ t = "這一頁的重點是 Double Patterning 跟 EUV 單次曝光的成本比較，這裡放圖一。"; k = @("Double Patterning","EUV") },
  @{ t = "resist 模型的 threshold 需要用 wafer 量測資料來校正。"; k = @("resist","wafer") },
  @{ t = "pupil 的形狀決定照明條件，SMO 會把它當成可調參數。"; k = @("pupil","SMO") },
  @{ t = "總結一下，ILT 搭配 High NA 可以改善 EPE，但運算量很大。"; k = @("ILT","High NA","EPE") }
)

$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
$i = 1
foreach ($it in $items) {
  $name = "m0_{0:D2}" -f $i
  $s = New-Object System.Speech.Synthesis.SpeechSynthesizer
  $s.SelectVoice("Microsoft Hanhan Desktop")
  $s.SetOutputToWaveFile((Join-Path $OutDir "$name.wav"), $fmt)
  $s.Speak($it.t); $s.Dispose()
  $json = @{ expected_terms = $it.k; reference = $it.t } | ConvertTo-Json -Compress
  [System.IO.File]::WriteAllText((Join-Path $OutDir "$name.json"), $json, (New-Object System.Text.UTF8Encoding($false)))
  $i++
}
Write-Output "Wrote $($items.Count) fixtures to $OutDir"
