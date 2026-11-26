# Input/output is UTF-8 JSON over anonymous pipes. No image is written to disk.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $streamType = [Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
    $writerType = [Windows.Storage.Streams.DataWriter,Windows.Storage.Streams,ContentType=WindowsRuntime]
    $decoderType = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    $bitmapType = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    $pixelType = [Windows.Graphics.Imaging.BitmapPixelFormat,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    $alphaType = [Windows.Graphics.Imaging.BitmapAlphaMode,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    $engineType = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
    $resultType = [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
    $languageType = [Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]
    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    } | Select-Object -First 1
    function Await-Result($operation, [Type]$type) {
        $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
        $task.Wait()
        return $task.Result
    }
    $inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $language = [string]$inputData.language
    if ($language -notin @('de-DE','ru','en-US')) { throw 'OCR_LANGUAGE_INVALID' }
    $engine = $engineType::TryCreateFromLanguage($languageType::new($language))
    if ($null -eq $engine) { throw 'OCR_LANGUAGE_NOT_INSTALLED' }
    $bytes = [Convert]::FromBase64String([string]$inputData.image)
    if ($bytes.Length -gt 2500000) { throw 'OCR_IMAGE_LIMIT' }
    $stream = $streamType::new()
    $writer = $writerType::new($stream)
    try {
        $writer.WriteBytes($bytes)
        $null = Await-Result ($writer.StoreAsync()) ([uint32])
        $null = Await-Result ($writer.FlushAsync()) ([bool])
        $null = $writer.DetachStream()
        $stream.Seek(0)
        $decoder = Await-Result ($decoderType::CreateAsync($stream)) $decoderType
        if ($decoder.PixelWidth -gt $engineType::MaxImageDimension -or
            $decoder.PixelHeight -gt $engineType::MaxImageDimension) { throw 'OCR_IMAGE_DIMENSIONS' }
        $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync($pixelType::Bgra8,$alphaType::Premultiplied)) $bitmapType
        try {
            $result = Await-Result ($engine.RecognizeAsync($bitmap)) $resultType
            $text = ($result.Lines | ForEach-Object { $_.Text }) -join [Environment]::NewLine
            [Console]::WriteLine((@{text=$text} | ConvertTo-Json -Compress))
        } finally { $bitmap.Dispose() }
    } finally { $writer.Dispose(); $stream.Dispose() }
} catch {
    $code = $_.Exception.Message
    if ($code -notin @('OCR_LANGUAGE_INVALID','OCR_LANGUAGE_NOT_INSTALLED','OCR_IMAGE_LIMIT','OCR_IMAGE_DIMENSIONS')) {
        $code = 'OCR_WINDOWS_FAILED'
    }
    [Console]::WriteLine((@{error=$code} | ConvertTo-Json -Compress))
    exit 1
}
