@echo off
rem Creates a self-signed HTTPS certificate in .\certs so phones on the local network can use GPS.
rem Uses the openssl.exe that ships with Git for Windows. For production use a real certificate.
chcp 65001 >nul
cd /d "%~dp0"
set OPENSSL=openssl
where openssl >nul 2>nul || set OPENSSL="C:\Program Files\Git\usr\bin\openssl.exe"
if not exist certs mkdir certs
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do set IP=%%a
set IP=%IP: =%
%OPENSSL% req -x509 -newkey rsa:2048 -nodes -days 825 -keyout certs\key.pem -out certs\cert.pem ^
  -subj "/CN=attendance.local" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:%IP%"
echo.
echo Done. Restart the server; it will now run on https://%IP%:8000
pause
