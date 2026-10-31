$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) {
  & node --check 'D:\ComfyUI_portable_TE_v2\ComfyUI\custom_nodes\ComfyUI-TopazStarlight\web\js\topaz_video_preview.js'
  Write-Output ("node check rc=" + $LASTEXITCODE)
} else {
  Write-Output 'no node'
}
