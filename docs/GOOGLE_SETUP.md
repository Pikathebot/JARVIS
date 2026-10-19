# Connecting Jarvis to Google (PLAN 4.16 phase B)

Jarvis talks to Google Calendar and Google Tasks with a key that belongs to **you**: a small Google
Cloud project of your own. Nothing goes through anyone else's server. You do this once, in your
browser (about 10 minutes); Jarvis never sees your Google password.

## 1. Create the project
1. Open <https://console.cloud.google.com/> and sign in with the Google account whose calendar
   and tasks Jarvis should use.
2. Top bar -> project picker -> **New project**. Name it `Jarvis`, no organisation -> **Create**.
   Make sure `Jarvis` is the selected project afterwards.

## 2. Turn on the two APIs
**APIs & Services -> Library**, search and **Enable** each:
- **Google Calendar API**
- **Google Tasks API**

## 3. The sign-in screen (OAuth consent)
**APIs & Services -> OAuth consent screen** (it may be called **Google Auth Platform**):
1. **Get started**: app name `Jarvis`, user support email = your address.
2. Audience: **External**. Contact email = your address. Agree and **Create**.
3. **Audience** page -> **Test users** -> **Add users** -> add your own Gmail address.
4. Recommended: on the same **Audience** page, click **Publish app** (status "In production").
   In "Testing" status Google makes the sign-in expire every 7 days. Published but unverified is
   fine for one person: at sign-in Google shows "Google hasn't verified this app" -- click
   **Advanced -> Go to Jarvis (unsafe)**. It's your own app, so that warning is about you.

## 4. The key Jarvis uses
**Clients** (or **APIs & Services -> Credentials -> Create credentials -> OAuth client ID**):
1. Application type: **Desktop app**. Name: `Jarvis desktop`. **Create**.
2. **Download JSON** in the dialog that appears.
3. Save it as **`D:\JARVIS\data\google_client_secret.json`**. (`data\` is never committed to git.)

## 5. Sign in
Once Jarvis has the sign-in code, tell Claude the file is in place. Signing in opens your browser
once; you pick the account and allow **Calendar events** and **Tasks**. Jarvis keeps the sign-in on
this PC only, encrypted with your Windows account (DPAPI). Signing out deletes it and revokes it
at Google.

Scopes asked for: `calendar.events` (read/add/change events) and `tasks`. Gmail comes later
(phase D), read + draft only, never send.
