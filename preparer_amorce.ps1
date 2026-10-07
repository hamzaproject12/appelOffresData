# Fabrique l'archive JSON que le pod Railway dépliera sur son volume au premier démarrage.
#
# Le pod ne peut pas reconstruire la base sans les extraits de PV déjà collectés, et les
# retélécharger prendrait plus d'une journée. Ils voyagent donc une fois dans le dépôt, puis
# le volume prend le relais et s'enrichit tout seul.
#
#   cd C:\pv\pv_travaux\site
#   powershell -ExecutionPolicy Bypass -File preparer_amorce.ps1
#
# GitHub refuse un fichier de plus de 100 Mo et grogne au-delà de 50. Le script mesure avant
# de compresser et s'arrête si c'est trop gros, plutôt que de te laisser découvrir le refus
# au moment du push.

$ErrorActionPreference = "Stop"
$site = Split-Path -Parent $MyInvocation.MyCommand.Path
$donnees = Split-Path -Parent $site
Set-Location $donnees

$dossiers = @("consultations", "extraits", "resultats") | Where-Object { Test-Path $_ }
if (-not $dossiers) { Write-Host "Aucune donnée dans $donnees" -ForegroundColor Red; exit 1 }

Write-Host "`nContenu de $donnees :" -ForegroundColor Yellow
$total = 0
foreach ($d in $dossiers) {
    $m = Get-ChildItem $d -Recurse -File -ErrorAction SilentlyContinue |
         Measure-Object -Property Length -Sum
    $mo = [math]::Round($m.Sum / 1MB, 1)
    $total += $m.Sum
    Write-Host ("  {0,-16} {1,8} fichiers {2,10} Mo" -f $d, $m.Count, $mo)
}
Write-Host ("  {0,-16} {1,19} Mo au total" -f "", [math]::Round($total / 1MB, 1))
Write-Host "`nLe JSON se compresse d'environ 10 fois : vise ~$([math]::Round($total/1MB/10,0)) Mo compressés." -ForegroundColor DarkGray

$cible = Join-Path $site "amorce.tar.gz"
Remove-Item $cible -ErrorAction SilentlyContinue
Write-Host "`nCompression vers $cible — quelques minutes…" -ForegroundColor Yellow
tar -czf $cible $dossiers
if ($LASTEXITCODE -ne 0) { Write-Host "tar a échoué (code $LASTEXITCODE)" -ForegroundColor Red; exit 1 }

$mo = [math]::Round((Get-Item $cible).Length / 1MB, 1)
Write-Host "`namorce.tar.gz : $mo Mo" -ForegroundColor Green
if ($mo -gt 95) {
    Write-Host "`nTROP GROS pour GitHub (limite 100 Mo)." -ForegroundColor Red
    Write-Host "Deux issues : activer Git LFS pour ce fichier, ou laisser la collecte sur ton PC"
    Write-Host "(tâche planifiée Windows + git push de la base). Dis-le-moi, je te donne les commandes."
    exit 1
}
if ($mo -gt 50) {
    Write-Host "Au-dessus de 50 Mo, GitHub accepte mais affiche un avertissement. C'est sans gravité." -ForegroundColor Yellow
}
Write-Host "`nÀ ajouter au dépôt :  git add amorce.tar.gz" -ForegroundColor Yellow
Write-Host "Une fois le volume rempli, ce fichier ne sert plus qu'au cas où tout serait à refaire."
