netsh interface set interface name="Wi-Fi" admin=disabled
netsh interface set interface name="Wi-Fi" admin=enabled
netsc stop WlanSvc
netsc start WlanSvc

:: 2. Force the Radio state via a direct Registry Toggle for Win 10
:: This flips the "Software On" switch directly
reg add "HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}\0001" /v "RadioEnable" /t REG_DWORD /d 1 /f
