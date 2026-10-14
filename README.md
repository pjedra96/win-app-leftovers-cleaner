# Windows App Leftovers Cleaner

A small, very basic Windows 11 GUI tool for removing leftovers that apps keep after updating.

It was made for apps that update with the **Squirrel** updater (GitHub Desktop,
MongoDB Compass, Discord, Slack, ...). They keep every old version in
`%LOCALAPPDATA%\<App>\app-<version>`. The tool keeps the current version and
offers the older ones for deletion to save some space on a computer - or just to work as a form or "version control".

Also added:

- **VS Code** – old build caches in `%APPDATA%\Code\CachedData` not used since before yesterday
- **VS Code** – spare extension packages and unfinished downloads in
  `%APPDATA%\Code\CachedExtensionVSIXs` from before yesterday (VS Code must be closed)
- **VS Code** – crash dumps in `%APPDATA%\Code\Crashpad\reports`, listed **unticked**
  since they're only useful for investigating past crashes: your call
- **npm cache** – cleared with `npm cache clean --force`
- **pip cache** – cleared with `pip cache purge`

## Usage

Requires Python 3.12+ (standard library only).

```
python app_leftovers_cleaner.py
```

It scans first, then lists what can be removed. Untick anything you want to keep and
press **Delete selected**. Deleting is permanent (no Recycle Bin), and current
versions are never offered.
