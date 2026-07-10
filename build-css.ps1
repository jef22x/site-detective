# Rebuild the compiled Tailwind stylesheet after template class changes.
# The output file is committed so the running app needs no build step.
& "$PSScriptRoot\tools\tailwindcss-windows-x64.exe" `
  --input "$PSScriptRoot\app\web\static\input.css" `
  --output "$PSScriptRoot\app\web\static\tailwind.css" `
  --minify
