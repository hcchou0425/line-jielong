# 🚀 RBOT Deployment Guide - Render
# 部署指南 - 使用 Render 平台

完整的逐步部署教學，從零開始到上線！
Complete step-by-step deployment guide from scratch to live!

---

## 📋 Prerequisites (準備工作)

### ✅ What You Have:
- ✅ RBOT code (app.py and all files)
- ✅ LINE Bot created (Channel Access Token & Secret)
- ✅ All packages listed in requirements.txt

### 📦 What You Need:
- [ ] GitHub account (free)
- [ ] Render account (free)
- [ ] 10-15 minutes

---

## 🎯 Step 1: Create GitHub Account & Repository

### 1.1 Sign up for GitHub
1. Go to https://github.com/signup
2. Enter your email, create password, choose username
3. Verify your email
4. ✅ You now have a GitHub account!

### 1.2 Create a New Repository
1. Go to https://github.com/new
2. Fill in:
   - **Repository name**: `rbot-line-bot` (or any name you like)
   - **Description**: "LINE Bot for managing sign-up chains"
   - **Visibility**: Choose "Private" (recommended) or "Public"
   - **❌ DO NOT** check "Add a README file"
   - **❌ DO NOT** check "Add .gitignore"
3. Click **"Create repository"**
4. Keep this page open - we'll use it next!

---

## 💻 Step 2: Upload Your Code to GitHub

You have two options: **Easy Way (Web Upload)** or **Git Command Line**

### Option A: Easy Way - Web Upload (Recommended for beginners)

1. On your GitHub repository page, click **"uploading an existing file"** link
2. **Drag and drop** these files from your folder:
   ```
   ✅ app.py
   ✅ requirements.txt
   ✅ gunicorn_config.py
   ✅ render.yaml
   ✅ README.md
   ✅ .env.example
   ```
3. **⚠️ IMPORTANT: DO NOT upload .env file if you have one!**
   - .env contains secrets and should never be uploaded
4. Click **"Commit changes"**
5. ✅ Done! Your code is on GitHub!

### Option B: Git Command Line (For advanced users)

```bash
# In your project folder
cd /path/to/line-jielong

# Initialize git (if not already)
git init

# Create .gitignore to exclude sensitive files
echo ".env" > .gitignore
echo "*.db" >> .gitignore
echo "__pycache__/" >> .gitignore
echo "*.pyc" >> .gitignore

# Add all files
git add .

# Commit
git commit -m "Initial commit - RBOT LINE Bot"

# Connect to GitHub (replace YOUR_USERNAME and YOUR_REPO)
git remote add origin https://github.com/YOUR_USERNAME/rbot-line-bot.git

# Push to GitHub
git branch -M main
git push -u origin main
```

---

## ☁️ Step 3: Deploy to Render

### 3.1 Sign up for Render
1. Go to https://render.com/register
2. Click **"Sign up with GitHub"** (easiest option)
3. Authorize Render to access your GitHub
4. ✅ You now have a Render account!

### 3.2 Create New Web Service
1. On Render Dashboard, click **"New +"** → **"Web Service"**
2. Connect your GitHub repository:
   - If you don't see your repo, click **"Configure account"**
   - Give Render access to your repository
   - Return to Render and refresh
3. Select your **rbot-line-bot** repository
4. Click **"Connect"**

### 3.3 Configure the Service

Render should auto-detect settings from `render.yaml`, but verify:

**Basic Settings:**
- **Name**: `rbot-line-bot` (or your choice)
- **Region**: Oregon (Free) or Singapore (closer to Taiwan)
- **Branch**: `main`
- **Root Directory**: (leave blank)

**Build & Deploy:**
- **Runtime**: `Python 3`
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 0 app:app`

**Instance Type:**
- Select **"Free"** (free forever, no credit card needed!)
- ⚠️ Note: Free tier sleeps after 15 min of inactivity (wakes up automatically when LINE sends message)

### 3.4 Set Environment Variables ⚠️ CRITICAL!

Scroll down to **"Environment Variables"** section:

Click **"Add Environment Variable"** for each:

1. **LINE_CHANNEL_ACCESS_TOKEN**
   - Value: `paste your token from LINE Developers Console`

2. **LINE_CHANNEL_SECRET**
   - Value: `paste your secret from LINE Developers Console`

3. **DB_PATH**
   - Value: `jielong.db`

4. **PORT**
   - Value: `10000`

### 3.5 Deploy!
1. Click **"Create Web Service"**
2. ⏳ Wait 2-5 minutes while Render:
   - Builds your app
   - Installs packages
   - Starts your bot
3. Watch the logs - you should see:
   ```
   [排程] 已啟動，每天 07:00（台灣時間）自動推播接龍名單
   ```
4. ✅ When you see "Your service is live 🎉", you're done!

### 3.6 Get Your Webhook URL

At the top of your Render service page, you'll see:
```
https://rbot-line-bot-xxxx.onrender.com
```

Your webhook URL is:
```
https://rbot-line-bot-xxxx.onrender.com/webhook
                                         ^^^^^^^^
                                         add /webhook at the end!
```

**📋 Copy this URL** - we'll use it in the next step!

---

## 🔗 Step 4: Configure LINE Webhook

### 4.1 Go to LINE Developers Console
1. Open https://developers.line.biz/console/
2. Select your provider
3. Select your **RBOT** channel
4. Go to **"Messaging API"** tab

### 4.2 Set Webhook URL
1. Find **"Webhook settings"** section
2. Click **"Edit"** next to Webhook URL
3. Paste your URL: `https://rbot-line-bot-xxxx.onrender.com/webhook`
4. Click **"Update"**
5. Click **"Verify"** button
   - Should show ✅ "Success" in green
   - If it shows error, check your Render logs

### 4.3 Enable Webhook
1. Toggle **"Use webhook"** to **ON** (Enabled)

### 4.4 Important Settings (verify these):

**✅ Must be ON:**
- **Use webhook**: Enabled
- **Allow bot to join group chats**: Enabled

**❌ Should be OFF (to avoid duplicates):**
- **Auto-reply messages**: Disabled
- **Greeting messages**: Optional (you can customize)

### 4.5 Get Bot QR Code
1. Scroll to **"Bot information"** section
2. Find **"Bot basic ID"** or **QR code**
3. Use this to add RBOT as a friend!

---

## 🧪 Step 5: Test Your Bot!

### Test 1: One-on-One Chat
1. Add RBOT as a friend (scan QR code or search by Basic ID)
2. Send: `說明`
3. ✅ Should receive help message instantly
4. Send: `接龍 測試`
5. ✅ Should receive confirmation

### Test 2: Group Chat
1. Create a test group (you + 1 friend)
2. Add RBOT to the group
3. ✅ Should receive welcome message:
   ```
   👋 大家好！我是接龍助理 RBOT
   我可以幫大家管理工作分派、團購、活動報名等接龍事項。
   ...
   ```
4. Send: `接龍 志工排班`
5. Send: `+1 小明 早班`
6. Send: `列表`
7. ✅ Should show the list!

---

## 🎉 Success! Your Bot is Live!

**Your bot is now running 24/7 on Render!**

### 📊 Monitor Your Bot

**View Logs:**
1. Go to Render Dashboard
2. Click your service
3. Click **"Logs"** tab
4. See real-time activity

**Restart Bot (if needed):**
1. Go to your service
2. Click **"Manual Deploy"** → **"Deploy latest commit"**

---

## 🔧 Troubleshooting

### Bot doesn't respond
- Check Render logs for errors
- Verify environment variables are set
- Check webhook is verified (green checkmark in LINE Console)

### Bot leaves group immediately
- Make sure code has JoinEvent handler ✅ (already added!)
- Check "Allow bot to join group chats" is ON
- Check webhook is responding (Render logs should show incoming requests)

### Free tier sleeping
- Render free tier sleeps after 15 min inactivity
- First message after sleep takes 30-60 seconds to wake up
- This is normal for free tier

### Upgrade to prevent sleeping
- Upgrade to Render paid plan ($7/month)

---

## 📝 Next Steps

- ✅ Bot is deployed and working
- ✅ Webhook is configured
- ✅ Group members can use RBOT

**Optional improvements:**
- Set up monitoring (uptime checker)
- Add more features to the bot
- Customize welcome messages
- Add more commands

---

## 🔐 Security Notes

- ✅ Never commit .env to GitHub
- ✅ Environment variables are secret in Render
- ✅ Keep your LINE tokens private
- ✅ Use Private GitHub repository if possible

---

## 📞 Need Help?

**Check logs first:**
- Render Dashboard → Your Service → Logs

**Common issues:**
- "Module not found": requirements.txt missing a package
- "Invalid signature": LINE_CHANNEL_SECRET incorrect
- "Unauthorized": LINE_CHANNEL_ACCESS_TOKEN incorrect

---

**Last Updated:** 2026-02-11
**Platform:** Render (Free Tier)
**Status:** Production Ready ✅

---

# 🎯 Quick Reference Card

```
Webhook URL Format:
https://YOUR-APP-NAME.onrender.com/webhook

Environment Variables Needed:
- LINE_CHANNEL_ACCESS_TOKEN=your_token_here
- LINE_CHANNEL_SECRET=your_secret_here
- DB_PATH=jielong.db
- PORT=10000

Bot Commands:
接龍 [名稱] - Start sign-up
+1 [名字] [項目] - Join
列表 - View list
退出 - Leave
結束接龍 - Close
說明 - Help
```

Good luck! 🚀
