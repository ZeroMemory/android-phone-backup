# Android Phone Backup

Back up Android shared storage to a verified TAR and export accessible SMS, MMS and contacts. Optional cleanup removes only files that still match the backup.

Use Python 3.10 or later with the standard library. Device operations require ADB and an authorized Android device. Streaming and deletion also require BusyBox on the phone.

The archive starts at `/storage/emulated/0` and includes Telegram files. Cache patterns filter temporary files.

## Back up a phone

Install [Android platform tools](https://developer.android.com/tools/releases/platform-tools), authorize your device and find its serial with `adb devices`. BusyBox defaults to `/data/local/tmp/busybox`. Set `--busybox` for another installed path.

```powershell
python phone_backup_stream.py --adb adb --serial YOUR_SERIAL --label test-phone --output-dir .\backup
python phone_provider_export.py --adb adb --serial YOUR_SERIAL --label test-phone --output-dir .\providers
```

Streaming produces `shared_storage.tar`, a SHA-256 sidecar and `shared_storage_manifest.json` with the device identity and file hashes.

Use a fresh output path. The optional vCard reader opens Contacts provider assets on the device.

## Read vCards as provider assets

Build the reader with the Android SDK and `javac` on PATH. The build targets API 26. Defaults are platform `android-34` and build tools `34.0.0`. Use `-Platform` and `-BuildTools` for other installed versions. Push the DEX to an unused device path.

```powershell
$sdkRoot = Read-Host 'Path to the Android SDK'
.\Build-VCardReader.ps1 -SdkRoot "$sdkRoot"
adb -s YOUR_SERIAL push .\build-vcard-reader\classes.dex /data/local/tmp/vcard-reader.dex
python phone_provider_export.py --adb adb --serial YOUR_SERIAL --label test-phone --output-dir .\providers-with-vcards --vcard-reader /data/local/tmp/vcard-reader.dex
```

## Plan and execute deletion

The default dry run checks the archive, manifest and requested device identity locally.

```powershell
python phone_delete_from_tar.py --adb adb --serial YOUR_SERIAL --label test-phone --archive .\backup\shared_storage.tar --report .\deletion-plan.json
```

Keep the verified backup, stop apps that write to shared storage and choose a new report path. Add `--execute --confirm-serial YOUR_SERIAL` to run the deletion plan.

The script hashes each file before removal and deletes matching regular files and empty directories listed in the archive.

```powershell
python phone_delete_from_tar.py --adb adb --serial YOUR_SERIAL --label test-phone --archive .\backup\shared_storage.tar --report .\deletion-result.json --execute --confirm-serial YOUR_SERIAL
```

## Optional legacy clicker

Build `uiauto_clicker/src/Clicker.java` with a JDK and Android SDK containing platform `android-34` and build tools `34.0.0`.

```powershell
$sdkRoot = Read-Host 'Path to the Android SDK'
.\Build-Clicker.ps1 -SdkRoot "$sdkRoot" -OutputDirectory .\clicker-build
```

Run the DEX `clicker.jar` with the legacy `uiautomator runtest` runner and permission to inject clicks.

Install Google's ADB, Android SDK and BusyBox separately.
