@echo off
rem Sinav takip script'ini baslatir; kapanirsa 60 sn sonra yeniden acar.
chcp 65001 >nul
cd /d "%~dp0"
title Sinav Takip

:dongu
echo [%date% %time%] Sinav takip baslatiliyor...
python -u sinav_takip.py
echo [%date% %time%] Script kapandi. 60 sn sonra yeniden baslatilacak (durdurmak icin bu pencereyi kapat).
timeout /t 60 /nobreak >nul
goto dongu
