# Zion Skank Shorts Automation Pipeline

A Python script that takes a long-form music video (MP4), extracts the most energetic 11-second clip, crops it to vertical 9:16 format, generates a title and description, and uploads it to YouTube as a Short — all in one command.

---

## Before You Write Any Code: Google Cloud Setup

You need to complete these one-time steps to get permission to upload videos to YouTube via the API. This is a manual browser-based process — you cannot skip it.

---

### Step 1 — Create a Google Cloud Project

1. Go to [https://console.cloud.google.com](https://console.cloud.google.com)
2. Sign in with the Google account that owns (or has manager access to) the **Zion Skank Shorts** YouTube channel
3. At the top of the page, click the project dropdown (it may say "Select a project")
4. Click **New Project**
5. Give it a name like `zion-shorts-pipeline`
6. Click **Create**
7. Wait a moment, then make sure your new project is selected in the dropdown at the top

---

### Step 2 — Enable the YouTube Data API v3

1. In the left sidebar, go to **APIs & Services → Library**
2. Search for `YouTube Data API v3`
3. Click on it, then click **Enable**
4. Wait for it to activate — you'll land on the API overview page when it's done

---

### Step 3 — Configure the OAuth Consent Screen

This tells Google what your app is and who can use it. Because this is a personal tool (not a published app), you'll use "External" type but keep it in testing mode.

1. Go to **APIs & Services → OAuth consent screen**
2. Choose **External** and click **Create**
3. Fill in the required fields:
   - **App name**: `Zion Shorts Pipeline` (or anything you like)
   - **User support email**: your email address
   - **Developer contact information**: your email address
4. Click **Save and Continue** through the Scopes screen (no changes needed there)
5. On the **Test users** screen, click **Add Users** and add the Google account email that owns the YouTube channel
6. Click **Save and Continue**, then **Back to Dashboard**

> **Why test mode?** Google requires a lengthy verification process for apps that go to production. Since this is a personal automation tool, staying in test mode is fine. Test mode allows up to 100 users and tokens that last 7 days before requiring re-auth.

---

### Step 4 — Create OAuth 2.0 Credentials

1. Go to **APIs & Services → Credentials**
2. Click **+ Create Credentials → OAuth client ID**
3. For **Application type**, choose **Desktop app**
4. Give it a name like `Zion Shorts Desktop Client`
5. Click **Create**
6. A dialog will appear with your Client ID and Client Secret — click **Download JSON**
7. Rename the downloaded file to exactly: `client_secrets.json`
8. Move it into the root of this project folder (same folder as `shorts_auto.py` when that exists)

> **Keep this file private.** Never commit `client_secrets.json` to Git. It is listed in `.gitignore`.

---

### Step 5 — Install ffmpeg

ffmpeg is the tool that does the actual video cutting and cropping. Install it via Homebrew (if you don't have Homebrew itself, install it first from [https://brew.sh](https://brew.sh)).

Open your Terminal and run:

```bash
brew install ffmpeg
```

This will take a few minutes. When it's done, verify it worked:

```bash
ffmpeg -version
```

You should see a version number printed. If you see `command not found`, something went wrong — let me know.

---

### Step 6 — First-Time Authentication (Run Once)

When you run the script for the first time, it will:

1. Open a browser window asking you to sign in to Google
2. Ask you to grant the app permission to manage your YouTube account
3. After you approve, save a file called `token.json` in the project folder

From that point on, the script refreshes the token automatically and you won't need to go through the browser again (unless you delete `token.json` or revoke access).

> If you see a warning that says **"Google hasn't verified this app"**, click **Advanced → Go to Zion Shorts Pipeline (unsafe)**. This is expected for apps in test mode.

---

## Project Structure (once code is written)

```
zion-shorts-pipeline/
├── shorts_auto.py        # Main script — the one you run
├── client_secrets.json   # Your OAuth credentials (never commit this)
├── token.json            # Auto-generated after first auth (never commit this)
├── requirements.txt      # Python dependencies
├── .gitignore
└── README.md
```

---

## How to Run (once set up)

```bash
python shorts_auto.py --input "my_video.mp4" --title "Original Video Title"
```

---

## What the Script Does

1. **Analyzes** the audio of your input MP4 to find the 11-second window with the highest energy
2. **Extracts** that clip and converts it to vertical 9:16 format with a blurred background fill
3. **Generates** a YouTube Shorts-appropriate title and description based on your input title
4. **Uploads** the clip to the Zion Skank Shorts YouTube channel with category set to Music

---

## Dependencies (installed via pip)

- `google-api-python-client` — YouTube Data API
- `google-auth-oauthlib` — OAuth 2.0 authentication
- `librosa` — audio analysis
- `pydub` — audio processing
- `ffmpeg-python` — video processing wrapper
**System requirement:** `ffmpeg` must be installed — see Step 5 above.
