# Telegram Post Scheduler

A production-ready, **owner-only** Telegram bot that schedules posts (photo / video / text) with an inline call-to-action button to a Telegram channel.
Runs entirely on **GitHub Actions** — no server, no database, no Docker.

- 🐍 Python 3.12
- 🤖 [python-telegram-bot](https://docs.python-telegram-bot.org) v21
- 💾 JSON storage (`posts.json`) committed back to the repo
- ⏱️ GitHub Actions cron (every minute)
- 🔐 Secrets via GitHub Actions Secrets

---

## Table of Contents

1. [Features](#features)
2. [Project Structure](#project-structure)
3. [BotFather Setup](#botfather-setup)
4. [Telegram Channel Setup](#telegram-channel-setup)
5. [GitHub Setup](#github-setup)
6. [GitHub Secrets](#github-secrets)
7. [Deployment](#deployment)
8. [Local Installation](#local-installation)
9. [Commands](#commands)
10. [Scheduling Wizard](#scheduling-wizard)
11. [How Scheduling Works](#how-scheduling-works)
12. [Troubleshooting](#troubleshooting)

---

## Features

- Owner-only access (all other users are silently ignored)
- Inline-keyboard driven scheduling wizard
- Multiple schedules, each with multiple posts
- Photo / Video / Text-only posts
- Inline CTA button per post (custom text + URL)
- HTML captions
- Interval unit: Seconds / Minutes / Hours / Days
- Start date + time in configurable timezone (default `Asia/Kolkata`)
- Pause / Resume / Delete schedules
- Fully driven by GitHub Actions cron — no hosting required
- Media never stored locally; only Telegram `file_id`s are saved

---

## Project Structure

```
telegram-post-scheduler/
├── bot.py                       # Bot + scheduler entry point
├── requirements.txt
├── config.json                  # Non-secret settings (timezone, limits…)
├── posts.json                   # JSON "database" (auto-updated)
├── README.md
└── .github/
    └── workflows/
        └── scheduler.yml        # Runs every minute
```

---

## BotFather Setup

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot` and follow the prompts.
3. Copy the **bot token** (looks like `123456:ABC-DEF...`). This is `BOT_TOKEN`.
4. Recommended: `/setprivacy` → **Disable** (so the bot sees all messages you send it).

To get your **OWNER_ID**:

- Message [@userinfobot](https://t.me/userinfobot) — it replies with your numeric Telegram ID.

---

## Telegram Channel Setup

1. Create a channel (or use an existing one).
2. Add your bot to the channel **as an administrator** with permission to **Post Messages**.
3. Get the channel ID:
   - **Public channel:** use `@yourchannelusername` as `CHANNEL_ID`.
   - **Private channel:** forward any message from the channel to [@userinfobot](https://t.me/userinfobot) and copy the numeric ID (starts with `-100…`).

---

## GitHub Setup

1. Create a **new GitHub repository** (public or private — this project ships no secrets in code).
2. Push all files from this project to the repository:

   ```bash
   git init
   git add .
   git commit -m "Initial commit: telegram-post-scheduler"
   git branch -M main
   git remote add origin https://github.com/<you>/telegram-post-scheduler.git
   git push -u origin main
   ```

3. Go to **Settings → Actions → General**:
   - **Workflow permissions** → select **“Read and write permissions”** (required so the workflow can commit `posts.json` back).
   - Save.

---

## GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

|
 Name         
|
 Value                                                                    
|
|
------------
|
------------------------------------------------------------------------
|
|
`BOT_TOKEN`
|
 The bot token from BotFather                                             
|
|
`OWNER_ID`
|
 Your numeric Telegram user ID                                            
|
|
`CHANNEL_ID`
|
`@yourchannel`
 (public) 
**
or
**
`-100xxxxxxxxxx`
 (private) target channel 
|

Never commit these values to the repo.

---

## Deployment

Once the secrets are set and the repo is pushed:

1. Go to **Actions** tab.
2. Enable workflows if prompted.
3. The **Telegram Post Scheduler** workflow will run automatically every minute.
4. To trigger manually, click the workflow → **Run workflow**.

> ⚠️ GitHub cron granularity is **1 minute** but GitHub does not guarantee exact firing — actual runs may drift by a few minutes under load. Do not rely on second-level accuracy.

---

## Local Installation

You can run the interactive bot locally to schedule posts (the scheduler itself still runs from GitHub Actions).

```bash
git clone https://github.com//telegram-post-scheduler.git
cd telegram-post-scheduler

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export BOT_TOKEN="123456:ABC..."
export OWNER_ID="123456789"
export CHANNEL_ID="@yourchannel"

# Interactive bot (long polling) — use for the wizard:
python bot.py

# Run scheduler once (what GitHub Actions calls):
python bot.py --scheduler
```

After scheduling posts locally, **commit and push `posts.json`** so GitHub Actions can see them:

```bash
git add posts.json
git commit -m "Add new schedule"
git push
```

---

## Commands

All commands are **owner-only**. Other users are ignored.

|
 Command                      
|
 Description                                     
|
|
----------------------------
|
-----------------------------------------------
|
|
`/start`
|
 Welcome screen with inline menu                 
|
|
`/help`
|
 List all commands                               
|
|
`/schedule`
|
 Start the scheduling wizard                     
|
|
`/list`
|
 List all schedules                              
|
|
`/delete <schedule_id>`
|
 Delete a schedule (prefix of 8+ chars is fine)  
|
|
`/edit`
|
 Editing helper (currently: delete + recreate)   
|
|
`/pause <schedule_id>`
|
 Pause all pending posts in a schedule           
|
|
`/resume <schedule_id>`
|
 Resume a paused schedule                        
|
|
`/status`
|
 Bot status, timezone, pending/sent counts       
|
|
`/cancel`
|
 Cancel the scheduling wizard                    
|

---

## Scheduling Wizard

`/schedule` guides you through:

1. **How many posts?** — 1, 2, 3, 5, 10, or custom.
2. **For each post:**
   - Step 1: Send an image, a video, or `/skip` for text-only.
   - Step 2: Product URL (`https://…`).
   - Step 3: Inline button text (e.g. `Buy Now`, `Order`, `View Product`).
   - Step 4: Caption (HTML supported: `<b>`, `<i>`, `<a href="...">`, `<code>`, …).
3. **Interval unit:** Seconds / Minutes / Hours / Days.
4. **Interval value:** e.g. `30`, `60`, `120`.
5. **Start Date:** `DD-MM-YYYY`.
6. **Start Time:** `HH:MM` (24-hour, timezone `Asia/Kolkata`).
7. **Confirmation:** Schedule / Cancel.

Post `n` is published at `start_time + n × interval`.

---

## How Scheduling Works

- `posts.json` stores an array of schedules. Each schedule holds an ordered list of posts, a start time, and an interval.
- Every minute, GitHub Actions runs `python bot.py --scheduler`, which:
  1. Loads `posts.json`.
  2. For each active schedule, checks which pending posts are due.
  3. Sends each due post to the channel with its inline button.
  4. Marks it `sent` and writes back `posts.json`.
  5. Commits `posts.json` back to the repository so the next run has fresh state.
- Media files themselves are **never** stored — only Telegram `file_id`s are persisted.

Post statuses: `pending` · `sent` · `paused` · `cancelled`.

---

## Troubleshooting

**The workflow runs but nothing posts to the channel.**
- Confirm the bot is a channel **administrator** with **Post Messages** permission.
- Confirm `CHANNEL_ID` is either `@publicname` or a `-100…` numeric ID.
- Check the Actions run log for `Telegram send failed: …`.

**`Unauthorized` errors in logs.**
- `BOT_TOKEN` is wrong or was regenerated. Update the GitHub Secret.

**The workflow cannot push `posts.json`.**
- Go to **Settings → Actions → General → Workflow permissions** and enable **Read and write permissions**.

**Posts fire late.**
- GitHub Actions cron is best-effort; expect drift of a few minutes, especially on the free plan.

**`posts.json` looks corrupted.**
- On corruption the bot automatically renames it to `posts.json.corrupt.<timestamp>` and starts fresh. Restore from git history if needed.

**Bot ignores me in chat.**
- You are not the owner. Set the `OWNER_ID` secret to your numeric Telegram ID (get it from [@userinfobot](https://t.me/userinfobot)).

**Media does not appear after re-scheduling old posts.**
- `file_id`s can expire or become bot-specific. Re-upload the media through the wizard.

---

## License

MIT — do whatever you want, no warranty.