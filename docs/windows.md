# Windows

[Back to the README](../README.md)

The Windows executable does not require Python, Git, or Command Prompt.

It includes every feature except the optional [Apprise](notifications.md#apprise) notifier.
All the other notification providers work. To use Apprise on Windows, run the
miner from a [source checkout](../README.md#source-checkout-quick-start) or Docker.

## Standard vs. portable

Two distributions are published on every release, identical in features and
differing only in where they keep configuration, Twitch login cookies,
analytics, and logs:

- **Standard** (`TwitchChannelPointsMiner-<version>-Setup.exe`, recommended):
  an installer that stores your data in your Windows user profile
  (`%LOCALAPPDATA%\TwitchChannelPointsMiner`), separate from the installed
  program. Reinstalling, updating, or uninstalling the app never touches it.
- **Portable** (`TwitchChannelPointsMiner-Portable-<version>.zip`): no
  installation - keeps everything beside the executable, so the whole folder
  can be moved, copied, or run from removable media. Keep its folder private,
  since it contains your Twitch login and saved session.

If you already used an older release (which always behaved like the portable
build, even when installed) and switch to the Standard installer, the app
detects your existing configuration, cookies, and analytics beside the old
executable on its first run, moves them into the new location automatically,
and shows a one-time confirmation dialog. Your original files are archived
untouched in a `config-legacy` folder next to where the old executable was,
so nothing is lost; this only happens once.

## Install the Windows executable

1. Open the project's
   [Releases page](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/releases)
   and select the newest release.
2. Under **Assets**, download `TwitchChannelPointsMiner-<version>-Setup.exe`
   for the Standard installer (recommended), or
   `TwitchChannelPointsMiner-Portable-<version>.zip` for the portable build.
   Do not download **Source code**.
   - **Installer:** run the downloaded `.exe` and follow its prompts.
   - **Portable:** open your **Downloads** folder, right-click the ZIP file,
     select **Extract All...**, choose a permanent location you can easily
     find (such as `Documents\TwitchChannelPointsMiner`), and select
     **Extract**. Do not run the program from inside the ZIP file.
3. Launch the app (the installer offers to do this automatically; for the
   portable build, open the extracted folder and double-click
   `TwitchChannelPointsMiner-Portable.exe`). The first run creates the
   configuration file and opens a desktop window directly on the **Config**
   tab, ready for you to fill in.

Only download the executable from the project's official Releases page. If
Microsoft Defender SmartScreen appears, check that the publisher warning names
the file you downloaded and that you used the link above. Then select
**More info** followed by **Run anyway**. If the file came from anywhere else,
delete it instead.

## Set up your account

The desktop window has two tabs: **Dashboard** (the analytics dashboard,
embedded) and **Console** (the miner's live log output). On first run it
opens straight to the Dashboard tab's **Config** view.

1. If the window asks for a username and password, use your Twitch username
   and the password printed on the **Console** tab (also saved in
   `config\config.py`, under `ANALYTICS_CONFIG`).
2. Under **Configured streamers**, add the channels you want to watch.
3. Review the other Config tab sections - categories, sources, logging, and
   notifications. The [configuration guide](../README.md#configuration-file) explains
   every section in more detail; anything not covered by the Config tab can
   still be edited directly in `config\config.py` with Notepad.

Changes made in the Config tab are written to `config\config.py`
automatically; streamer and category changes take effect immediately, and
other settings after a restart. You can return to this tab any time to enable
followed channels, Drops categories, predictions, notifications, and other
settings.

## Start the miner

The miner starts automatically as soon as the desktop window opens - there is
no separate step. Watch its progress on the **Dashboard** tab. If Twitch sign-in
is needed, a window pops up with the activation link and code to enter (also
available, along with startup errors if the dashboard itself fails to load, on
the **Console** tab). Use **Open in browser** on either tab to view the
dashboard in your default browser instead.

Closing the window stops the miner. Where its settings, login session, and
logs are saved depends on which distribution you installed (see
[Standard vs. portable](#standard-vs-portable) above):

- **Standard:** in your Windows user profile
  (`%LOCALAPPDATA%\TwitchChannelPointsMiner`), independent of the installed
  program's location.
- **Portable:** in folders beside the executable. Do not move the executable
  by itself after setup; move the entire `TwitchChannelPointsMiner` folder if
  you want it in a different location.

## Update the miner

- **Standard:** download and run the newest `-Setup.exe` from the
  [Releases page](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/releases)
  and follow its prompts; it updates the installed program in place. Your
  configuration and login data live outside the install directory and are
  never affected.
- **Portable:**
  1. Stop the running miner.
  2. Download and extract the newest portable ZIP as described above.
  3. Copy the new `TwitchChannelPointsMiner-Portable.exe` into your existing
     miner folder.
  4. When Windows asks, choose **Replace the file in the destination**.
  5. Start the miner normally. Your existing configuration and login data
     remain in place.

## Windows troubleshooting

- **The desktop window never appears, or a console window flashes and
  closes:** The executable needs the Microsoft Edge WebView2 runtime, which
  ships with Windows 11 and most up-to-date Windows 10 installs. If it's
  missing, install the
  [Evergreen WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
  and run the executable again.
- **The Dashboard tab shows "The dashboard isn't available yet":** Check the
  Console tab for the reason. Analytics may still be starting up, or
  `enable_analytics` may be set to `False` in `config\config.py`.
- **Windows says it cannot find the configuration (portable build only):**
  Right-click the downloaded ZIP and use **Extract All...** before running the
  executable.
- **"Notifications to apprise are failing: The Windows executable does not
  include Apprise":** The executable cannot send through Apprise. Remove
  `apprise=Apprise(...)` from `MINER_CONFIG["logger_settings"]` and use a
  built-in provider such as Discord, Telegram or ntfy, or see
  [Apprise](notifications.md#apprise) for the setups that include it.
- **Emoji or symbols look broken:** In `config.py`, use
  `LoggerSettings(emoji=False)` in `MINER_CONFIG["logger_settings"]`. See
  [LoggerSettings](settings.md#loggersettings) for the complete example.
- **The miner closes or reports an error after setup:** Check the Console tab,
  or use the desktop window's **Open logs folder** button (or open
  `%LOCALAPPDATA%\TwitchChannelPointsMiner\logs` for a Standard install, or
  the `logs` folder beside the executable for a portable one) and check the
  newest log file. Remove account names, cookies, tokens, and passwords
  before sharing a log in a bug report.

Developers who want to create the executable from source can use the
[Windows build guide](../BUILD.md#windows-executable).
