# Dealer Inventory Sync Agent

This Windows background agent watches a local folder for `.xlsx` and `.xls`
inventory files and uploads changed files to the dealer inventory API.

## Install on a dealer Windows computer

1. Download `DealerInventorySync-Setup.exe` from the latest GitHub release.
2. Run the installer. Windows may ask you to confirm the download.
3. Enter the four values supplied by DealerOps:
   - Dashboard upload URL
   - Dealer ID
   - Dealer API key
   - Inventory folder to watch
4. Finish installation.

The installer starts the agent immediately and registers it to start
automatically whenever that Windows user signs in. It runs without a console
window in the background.

The watched folder is created if it does not already exist. New, changed, or
moved `.xlsx` and `.xls` files are queued as soon as Windows reports them. The
agent waits until each file has finished writing before uploading it.

The executable, local configuration, rotating logs, and upload state are stored
under:

```text
%LOCALAPPDATA%\Programs\DealerInventorySync
```

The dealer API key stays on the dealer computer in `config.json`. Do not email
that file or publish it.

## Set up for local Python development

1. Install Python 3.11 or newer.
2. Open PowerShell in this folder.
3. Create and activate a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

4. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

5. Copy `config.example.json` to `config.json`.
6. Replace `api_url` with the published backend URL, and set `api_key` and
   `dealer_id` to the values returned when the dealer was provisioned.
7. Start the agent:

   ```powershell
   py dealer_sync_agent.py
   ```

`config.json` contains a credential and should remain on the dealer's PC. Do not
email it, commit it to source control, or include it inside the executable.

## Behavior

- Watches the configured folder for created, changed, and moved Excel files.
- Runs an immediate scan at startup, polls the authenticated dealer settings
  endpoint every five minutes, and automatically adopts the server-configured
  sync frequency. The local 15-minute value is used until settings are fetched.
- Waits for a file to stop changing before opening it, avoiding uploads while
  Excel or another export process is still writing.
- Records successful file versions in `dealer_sync_state.json`; unchanged files
  are not uploaded repeatedly.
- Uses the file's SHA-256 digest as an idempotency key, so a timeout followed by
  a retry cannot create duplicate backend uploads.
- Retries timeouts, connection errors, HTTP 408/425/429, and server errors using
  exponential backoff.
- On a normal Task Scheduler stop or Ctrl+C, stops accepting new events and
  finishes files already queued before exiting.
- Writes rotating local logs to `dealer_sync.log` by default. Five old 5 MB log
  files are retained.
- Ignores Excel's temporary lock files beginning with `~$`.

All paths in `config.json` may be absolute or relative to the script/executable.
Environment variables such as `%USERPROFILE%` are expanded.
Remote API URLs must use HTTPS; plain HTTP is only accepted for localhost
development.

## Package as a standalone Windows executable manually

Run this command from an activated environment on a Windows PC:

```powershell
pyinstaller --onefile --name DealerInventorySync dealer_sync_agent.py
```

The executable is created at `dist\DealerInventorySync.exe`. Copy it to its
permanent folder and place `config.json` beside it. The log and state files are
also written relative to the executable unless absolute paths are configured.

PyInstaller must be run on Windows to produce a Windows `.exe`.

## Manual auto-launch alternative with Windows Task Scheduler

1. Open **Task Scheduler** and choose **Create Task**.
2. On **General**, select **Run whether user is logged on or not** and
   **Do not start a new instance**.
3. Add an **At startup** or **At log on** trigger.
4. Add a **Start a program** action:
   - Program: the full path to `DealerInventorySync.exe`
   - Start in: the folder containing the executable and `config.json`
5. On **Settings**, enable **Restart the task if it fails** and choose an
   appropriate restart interval.

Stop the scheduled task before replacing the executable or editing files that
it currently has open.

## Automated GitHub release

The `.github/workflows/release.yml` workflow builds on a Windows GitHub runner.
Pushing a version tag such as `v1.0.0` creates a GitHub release and attaches
`DealerInventorySync-Setup.exe`. A manual workflow run builds the same installer
as a downloadable workflow artifact without publishing a release.