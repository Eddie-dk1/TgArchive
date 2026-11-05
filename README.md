# Telegram Archive

A local macOS app for archiving Telegram channels, groups, and supported Mini Apps. Save messages, lesson texts, photos, videos, and other attachments to a folder of your choice, then browse the archive offline.

Built with Python, Tkinter, Telethon, HTTPX, and SQLite. The interface is currently in Russian and uses a compact, dark blue theme. No bot, Docker, or external server is required.

## Features

- Choose public or private channels and groups from your account.
- Connect using an existing Telegram Desktop account, or sign in separately with a QR code or phone number.
- Choose an output folder before every export.
- Collect message history and save a durable media queue **before downloading attachments**.
- Stop and resume interrupted exports, including partially downloaded files.
- Run a full history check to collect new posts and update existing messages.
- Track messages, files, errors, transferred bytes, speed, and estimated time remaining.
- Detect changed or corrupted local files with SHA-256 and restore them on resume.
- View unfinished attachments in the app and retry failures without scanning history again.
- Build a standalone macOS `.app` with an icon; CI produces an Apple Silicon artifact.
- Browse saved messages in HTML; use JSONL, CSV, and SQLite for further processing.
- Inspect persistent run logs, failed downloads, and gaps in message IDs.
- Archive your accessible Edify courses, preserving course/module/lesson structure, text, photos, direct video files, presentations, and lesson attachments.
- Refresh the Mini App catalogue to collect newly unlocked sections without downloading unchanged files again.

## Edify Mini App archives

Mini App observations and download results are also kept in a persistent history,
independent of export folders: `.private/history/` for source runs, or
`~/Library/Application Support/Telegram Archive/history/` for the bundled app
(the data-directory override applies to both). `miniapps.sqlite3` is authoritative;
`events.jsonl` is a readable event export. Source and bundled profiles are separate.
Keep this history when removing downloaded materials; delete it only if explicitly
requested. It records account/source scope, lesson IDs and titles, attachment slots,
file identities, sizes, timestamps, status and SHA-256, without tokens, signed URLs,
or lesson bodies. A changed source identity is distinguished from a new lesson or
new attachment. Where both hashes exist, the journal records identical or different
file bytes; it cannot determine whether exercises or scenes changed semantically.

1. Connect the Telegram account that has access to [Edify](https://t.me/edify_platform_bot?profile). With several Desktop accounts, use **Подключить Telegram Desktop** to select the right one.
2. Select **Мини-приложения** (Mini Apps) in the source selector above the list.
3. Select **Edify · весь каталог** and click **Обновить и докачать** (Refresh and resume).
4. Choose a destination folder. On later runs, choose the same parent folder to update that archive.
5. Open **Открыть архив** (Open archive) to browse the offline catalogue. **Проблемные файлы** shows catalogue warnings and unfinished materials.

The archive is stored under `miniapp_edify_<account-id>_<bot-id>/`. It contains a separate `catalogue.sqlite3`, `index.html`, `report.json`, readable lesson folders, `lesson.md` text files, and local media. Stable IDs keep renamed lessons from creating duplicate folders. Replaced source files get separate revision paths; previously saved material is retained when access or the remote catalogue changes.

Each run reads the accessible catalogue before downloading files. Sections with no currently accessible lessons remain visible as **Материалы пока недоступны** (Materials not available yet). Run **Обновить и докачать** again when they open. A successful export means **all materials accessible now**, not the eventual contents of sections that will open later. Incomplete API responses and unsupported media are shown as limitations, rather than silently treated as success.

Supported lesson media: Telegram-hosted MP4, uploaded direct files, slider images, Markdown images with stable Edify storage keys or unsigned public HTTPS URLs, PDF/PPTX presentations, and lesson material attachments. Text updates are reflected in the next offline export. External YouTube/Rutube/Kinescope playback, HLS/DASH, podcasts, tests/attestations, homework, and other Mini App providers are not implemented. Unsupported entries prevent a clean completion result and appear in the report. Browser screenshots and protected streaming playback are not used as substitutes for original files.

Files download sequentially with bounded memory. Telegram videos with a stable file identity and known size support byte-range resume across restarts; non-versioned image/storage-key downloads restart safely if interrupted. SHA-256 detects changed local files. A server that ignores Range causes a clean restart, and invalid range responses are never appended. The app can renew expired signed links. Edify access tokens and signed media URLs stay in memory and are excluded from archive metadata and error messages.

The Mini App integration uses Telegram's existing account session and read-only student endpoints. It does not mark lessons complete, update watch progress, submit homework, or log out of Telegram Desktop. Existing SOCKS5 settings apply to both Telegram and Edify HTTP requests. Only materials already available to the connected account are collected.

## Requirements

- macOS with Python **3.11 or newer**, including Tkinter. The installer from [python.org](https://www.python.org/downloads/macos/) includes Tkinter.
- A Telegram account with access to the channel or group you want to archive.
- An internet connection and enough free disk space for the export.

The development environment was tested on Apple Silicon with Python 3.14.3 and Tk 8.6. Other environments may need additional dependency setup. Windows and Linux are not currently supported by the launcher or Desktop session discovery.

## Standalone macOS app

The [GitHub Actions workflow](https://github.com/Eddie-dk1/TgArchive/actions/workflows/macos.yml) tests and builds the app for **Apple Silicon (`arm64`)**. Open a successful workflow run and download the Apple Silicon artifact. Extract the artifact, then its inner ZIP, and move `Telegram Archive.app` to Applications or another writable location. Python does not need to be installed to run the bundle.

The app is ad-hoc signed, not Apple-notarized. macOS may require explicit approval to open it. Developer ID signing and notarization require Apple developer credentials and are not configured in this repository.

The bundle stores its account data in `~/Library/Application Support/Telegram Archive/`, outside the signed application. Source runs keep using `.private/` in the project. These are separate local profiles: connect once on the first `.app` launch; your existing source profile and Telegram Desktop are left in place. Export folders remain wherever you selected them.

## Run from source

```sh
git clone https://github.com/Eddie-dk1/TgArchive.git
cd TgArchive
./archive.command
```

You can also double-click `archive.command` in Finder. On its first run, the launcher creates a local `.venv` and installs the dependencies from `requirements.txt`.

If you prefer to set up the environment yourself:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m telegram_archive
```

`requirements-lock-macos.txt` records the exact versions used in the tested macOS environment, including optional accelerators. `requirements.txt` is the baseline installation.

## Connect your account

### Existing Telegram Desktop account

Click **Подключить Telegram Desktop** (Connect Telegram Desktop). The app looks for a local `tdata` directory; you can select it manually if needed. No phone number, API ID, or API hash is required for this method.

If Desktop has several accounts, choose one in the account dialog. If it has a local passcode, enter it when prompted. The current import library supports ASCII passcodes; use QR sign-in if your passcode is incompatible.

Importing uses opentele2's `UseCurrentSession` mode. It reads the existing Desktop authorization and stores a local copy for Archive. **The source `tdata` files are not modified.** This applies to Telegram Desktop, not the separate native Swift Telegram app for macOS.

**The imported authorization is shared with Desktop.** Revoking that session through Telegram's Devices settings also disconnects Desktop. Closing Archive does not revoke anything. The app's **Выйти из Archive** (Sign out of Archive) action removes Archive's local authorization files without calling Telegram's server-side logout, so Desktop remains signed in.

### Independent QR or phone sign-in

1. Open **Подключение** (Connection).
2. Choose QR code or phone sign-in.
3. Enter your own API ID and API hash from [my.telegram.org/apps](https://my.telegram.org/apps).
4. Click **Подключить** (Connect).
5. For QR sign-in, open Telegram on your phone → Settings → Devices → Link Desktop Device and scan the code. For phone sign-in, enter the login code when asked.

Two-step verification passwords are requested when needed. QR codes refresh automatically when they expire. The session and connection settings are saved locally, so subsequent launches do not require signing in again unless the session has been revoked.

This method creates an authorization separate from Telegram Desktop.

## Export a channel

1. Connect and wait for the channel list.
2. Select a channel or group. Search works by name or chat ID.
3. Choose an export mode.
4. Click **Выбрать папку и начать** (Choose folder and start).
5. Select the parent folder where the channel archive should be stored.

The app creates a `channel_<chat_id>` subfolder. Renaming a Telegram channel does not create another archive.

| Mode | Behavior |
| --- | --- |
| **Продолжить загрузку** — Resume | For a new archive, collects all accessible history. For an existing archive, resumes an interrupted history scan or the saved download queue, checks existing files, and retries pending or failed downloads. Does not look for new posts after the saved history scan is complete. |
| **Полная сверка** — Full check | Scans the history again, collects new posts, updates message content, checks previously saved posts, and refreshes the download queue. Reuses files whose expected size matches. |

To resume an export, choose the same channel and the **same parent folder** as before. For example, if the archive is `Downloads/Telegram/channel_<chat_id>`, select `Downloads/Telegram`, not the channel subfolder.

### Download order and stopping

A scan captures an upper message ID, then collects accessible posts **from oldest to newest**. Messages created after that boundary are included in the next full check. The oldest accessible post may differ from the channel's first post if older history is hidden from your account.

After collecting history, the app writes HTML, JSONL, and CSV exports, checks message-ID gaps and local files, then downloads the media queue. Photos use the largest available size; videos and documents are saved without transcoding.

**Остановить** (Stop) preserves the queue and export state. Partial files resume at a 512 KiB block boundary; an incomplete final block may be downloaded again. Telegram rate-limit waits are displayed and can be cancelled. Ordinary download failures are retried up to three times per run.

Closing the window during an active export requests a stop and saves the archive before exiting.

## Archive contents

```text
channel_<chat_id>/
├── index.html          Offline message viewer
├── page_2.html         Additional pages for large archives
├── messages.jsonl      Messages, metadata, and raw Telegram structures
├── messages.csv        Spreadsheet-friendly message export
├── report.html         Run summary, errors, and recent events
├── journal.jsonl       Persistent run and retry history
├── errors.csv          Attachments that could not be saved
├── gaps.csv            Checked and unverified message-ID gaps
├── channel.json        Snapshot boundary, queue state, and counters
├── archive.sqlite3     Durable state for resume and updates
├── SHA256SUMS          Checksums of completed local files
└── media/
    ├── photos/
    ├── videos/
    ├── audio/
    ├── documents/
    └── stickers_gifs/
```

The HTML viewer shows plain message text and attachments, with up to 500 messages per page. Additional Telegram fields, such as album grouping, entities, and reply metadata, remain available in JSONL and SQLite. CSV exports escape potential spreadsheet formulas; JSONL and SQLite preserve the original text.

Use **Открыть архив** (Open archive) to view saved posts, **Проблемные файлы** (Problem files) for unfinished attachments, or **Журнал** (Log) for application events.

### Inspect and retry problem files

Open **Проблемные файлы** after an export, or choose an existing `channel_<chat_id>` folder after restarting the app. The paginated list shows the post ID, current state, total attempts, and failure reason. Select a row to read its reason and open the original Telegram post; the HTML report remains available in that window.

**Повторить проблемные** (Retry problem files) retries failed or previously unavailable attachments only. It does not rescan history, revalidate successful files, or process ordinary pending files. Previously unavailable posts are requested again, but Telegram may still deny access. Use Resume to process the remaining regular queue. Retry checks archive ownership and refuses to process another account's archive.

### File integrity

New downloads receive a SHA-256 digest before being marked complete. Resume and Full check recalculate digests of completed local files; missing, size-mismatched, or hash-mismatched files are queued for repair. Hashing uses bounded memory and can be stopped.

Digests and verification timestamps are stored in SQLite, JSONL, and CSV. The archive also includes `SHA256SUMS` for independent checks:

```sh
cd /path/to/channel_archive
shasum -a 256 -c SHA256SUMS
```

Existing archives without digests receive an initial baseline at their next Resume or Full check, without redownloading unchanged files. This first baseline cannot establish whether a file was already damaged. Hashes detect subsequent local changes; they are not a comparison against an independently supplied Telegram checksum.

## Local account data

Source runs store connection settings and authorization files in `.private/` beside the source. The standalone app uses `~/Library/Application Support/Telegram Archive/`. Directory permissions are set to `700` and files to `600`. API hashes and proxy passwords are stored locally in the settings file; login codes, two-step verification passwords, and Desktop passcodes are not saved by the app.

Telegram session files grant account access. Keep `.private/` private. The repository excludes it, virtual environments, downloaded archives, databases, logs, and local development notes through `.gitignore`.

Exports stay in the folder you choose. If that folder is synced with iCloud or another service, its existing sync settings apply.

## Limitations

- Only history and files accessible to the signed-in account can be archived. Previously deleted messages, hidden history, and inaccessible attachments cannot be recovered.
- Channels with Telegram's content-protection flag are not automatically excluded. The app uses the API rather than Desktop's export or forwarding features; actual file availability depends on Telegram's server-side permissions. Failed attachments are recorded instead of being silently treated as downloaded.
- Gaps in message IDs do not necessarily mean lost messages. Up to 1,000 missing IDs are checked per scan; other gaps remain explicitly unverified.
- Comments in a linked discussion group are a separate chat. Select that group separately if your account has access.
- Albums retain their message records and shared `grouped_id`; the HTML viewer does not combine them into an album layout.
- SHA-256 detects changes against the locally recorded baseline; it cannot prove that a legacy file was intact before its first checksum was created.
- Browser video playback depends on codec support. Open the saved file in a compatible player when necessary.
- SOCKS5 proxies are supported in Connection settings. Telegram MTProxy links are not supported.

## Development and tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

The automated suite covers archive collection, resume, cancellation, retries, migration, gap handling, progress events, QR authentication, local-only sign-out, and Desktop session conversion. Mini App fixtures cover API contracts, multiple attachments, future section unlocks, photo/text export, byte-range validation, capped range responses, ignored ranges, expired links, disk space, local corruption, account ownership, and secret-free reports. Tests use synthetic data and mocked Telegram/HTTP clients; they do not require a real account or network access.

`tools/check_edify.py` is a separate, explicit **live** acceptance tool. It reads the active Telegram Desktop authorization in memory, disconnects without logging out, and never prints tokens or signed URLs. Its `--account N` option selects a 1-based Desktop account index; otherwise it uses the active Desktop account. Choose an ignored output directory:

```sh
.venv/bin/python tools/check_edify.py --output outputs/edify-check --inspect
.venv/bin/python tools/check_edify.py --output outputs/edify-check --stop-after-mib 8
.venv/bin/python tools/check_edify.py --output outputs/edify-check
.venv/bin/python tools/check_edify.py --output outputs/edify-check --compare-first
```

The last command independently reads the first video and compares its SHA-256 with the finished archive. This consumes additional network traffic. Do not commit downloaded courses, session data, or live test outputs.

Native interface checks are opt-in and require a macOS desktop session:

```sh
TG_ARCHIVE_GUI_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_ui.py -v
```

Project modules:

- `gui.py`, `ui.py`: native interface, styles, and progress display.
- `auth.py`, `desktop.py`: QR/phone authentication and local Desktop session import.
- `core.py`: history collection, media queue, retries, and gap verification.
- `storage.py`, `common.py`: SQLite state, exports, and shared helpers.
- `integrity.py`, `issues.py`: cooperative hashing and read-only archive inspection.
- `paths.py`: writable profile location for source and bundled execution.

## Build the macOS app

Use a macOS Python installation with bundled Tcl/Tk (the python.org installer is used by CI). Build on an Apple Silicon Mac (`arm64`):

```sh
.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python tools/build_macos.py
.venv/bin/python tools/smoke_macos.py
```

Outputs are `dist/Telegram Archive.app`, an Apple Silicon ZIP, and its checksum. The build explicitly collects application dependencies and the license, excludes account data, verifies the bundle signature, and checks for unexpected runtime files before creating the ZIP. The smoke check creates a temporary profile, imports Desktop and SOCKS5 support, opens the UI, renders a QR code, and exercises the problem-file window without network access or a real account.

The workflow runs on pushes to `main`, pull requests, and manual dispatch. The Apple Silicon job runs engine and native UI tests, builds the app, executes the bundle smoke check, and uploads the ZIP with its checksum. Artifacts are retained for 30 days. Build products, local smoke data, and account profiles are not committed.

## Credits and license

Originally adapted from [zsv555/telegram-media-downloader](https://github.com/zsv555/telegram-media-downloader). The interface and archive engine were substantially reworked around SQLite, a separate history-collection phase, and a persistent media queue. The upstream MIT license notice is preserved in [LICENSE](LICENSE).

Built with [Telethon](https://docs.telethon.dev/), [opentele2](https://opentele2.github.io/), and Python's standard Tkinter and SQLite libraries.
