# Apple Stock Monitor
**For Human Reader(written by human too)**




**Set up / For Agents**
Check local Apple Store pickup for iPhone Duo and selected Mac Studio configurations. The dashboard runs in your browser at [localhost:8766](http://localhost:8766).

1. Install [Python 3.11 or later](https://www.python.org/downloads/) if it isn’t already installed. The `python3` that comes with macOS developer tools is too old. On a Mac, after installing from python.org, open **Applications → Python 3.x** and double-click **Install Certificates.command** once. Without this step, Python can’t make secure connections, and every stock check and email fails with a certificate error. Python from Homebrew doesn’t need this step.
2. Double-click **Open Stock Monitor.command** on Mac or **Open Stock Monitor.cmd** on Windows. The Windows launcher installs Python’s time zone data the first time it needs it. If you downloaded the project as a ZIP, macOS may block the launcher the first time. To allow it, open **System Settings → Privacy & Security** and click **Open Anyway**.
3. In the browser, choose a working folder or click **Skip and use default**. Add a monitoring list with at least one US ZIP code and one product configuration. Enter your SMTP sender settings or click **Skip email alerts**.

For personal Gmail, use `smtp.gmail.com`, port `587` (STARTTLS) or `465` (implicit TLS), your full Gmail address, and a [Google app password](https://myaccount.google.com/apppasswords). Set recipients for each list. Each product can use the list addresses or specify its own. Leave the applicable recipients blank to check stock without email.

The default check interval is 60 seconds. Set a separate interval in seconds or minutes in each list. Set an optional first check time using the computer’s local clock. When the monitor starts or resumes, the list waits for that time’s next occurrence and then follows its interval. Leave fields blank to use the defaults under **Settings → Lists & timing**. The monitor sends five emails per recipient per product and ZIP when it finds newly available pickup. Lists due together share a pickup request for each ZIP code and deduplicate recipients. A partial result means some store and product combinations have a known pickup status and others are unknown. This can occur for configure-to-order Mac Studio models. **Check log** shows each request and any errors. **Restock history** shows pickup windows and can download them as CSV.

Use **Default first check second of minute** to delay the first check when starting or resuming. Enter 0–59 or leave the field blank to check immediately. At a 60-second interval, an offset of 20 keeps checks near :20 each minute. Other intervals count from the first check.

Enable **Automatic recovery** on the overview to review the logs every 10 minutes. Three consecutive HTTP 541 errors for any monitored ZIP pause checks for 10 minutes. The dashboard shows the pause and resume events, which are also logged. After resuming, only fresh checks count toward the next pause. Click **Turn off automatic recovery** to stop the schedule. If checks were paused for recovery, use **Start checks** when you want to resume.

On macOS, turn on **iPhone Duo Mac alerts** in the overview. A new iPhone Duo restock at an alert store opens the Apple Shopping Bag in your default browser and shows the color and store in a macOS notification. **Your bag must be in your default browser.** Before monitoring, sign in to your Apple Account and add the products to your bag in that browser. Otherwise, the alert opens an empty bag. To change the default browser, open **System Settings → Desktop & Dock → Default web browser**. The first alert may ask you to allow notifications from Script Editor. Choose **Turn off Mac alerts** to disable both actions.

Keep the launcher window open while monitoring. You can close and reopen the browser tab. Closing the launcher window stops monitoring. A machine that goes to sleep or disconnects can miss stock changes.

Monitoring lists and dashboard preferences are saved locally in `dashboard_settings.json`. History and SMTP settings live in the selected working folder. To start with a different folder, pick it in **Settings**. Previous history stays in its original folder. The app password is saved locally in `email_settings.ini`. Keep that file and the history database out of Git. The `email_settings.example.ini` template contains blanks.
